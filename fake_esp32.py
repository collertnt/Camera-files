"""Pretend to be the ESP32 so you can test the website without hardware.

    python tools/fake_esp32.py --server https://your-site.onrender.com --token YOUR_TOKEN
    (add --auto 30 when testing locally: starts/stops a 30-second recording by itself)

It draws a fake whiteboard (new line every few seconds, new "slide" every 15 s)
and sends a tone as audio, using exactly the same HTTP protocol as the firmware.
"""
import argparse
import io
import math
import struct
import threading
import time

import requests
from PIL import Image, ImageDraw, ImageFont

try:
    FONT = ImageFont.load_default(size=26)
except TypeError:  # Pillow < 10.1
    FONT = ImageFont.load_default()

LINES = [
    "Projectile motion", "v_x = v0 cos(theta)", "v_y = v0 sin(theta) - g t", "T = 2 v0 sin(theta) / g",
    "R = v0^2 sin(2 theta) / g", "Example: v0 = 20 m/s, theta = 30 deg", "R = 35.3 m", "Max range at 45 deg",
]


def frame(t: float) -> bytes:
    slide = int(t // 15)
    tint = [(245, 245, 240), (235, 244, 255), (255, 243, 230), (238, 255, 238)][slide % 4]
    im = Image.new("RGB", (640, 480), tint)
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, 640, 40], fill=(40 + slide * 50 % 200, 70, 160))
    d.text((12, 6), f"Slide {slide + 1}", fill="white", font=FONT)
    shown = int((t % 15) // 3) + 1
    for i in range(shown):
        d.text((30, 70 + i * 60), LINES[(slide * 3 + i) % len(LINES)], fill=(20, 20, 20), font=FONT)
    d.ellipse([500 + 20 * math.sin(t), 300, 560 + 20 * math.sin(t), 360], outline=(200, 30, 30), width=3)
    b = io.BytesIO()
    im.save(b, "JPEG", quality=80)
    return b.getvalue()


def audio_chunk(offset: int, n: int, rate: int) -> bytes:
    out = bytearray()
    for i in range(offset, offset + n):
        t = i / rate
        env = 0.5 + 0.5 * math.sin(2 * math.pi * 0.7 * t)  # "syllables"
        out += struct.pack("<h", int(6000 * env * math.sin(2 * math.pi * 220 * t)))
    return bytes(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default="http://127.0.0.1:8000")
    ap.add_argument("--token", required=True)
    ap.add_argument("--auto", type=float, default=0, help="start a recording, stop after N seconds")
    ap.add_argument("--title", default="Simulated physics class")
    ap.add_argument("--password", default="", help="website password (only needed with --auto)")
    a = ap.parse_args()

    H = {"X-Device-Token": a.token, "X-Device-Id": "fake-esp32", "X-RSSI": "-55", "X-Mic": "1"}
    st = {"record": False, "session": "", "fps": 1.0, "preview": False}
    sess = {"id": "", "t0": 0.0, "samples": 0}
    rate, chunk = 16000, 8000
    s = requests.Session()

    def apply(j):
        st.update(j)
        if j["record"] and j["session"] != sess["id"]:
            sess.update(id=j["session"], t0=time.time(), samples=0)
            print("[fake] START", j["session"])
        elif not j["record"] and sess["id"]:
            print("[fake] STOP", sess["id"])
            sess["id"] = ""

    if a.auto:
        def driver():
            time.sleep(2)
            web = requests.Session()
            web.post(a.server + "/login", data={"password": a.password})
            print("[fake] start:", web.post(a.server + "/api/start", json={"title": a.title}).json())
            time.sleep(a.auto)
            print("[fake] stop:", web.post(a.server + "/api/stop").json())
        threading.Thread(target=driver, daemon=True).start()

    last_poll = last_frame = 0.0
    end_after = (a.auto + 6) if a.auto else None
    start = time.time()
    while end_after is None or time.time() - start < end_after:
        now = time.time()
        try:
            if now - last_poll > 1:
                last_poll = now
                r = s.get(a.server + "/api/device/poll", headers=H, timeout=5)
                if r.status_code == 401:
                    raise SystemExit("401: wrong token")
                apply(r.json())
            rec = bool(sess["id"])
            fps = st["fps"] if rec else (2 if st["preview"] else 0)
            if fps and now - last_frame >= 1 / fps:
                last_frame = now
                t = now - sess["t0"] if rec else 0
                q = {"session": sess["id"], "t": int(t * 1000)} if rec else {"session": "", "t": 0}
                apply(s.post(a.server + "/api/device/frame", params=q, data=frame(t), headers=H, timeout=5).json())
            if sess["id"] and (time.time() - sess["t0"]) * rate >= sess["samples"] + chunk:
                q = {"session": sess["id"], "offset": sess["samples"], "rate": rate}
                data = audio_chunk(sess["samples"], chunk, rate)
                sess["samples"] += chunk
                apply(s.post(a.server + "/api/device/audio", params=q, data=data, headers=H, timeout=5).json())
        except requests.RequestException as e:
            print("[fake] network:", e)
            time.sleep(1)
        time.sleep(0.02)


if __name__ == "__main__":
    main()
