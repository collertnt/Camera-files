# Class Recorder: ESP32 camera + mic → your own website → video for any AI

My ESP32 uploads what it **sees and hears** to my website over the internet. When you press **Stop**,
the website builds the recording into an **MP4 video** (photos + sound, in sync). It also makes an
**AI pack** for AIs that can't watch videos, like Claude: a PDF of every board/slide, a transcript, and
a ready-made prompt for detailed notes. The raw upload is deleted once the video is built.

You can set everything up **from a web browser**, with no command window needed.
The only step that needs a program installed is flashing the ESP32 once

```
ESP32 camera + mic ──Wi-Fi/internet (HTTPS)──▶ your website (Render) ──▶ MP4 + slides PDF + transcript
                                                      ▲
                          you: phone or any computer, log in, Start / Stop / Download
```

---

