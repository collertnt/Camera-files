// ============================================================================
//  ESP32 Class Recorder
//  Uploads camera photos + 16 kHz microphone audio to your Class Recorder
//  website over the internet (HTTPS). The website decides when to record
//  (Start/Stop buttons, or the BOOT button on the XIAO).
//
//  Arduino IDE settings
//   XIAO ESP32S3 Sense : Board "XIAO_ESP32S3", PSRAM "OPI PSRAM"
//   AI-Thinker         : Board "AI Thinker ESP32-CAM", PSRAM enabled
//   Requires the "esp32" boards package by Espressif, v3.x recommended.
//
//  Protocol (all requests carry X-Device-Token and X-Device-Id headers)
//   GET  /api/device/poll                         -> {"record":true,"session":"..","fps":1,..}
//   POST /api/device/frame?session=S&t=MS         body = JPEG
//   POST /api/device/audio?session=S&offset=N&rate=R   body = int16 LE mono PCM
//   POST /api/device/toggle                       (button press)
// ============================================================================
#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <WiFiClientSecure.h>
#include "esp_camera.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "config.h"

#if defined(BOARD_XIAO_ESP32S3_SENSE)
  #include <ESP_I2S.h>
  I2SClass i2s;
  #define LED_PIN        21
  #define LED_ACTIVE_LOW 1
  #define BUTTON_PIN     0      // BOOT button, usable after boot
  #define PWDN_GPIO_NUM  -1
  #define RESET_GPIO_NUM -1
  #define XCLK_GPIO_NUM  10
  #define SIOD_GPIO_NUM  40
  #define SIOC_GPIO_NUM  39
  #define Y9_GPIO_NUM    48
  #define Y8_GPIO_NUM    11
  #define Y7_GPIO_NUM    12
  #define Y6_GPIO_NUM    14
  #define Y5_GPIO_NUM    16
  #define Y4_GPIO_NUM    18
  #define Y3_GPIO_NUM    17
  #define Y2_GPIO_NUM    15
  #define VSYNC_GPIO_NUM 38
  #define HREF_GPIO_NUM  47
  #define PCLK_GPIO_NUM  13
#elif defined(BOARD_AI_THINKER_ESP32CAM)
  #include <driver/i2s.h>      // legacy driver; the camera occupies I2S0, so the mic uses I2S1
  #define LED_PIN        33
  #define LED_ACTIVE_LOW 1
  #define BUTTON_PIN     -1     // GPIO0 is the camera clock on this board
  #define PWDN_GPIO_NUM  32
  #define RESET_GPIO_NUM -1
  #define XCLK_GPIO_NUM  0
  #define SIOD_GPIO_NUM  26
  #define SIOC_GPIO_NUM  27
  #define Y9_GPIO_NUM    35
  #define Y8_GPIO_NUM    34
  #define Y7_GPIO_NUM    39
  #define Y6_GPIO_NUM    36
  #define Y5_GPIO_NUM    21
  #define Y4_GPIO_NUM    19
  #define Y3_GPIO_NUM    18
  #define Y2_GPIO_NUM    5
  #define VSYNC_GPIO_NUM 25
  #define HREF_GPIO_NUM  23
  #define PCLK_GPIO_NUM  22
#else
  #error "Pick a board in config.h"
#endif

// ---------------------------------------------------------------- audio pool
// The mic is read by its own task into fixed-size chunks so that slow Wi-Fi
// uploads never cause gaps. Each chunk remembers its sample offset; if the
// pool overflows a chunk is dropped and the server fills the gap with silence.
static const int AUDIO_CHUNK_SAMPLES = 8192;            // 0.5 s @ 16 kHz
static const int AUDIO_POOL_CHUNKS   = 40;              // 20 s of buffering (640 KB in PSRAM) for slow internet
static const int AUDIO_BATCH_CHUNKS  = 6;               // send up to 3 s of audio per upload
static int16_t*  audioSendBuf = nullptr;
static int16_t*  audioPool[AUDIO_POOL_CHUNKS];
static uint32_t  audioOffsets[AUDIO_POOL_CHUNKS];
static QueueHandle_t freeQ, filledQ;
static volatile bool     audioRecording = false;
static volatile uint32_t audioSampleCounter = 0;        // samples since session start
static volatile uint32_t audioDroppedChunks = 0;
static bool micOk = false;

// ------------------------------------------------------------- shared state
static String   sessionId = "";
static bool     recording = false;
static bool     previewWanted = false;
static float    targetFps = 1.0f;
static uint32_t sessionStartMs = 0;
static uint32_t lastFrameMs = 0, lastPollMs = 0;
static String   baseUrl;

WiFiClient plainClient;          // used if SERVER_URL starts with http://
WiFiClientSecure secureClient;   // used for https:// (normal for websites)
HTTPClient http;

// ============================================================ small helpers
static void led(bool on) {
  digitalWrite(LED_PIN, (LED_ACTIVE_LOW ? !on : on) ? HIGH : LOW);
}

static bool jsonBool(const String& j, const char* key) {
  String k = String("\"") + key + "\":";
  int i = j.indexOf(k);
  return i >= 0 && j.substring(i + k.length(), i + k.length() + 4) == "true";
}

static String jsonStr(const String& j, const char* key) {
  String k = String("\"") + key + "\":\"";
  int i = j.indexOf(k);
  if (i < 0) return "";
  int s = i + k.length();
  int e = j.indexOf('"', s);
  return e > s ? j.substring(s, e) : "";
}

static float jsonNum(const String& j, const char* key, float def) {
  String k = String("\"") + key + "\":";
  int i = j.indexOf(k);
  if (i < 0) return def;
  return j.substring(i + k.length()).toFloat();
}

static framesize_t parseFramesize(const String& s) {
  if (s == "QVGA") return FRAMESIZE_QVGA;   // 320x240
  if (s == "CIF")  return FRAMESIZE_CIF;    // 400x296
  if (s == "VGA")  return FRAMESIZE_VGA;    // 640x480
  if (s == "SVGA") return FRAMESIZE_SVGA;   // 800x600
  if (s == "XGA")  return FRAMESIZE_XGA;    // 1024x768
  if (s == "HD")   return FRAMESIZE_HD;     // 1280x720
  if (s == "SXGA") return FRAMESIZE_SXGA;   // 1280x1024
  if (s == "UXGA") return FRAMESIZE_UXGA;   // 1600x1200
  return FRAMESIZE_VGA;
}

// Sends one HTTP request; returns status code and fills `body` with the reply.
static int request(const char* method, const String& path, const uint8_t* data, size_t len,
                   const char* contentType, String& body) {
  if (WiFi.status() != WL_CONNECTED) return -1;
  if (baseUrl.startsWith("https")) http.begin(secureClient, baseUrl + path);
  else http.begin(plainClient, baseUrl + path);
  http.setTimeout(20000);   // free hosting can take a while to wake up
  http.addHeader("X-Device-Token", DEVICE_TOKEN);
  http.addHeader("X-Device-Id", DEVICE_ID);
  http.addHeader("X-RSSI", String(WiFi.RSSI()));
  http.addHeader("X-Mic", micOk ? "1" : "0");
  http.addHeader("X-Dropped", String(audioDroppedChunks));
  int code;
  if (strcmp(method, "GET") == 0) {
    code = http.GET();
  } else {
    if (contentType) http.addHeader("Content-Type", contentType);
    code = http.POST((uint8_t*)data, len);
  }
  body = code > 0 ? http.getString() : "";
  http.end();
  return code;
}

// =================================================================== camera
static bool initCamera() {
  camera_config_t c = {};
  c.ledc_channel = LEDC_CHANNEL_0;
  c.ledc_timer   = LEDC_TIMER_0;
  c.pin_d0 = Y2_GPIO_NUM; c.pin_d1 = Y3_GPIO_NUM; c.pin_d2 = Y4_GPIO_NUM; c.pin_d3 = Y5_GPIO_NUM;
  c.pin_d4 = Y6_GPIO_NUM; c.pin_d5 = Y7_GPIO_NUM; c.pin_d6 = Y8_GPIO_NUM; c.pin_d7 = Y9_GPIO_NUM;
  c.pin_xclk = XCLK_GPIO_NUM; c.pin_pclk = PCLK_GPIO_NUM;
  c.pin_vsync = VSYNC_GPIO_NUM; c.pin_href = HREF_GPIO_NUM;
  c.pin_sccb_sda = SIOD_GPIO_NUM; c.pin_sccb_scl = SIOC_GPIO_NUM;
  c.pin_pwdn = PWDN_GPIO_NUM; c.pin_reset = RESET_GPIO_NUM;
  c.xclk_freq_hz = 20000000;
  c.pixel_format = PIXFORMAT_JPEG;
  c.frame_size   = FRAMESIZE_UXGA;        // allocate for the largest size, then step down
  c.jpeg_quality = 12;
  c.fb_count     = 2;
  c.fb_location  = CAMERA_FB_IN_PSRAM;
  c.grab_mode    = CAMERA_GRAB_LATEST;    // always send the freshest frame

  esp_err_t err = esp_camera_init(&c);
  if (err != ESP_OK) {
    Serial.printf("[cam] init failed 0x%x — check board selection and PSRAM setting\n", err);
    return false;
  }
  sensor_t* s = esp_camera_sensor_get();
  s->set_framesize(s, FRAMESIZE_VGA);
#if defined(BOARD_AI_THINKER_ESP32CAM)
  s->set_vflip(s, 0);
#endif
  Serial.println("[cam] ready");
  return true;
}

// ===================================================================== mic
static bool initMic() {
#if defined(BOARD_XIAO_ESP32S3_SENSE)
  i2s.setPinsPdmRx(42, 41);  // CLK, DATA of the on-board PDM microphone
  if (!i2s.begin(I2S_MODE_PDM_RX, AUDIO_SAMPLE_RATE, I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO)) {
    Serial.println("[mic] PDM init failed");
    return false;
  }
#else
  i2s_config_t cfg = {};
  cfg.mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX);
  cfg.sample_rate = AUDIO_SAMPLE_RATE;
  cfg.bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT;
  cfg.channel_format = MIC_USE_RIGHT_CHANNEL ? I2S_CHANNEL_FMT_ONLY_RIGHT : I2S_CHANNEL_FMT_ONLY_LEFT;
  cfg.communication_format = I2S_COMM_FORMAT_STAND_I2S;
  cfg.intr_alloc_flags = ESP_INTR_FLAG_LEVEL1;
  cfg.dma_buf_count = 8;
  cfg.dma_buf_len = 256;
  cfg.use_apll = false;
  i2s_pin_config_t pins = {};
  pins.mck_io_num = I2S_PIN_NO_CHANGE;
  pins.bck_io_num = I2S_SCK_PIN;
  pins.ws_io_num = I2S_WS_PIN;
  pins.data_out_num = I2S_PIN_NO_CHANGE;
  pins.data_in_num = I2S_SD_PIN;
  if (i2s_driver_install(I2S_NUM_1, &cfg, 0, NULL) != ESP_OK || i2s_set_pin(I2S_NUM_1, &pins) != ESP_OK) {
    Serial.println("[mic] I2S init failed");
    return false;
  }
#endif
  Serial.println("[mic] ready");
  return true;
}

// Reads exactly `n` 16-bit samples into dst (blocking).
static void readMic(int16_t* dst, int n) {
#if defined(BOARD_XIAO_ESP32S3_SENSE)
  size_t got = 0;
  while (got < (size_t)n * 2) {
    size_t r = i2s.readBytes((char*)dst + got, n * 2 - got);
    if (r == 0) vTaskDelay(1);
    got += r;
  }
  for (int i = 0; i < n; i++) {
    int32_t v = (int32_t)dst[i] << MIC_GAIN_SHIFT;
    dst[i] = v > 32767 ? 32767 : (v < -32768 ? -32768 : v);
  }
#else
  static int32_t raw[512];
  int done = 0;
  while (done < n) {
    int want = min(512, n - done);
    size_t bytes = 0;
    i2s_read(I2S_NUM_1, raw, want * 4, &bytes, portMAX_DELAY);
    int got = bytes / 4;
    for (int i = 0; i < got; i++) {
      int32_t v = (raw[i] >> 16) << MIC_GAIN_SHIFT;    // 24-bit sample, left-justified
      dst[done + i] = v > 32767 ? 32767 : (v < -32768 ? -32768 : v);
    }
    done += got;
  }
#endif
}

static void audioTask(void*) {
  int16_t* scratch = (int16_t*)heap_caps_malloc(AUDIO_CHUNK_SAMPLES * 2, MALLOC_CAP_SPIRAM);
  for (;;) {
    if (!audioRecording) {
      readMic(scratch, 512);   // keep the DMA drained while idle
      continue;
    }
    int idx;
    bool haveSlot = xQueueReceive(freeQ, &idx, 0) == pdTRUE;
    uint32_t offset = audioSampleCounter;
    readMic(haveSlot ? audioPool[idx] : scratch, AUDIO_CHUNK_SAMPLES);
    audioSampleCounter = offset + AUDIO_CHUNK_SAMPLES;
    if (!audioRecording) {               // session ended while we were reading
      if (haveSlot) xQueueSend(freeQ, &idx, 0);
      continue;
    }
    if (haveSlot) {
      audioOffsets[idx] = offset;
      xQueueSend(filledQ, &idx, 0);
    } else {
      audioDroppedChunks++;              // Wi-Fi too slow; server pads this gap with silence
    }
  }
}

static void flushAudioQueue() {
  int idx;
  while (xQueueReceive(filledQ, &idx, 0) == pdTRUE) xQueueSend(freeQ, &idx, 0);
}

// ============================================================ session logic
static void applyPoll(const String& j) {
  bool wantRec = jsonBool(j, "record");
  String sid = jsonStr(j, "session");
  previewWanted = jsonBool(j, "preview");
  targetFps = constrain(jsonNum(j, "fps", 1.0f), 0.1f, 10.0f);

  sensor_t* s = esp_camera_sensor_get();
  if (s) {
    String fsz = jsonStr(j, "framesize");
    if (fsz.length()) {
      framesize_t f = parseFramesize(fsz);
      if (s->status.framesize != f) s->set_framesize(s, f);
    }
    int q = (int)jsonNum(j, "quality", 12);
    if (q >= 4 && q <= 63 && s->status.quality != q) s->set_quality(s, q);
  }

  if (wantRec && (!recording || sid != sessionId)) {
    audioRecording = false;
    flushAudioQueue();
    sessionId = sid;
    sessionStartMs = millis();
    audioSampleCounter = 0;
    audioDroppedChunks = 0;
    recording = true;
    audioRecording = micOk;
    Serial.printf("[rec] START session %s\n", sid.c_str());
  } else if (!wantRec && recording) {
    recording = false;
    audioRecording = false;
    Serial.printf("[rec] STOP session %s\n", sessionId.c_str());
  }
}

static void sendFrame() {
  camera_fb_t* fb = esp_camera_fb_get();
  if (!fb) { Serial.println("[cam] capture failed"); return; }
  String path = "/api/device/frame?session=" + (recording ? sessionId : String("")) +
                "&t=" + String(recording ? millis() - sessionStartMs : 0);
  String body;
  int code = request("POST", path, fb->buf, fb->len, "image/jpeg", body);
  esp_camera_fb_return(fb);
  if (code == 200) applyPoll(body);
  else Serial.printf("[net] frame POST -> %d\n", code);
}

static void sendAudio() {
  // Join consecutive chunks into one upload: fewer HTTPS requests = keeps up over the internet.
  int idx;
  for (int round = 0; round < 3 && xQueueReceive(filledQ, &idx, 0) == pdTRUE; round++) {
    uint32_t start = audioOffsets[idx];
    int n = 0;
    memcpy(audioSendBuf, audioPool[idx], AUDIO_CHUNK_SAMPLES * 2);
    xQueueSend(freeQ, &idx, 0);
    n = 1;
    int next;
    while (n < AUDIO_BATCH_CHUNKS && xQueuePeek(filledQ, &next, 0) == pdTRUE &&
           audioOffsets[next] == start + (uint32_t)n * AUDIO_CHUNK_SAMPLES) {
      xQueueReceive(filledQ, &next, 0);
      memcpy(audioSendBuf + n * AUDIO_CHUNK_SAMPLES, audioPool[next], AUDIO_CHUNK_SAMPLES * 2);
      xQueueSend(freeQ, &next, 0);
      n++;
    }
    String path = "/api/device/audio?session=" + sessionId + "&offset=" + String(start) +
                  "&rate=" + String(AUDIO_SAMPLE_RATE);
    String body;
    int code = request("POST", path, (uint8_t*)audioSendBuf, n * AUDIO_CHUNK_SAMPLES * 2,
                       "application/octet-stream", body);
    if (code == 200) applyPoll(body);
    else Serial.printf("[net] audio POST -> %d\n", code);
  }
}

static void connectWiFi() {
  if (WiFi.status() == WL_CONNECTED) return;
  Serial.printf("[wifi] connecting to %s", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);                  // lower latency for streaming
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 20000) {
    delay(300); Serial.print("."); led(((millis() / 300) % 2) == 0);
  }
  led(false);
  if (WiFi.status() == WL_CONNECTED)
    Serial.printf("\n[wifi] connected, IP %s, RSSI %d dBm\n", WiFi.localIP().toString().c_str(), WiFi.RSSI());
  else
    Serial.println("\n[wifi] failed — will retry");
}

// =================================================================== setup
void setup() {
  Serial.begin(115200);
  delay(500);
  pinMode(LED_PIN, OUTPUT);
  led(false);
  if (BUTTON_PIN >= 0) pinMode(BUTTON_PIN, INPUT_PULLUP);
  Serial.println("\n=== ESP32 Class Recorder ===");

  if (!psramFound()) Serial.println("[warn] PSRAM not found — enable PSRAM in Tools menu!");
  if (!initCamera()) { delay(3000); ESP.restart(); }
  micOk = initMic();

  freeQ = xQueueCreate(AUDIO_POOL_CHUNKS, sizeof(int));
  filledQ = xQueueCreate(AUDIO_POOL_CHUNKS, sizeof(int));
  for (int i = 0; i < AUDIO_POOL_CHUNKS; i++) {
    audioPool[i] = (int16_t*)heap_caps_malloc(AUDIO_CHUNK_SAMPLES * 2, MALLOC_CAP_SPIRAM);
    xQueueSend(freeQ, &i, 0);
  }
  if (micOk) xTaskCreatePinnedToCore(audioTask, "audio", 4096, NULL, 5, NULL, 0);

  audioSendBuf = (int16_t*)heap_caps_malloc(AUDIO_CHUNK_SAMPLES * 2 * AUDIO_BATCH_CHUNKS, MALLOC_CAP_SPIRAM);
  baseUrl = SERVER_URL;
  if (baseUrl.endsWith("/")) baseUrl.remove(baseUrl.length() - 1);
  secureClient.setInsecure();   // encrypts traffic; skips certificate pinning so it works with any host
  http.setReuse(true);          // keep one connection open (HTTPS handshakes are slow on the ESP32)
  connectWiFi();
}

// ==================================================================== loop
void loop() {
  connectWiFi();
  if (WiFi.status() != WL_CONNECTED) { delay(2000); return; }
  uint32_t now = millis();

  // Poll for commands (once a second when idle; replies to uploads also carry commands)
  if (now - lastPollMs > (recording ? 3000 : 1000)) {
    lastPollMs = now;
    String body;
    int code = request("GET", "/api/device/poll", nullptr, 0, nullptr, body);
    if (code == 200) applyPoll(body);
    else if (code == 401) Serial.println("[net] 401 — DEVICE_TOKEN does not match the server");
    else Serial.printf("[net] poll -> %d (website asleep or wrong SERVER_URL? %s)\n", code, baseUrl.c_str());
  }

  // Physical button (XIAO BOOT key) toggles recording
  if (BUTTON_PIN >= 0 && digitalRead(BUTTON_PIN) == LOW) {
    delay(30);
    if (digitalRead(BUTTON_PIN) == LOW) {
      String body;
      if (request("POST", "/api/device/toggle", nullptr, 0, "text/plain", body) == 200) applyPoll(body);
      while (digitalRead(BUTTON_PIN) == LOW) delay(10);
    }
  }

  // Frames: at target fps while recording; ~2 fps while the dashboard preview is open
  float fps = recording ? targetFps : (previewWanted ? 2.0f : 0.0f);
  if (fps > 0 && now - lastFrameMs >= (uint32_t)(1000.0f / fps)) {
    lastFrameMs = now;
    sendFrame();
  }

  if (recording) sendAudio();
  led(recording && ((now / 500) % 2 == 0));   // blink while recording
  delay(5);
}
