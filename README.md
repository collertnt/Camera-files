# Class Recorder: ESP32 camera + mic → your own website → video for any AI

Your ESP32 uploads what it **sees and hears** to your website over the internet. When you press **Stop**,
the website builds the recording into an **MP4 video** (photos + sound, in sync). It also makes an
**AI pack** for AIs that can't watch videos, like Claude: a PDF of every board/slide, a transcript, and
a ready-made prompt for detailed notes. The raw upload is deleted once the video is built.

You can set everything up **from a web browser**, with no command window needed.
The only step that needs a program installed is flashing the ESP32 once (step 4).

```
ESP32 camera + mic ──Wi-Fi/internet (HTTPS)──▶ your website (Render) ──▶ MP4 + slides PDF + transcript
                                                      ▲
                          you: phone or any computer, log in, Start / Stop / Download
```

---

## 1. Put the code on GitHub (5 min, browser only)
1. Make a free account at **github.com**.
2. Click **+** (top right) → **New repository**. Name it `class-recorder`, choose **Private**, click **Create repository**.
3. On the next page, click the link **"uploading an existing file"**.
4. Open the unzipped `class-recorder` folder on your computer. Select **everything inside it**
   (`webapp`, `firmware`, `tools`, `run.py`, `requirements.txt`, `render.yaml`, `README.md`, …)
   and drag it all into the GitHub page.
5. Wait for the uploads to finish, then click **Commit changes**.

## 2. Put the website online with Render (5 min, browser only, free)
1. Go to **render.com** → **Get Started** → **sign up with GitHub**.
2. Click **New +** → **Blueprint** → connect your `class-recorder` repository.
3. Render reads the settings file and asks for two things:
   - **SITE_PASSWORD**: invent a password; you'll use it to log in to your site.
   - **GEMINI_API_KEY**: *optional*. Paste a free key from **aistudio.google.com/apikey** to get automatic
     transcripts (needed for the Claude pack to include what was *said*). Leave it empty to skip.
4. Click **Apply**. The first build takes ~3–5 minutes.
5. When it says **Live**, your site's address appears at the top, e.g. `https://class-recorder-ab12.onrender.com`.
   Open it and log in with your password.
6. In Render, open your service → **Environment** → copy the value of **DEVICE_TOKEN** (Render made a random one).

## 3. Get the Wi-Fi right
The ESP32 needs **2.4 GHz Wi-Fi with a normal password**. School Wi-Fi often has a sign-in page or blocks
gadgets, so use your **phone's hotspot** instead (set it to "2.4 GHz" / "maximize compatibility" on iPhone).

## 4. Flash the ESP32 (once, on a computer where you can install programs)
1. Install **Arduino IDE 2** (arduino.cc). Then **Tools → Board → Boards Manager**, search **esp32**,
   install **"esp32 by Espressif Systems"**.
2. Open `firmware/esp32_class_recorder/esp32_class_recorder.ino`.
3. In the **config.h** tab, fill in:
   - which board you have (XIAO ESP32S3 Sense is the default)
   - `WIFI_SSID` / `WIFI_PASSWORD` (your hotspot)
   - `SERVER_URL`: your Render address from step 2.5
   - `DEVICE_TOKEN`: from step 2.6
4. **Tools** menu: XIAO → Board **XIAO_ESP32S3**, PSRAM **OPI PSRAM**. AI-Thinker → Board **AI Thinker ESP32-CAM**.
5. Plug the ESP32 in and click **Upload** (→). Open **Tools → Serial Monitor** (115200 baud) and look for
   `[wifi] connected`, `[cam] ready`, `[mic] ready`.

From then on, just power the ESP32 from a USB power bank.

**AI-Thinker ESP32-CAM only:** wire an INMP441 mic: VDD→3V3, GND→GND, SCK→GPIO14, WS→GPIO15, SD→GPIO13, L/R→GND.

## 5. Record a class
1. Turn on your hotspot and the ESP32. Open your website. The badge turns green: **ESP32 online**.
   *If the site was asleep, the first load takes up to a minute.*
2. Aim the camera using **Live camera**, type the class name, and press **Start recording**.
   (XIAO: you can also press the **BOOT** button on the board to start/stop.)
3. Press **Stop & make video**. After a short wait the recording shows a player and download buttons.
4. **Download right away.** On the free plan, files are erased when the site goes to sleep
   (15 minutes with no ESP32 connected and nobody on the page).

## 6. Get notes from an AI
| AI | What to upload |
|---|---|
| **Gemini** (gemini.google.com) | the **MP4 video**, then click **📋 Copy AI notes prompt** and paste it |
| **Claude, and AIs that can't watch video** | unzip the **AI pack** and upload the **slides PDF + transcript**, then paste the prompt |

The prompt asks for notes that teach from scratch: concepts, diagrams, every class example worked out,
harder examples, practice problems and a cheat sheet.

---

## Settings & limits
- **Camera settings** (on the website): 1 photo/sec at VGA is ideal for boards/slides. Raise the resolution if
  writing is small; that needs a stronger connection.
- One hour of class ≈ 250 MB uploaded while recording, and roughly a 30–100 MB MP4 to download.
  Render's free plan includes a limited amount of outbound data per month (about 5 GB at the time of writing).
- Want files to stay on the site? Use a paid Render instance with a disk, or another host with storage.

## Troubleshooting
| Problem | Fix |
|---|---|
| Serial shows `poll -> -1` or `-11` | Site asleep (wait a minute), wrong `SERVER_URL`, or the Wi-Fi blocks it: try the hotspot |
| Serial shows `401` | `DEVICE_TOKEN` in config.h doesn't match Render → Environment |
| `[cam] init failed` | Wrong board in config.h, or PSRAM not enabled in the Tools menu |
| "audio pieces lost" | Weak signal: move the phone/hotspot closer, lower photos/sec or resolution |
| Video is silent | AI-Thinker: check mic wiring or set `MIC_USE_RIGHT_CHANNEL 1`. Quiet: raise `MIC_GAIN_SHIFT` |
| Login page says SITE_PASSWORD isn't set | Render → your service → Environment → add `SITE_PASSWORD`, then redeploy |

## Test without hardware
On a computer where you can run Python: `pip install -r requirements.txt`, then
`python tools/fake_esp32.py --server https://YOUR-SITE.onrender.com --token YOUR_DEVICE_TOKEN`.
A fake camera appears on your site and you can record with it.

## Files
```
run.py                     starts the website
webapp/app.py              login, ESP32 upload API, Start/Stop, downloads
webapp/recorder.py         builds the MP4, slides PDF, transcript, AI prompt
webapp/keyframes.py        picks one photo per board/slide
webapp/static/             the web pages
render.yaml                Render one-click settings
firmware/                  ESP32 Arduino sketch + config.h
tools/fake_esp32.py        simulator
```
