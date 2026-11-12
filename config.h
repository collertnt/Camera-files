// ============================================================================
//  config.h  —  EDIT THIS FILE before flashing
// ============================================================================
#pragma once

// ---- 1. Pick your board (uncomment exactly ONE) -----------------------------
#define BOARD_XIAO_ESP32S3_SENSE   // Seeed XIAO ESP32S3 Sense (camera + built-in PDM mic) — recommended
// #define BOARD_AI_THINKER_ESP32CAM  // AI-Thinker ESP32-CAM + external INMP441 I2S microphone

// ---- 2. Wi-Fi (2.4 GHz only). School Wi-Fi that needs a sign-in page won't work —
//         use your phone's hotspot instead. -------------------------------------
#define WIFI_SSID      "YOUR_WIFI_NAME"
#define WIFI_PASSWORD  "YOUR_WIFI_PASSWORD"

// ---- 3. Your website ------------------------------------------------------------
// The address Render gave your site (no slash at the end).
#define SERVER_URL     "https://class-recorder-xxxx.onrender.com"

// Copy DEVICE_TOKEN from Render → your service → Environment.
// It stops anyone else from sending data to your website.
#define DEVICE_TOKEN   "PASTE_DEVICE_TOKEN_HERE"
#define DEVICE_ID      "esp32-cam-1"

// ---- 4. Audio ----------------------------------------------------------------
#define AUDIO_SAMPLE_RATE 16000     // 16 kHz mono is ideal for speech recognition
#define MIC_GAIN_SHIFT    2         // digital gain: 0 = x1, 1 = x2, 2 = x4, 3 = x8 (raise if recordings are quiet)

// AI-Thinker + INMP441 wiring (ignored on XIAO). SD card slot must NOT be used.
//   INMP441 SCK -> GPIO14, WS -> GPIO15, SD -> GPIO13, L/R -> GND, VDD -> 3V3, GND -> GND
#define I2S_SCK_PIN 14
#define I2S_WS_PIN  15
#define I2S_SD_PIN  13
// If you get silence, either tie INMP441 L/R to 3V3 or set this to 1:
#define MIC_USE_RIGHT_CHANNEL 0
