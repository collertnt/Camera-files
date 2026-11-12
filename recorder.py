"""Recordings on disk + turning them into an MP4, a slides PDF, and (optionally) a transcript."""
from __future__ import annotations

import base64
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import traceback
import wave
from pathlib import Path

import requests

from . import keyframes

DATA = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent / "data")).resolve()
REC = DATA / "recordings"
REC.mkdir(parents=True, exist_ok=True)
ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[a-f0-9]{4}$")
GEMINI = "https://generativelanguage.googleapis.com"


def ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    import imageio_ffmpeg  # bundled ffmpeg binary, so hosting needs nothing extra installed
    return imageio_ffmpeg.get_ffmpeg_exe()


def ts(sec: float) -> str:
    sec = int(sec)
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# --------------------------------------------------------------- recordings
class Recordings:
    def __init__(self):
        self.locks: dict[str, threading.Lock] = {}
        self.glock = threading.Lock()

    def lock(self, rid):
        with self.glock:
            return self.locks.setdefault(rid, threading.Lock())

    def path(self, rid: str) -> Path:
        if not ID_RE.match(rid or ""):
            raise ValueError("bad id")
        return REC / rid

    def create(self, title: str) -> str:
        rid = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
        (self.path(rid) / "frames").mkdir(parents=True)
        self._write(rid, {"id": rid, "title": title or "Class recording", "created": time.time(),
                          "status": "recording", "frames": 0, "audio_samples": 0, "audio_rate": 16000})
        return rid

    def _write(self, rid, meta):
        p = self.path(rid) / "meta.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(meta, indent=1), "utf-8")
        tmp.replace(p)

    def meta(self, rid) -> dict:
        try:
            return json.loads((self.path(rid) / "meta.json").read_text("utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def update(self, rid, **kw) -> dict:
        with self.lock(rid):
            m = self.meta(rid)
            m.update(kw)
            self._write(rid, m)
            return m

    def add_frame(self, rid, t_ms: int, jpeg: bytes):
        with self.lock(rid):
            m = self.meta(rid)
            m["frames"] += 1
            (self.path(rid) / "frames" / f"{int(t_ms):010d}_{m['frames']:06d}.jpg").write_bytes(jpeg)
            self._write(rid, m)

    def add_audio(self, rid, offset: int, rate: int, pcm: bytes):
        p = self.path(rid) / "audio.pcm"
        with self.lock(rid):
            with open(p, "r+b" if p.exists() else "w+b") as f:
                f.seek(offset * 2)  # gaps from dropped chunks become silence
                f.write(pcm)
            m = self.meta(rid)
            m["audio_rate"] = rate
            m["audio_samples"] = max(m["audio_samples"], offset + len(pcm) // 2)
            self._write(rid, m)

    def list(self) -> list[dict]:
        out = []
        for d in sorted(REC.iterdir(), reverse=True):
            m = self.meta(d.name) if ID_RE.match(d.name) else {}
            if m:
                m["files"] = {f.name: f.stat().st_size for f in d.iterdir()
                              if f.is_file() and f.suffix in (".mp4", ".pdf", ".txt", ".zip")}
                out.append(m)
        return out

    def delete(self, rid):
        shutil.rmtree(self.path(rid), ignore_errors=True)


recs = Recordings()


# -------------------------------------------------------------- processing
def _frames(d: Path) -> list[tuple[float, Path]]:
    out = []
    for f in sorted((d / "frames").glob("*.jpg")):
        try:
            out.append((int(f.stem.split("_")[0]) / 1000, f))
        except ValueError:
            pass
    return out


def _wav(d: Path, rate: int) -> tuple[Path | None, float]:
    pcm, wav = d / "audio.pcm", d / "audio.wav"
    if not pcm.exists() or pcm.stat().st_size < 3200:
        return None, 0.0
    with open(pcm, "rb") as src, wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        while chunk := src.read(1 << 20):
            w.writeframes(chunk)
    pcm.unlink()
    with wave.open(str(wav)) as w:
        return wav, w.getnframes() / w.getframerate()


def make_video(d: Path, frames, wav: Path | None, audio_dur: float, out: Path) -> None:
    """Frames keep their real timing (1 photo/sec stays 1 photo/sec) and line up with the audio."""
    cmd = [ffmpeg(), "-y", "-loglevel", "error"]
    if frames:
        times = [t for t, _ in frames]
        end = max(audio_dur, times[-1] + 1.0)
        lines = []
        if times[0] > 0.05:  # the video starts at 0 like the audio: hold the first photo until then
            lines += [f"file '{frames[0][1].name}'", f"duration {times[0]:.3f}"]
        for i, (t, p) in enumerate(frames):
            nxt = times[i + 1] if i + 1 < len(frames) else end
            lines += [f"file '{p.name}'", f"duration {max(nxt - t, 0.04):.3f}"]
        lines.append(f"file '{frames[-1][1].name}'")
        (d / "frames" / "list.txt").write_text("\n".join(lines) + "\n")
        w, h = _size(frames[0][1])
        cmd += ["-f", "concat", "-safe", "0", "-i", "list.txt"]
        vf = f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,format=yuv420p"
    else:
        cmd += ["-f", "lavfi", "-i", "color=c=black:s=640x480:r=1"]
        vf = "format=yuv420p"
    if wav:
        cmd += ["-i", str(wav.resolve())]
    cmd += ["-map", "0:v"] + (["-map", "1:a", "-c:a", "aac", "-b:a", "64k"] if wav else [])
    cmd += ["-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-tune", "stillimage",
            "-fps_mode", "vfr", "-movflags", "+faststart"]
    if not frames:
        cmd += ["-shortest"]
    cmd += [str(out.resolve())]
    cwd = d / "frames"  # the list uses bare file names
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0 and "fps_mode" in (r.stderr or ""):  # older ffmpeg
        cmd[cmd.index("-fps_mode"):cmd.index("-fps_mode") + 2] = ["-vsync", "vfr"]
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0 or not out.exists():
        raise RuntimeError("Video compile failed: " + (r.stderr or "")[-600:])


def _size(p: Path) -> tuple[int, int]:
    from PIL import Image
    with Image.open(p) as im:
        return im.width // 2 * 2, im.height // 2 * 2


def make_slides_pdf(frames, out: Path, max_n: int = 40) -> int:
    from PIL import Image, ImageDraw
    picked = keyframes.select(frames, max_n)
    pages = []
    for t, p in picked:
        im = Image.open(p).convert("RGB")
        bar = Image.new("RGB", (im.width, 28), (30, 30, 30))
        ImageDraw.Draw(bar).text((10, 8), f"Photo at {ts(t)} into the class", fill="white")
        page = Image.new("RGB", (im.width, im.height + 28), "white")
        page.paste(bar, (0, 0))
        page.paste(im, (0, 28))
        pages.append(page)
    if pages:
        pages[0].save(out, "PDF", save_all=True, append_images=pages[1:], resolution=96)
    return len(pages)


TRANSCRIBE_PROMPT = ("Transcribe this classroom audio verbatim. Start a new paragraph for every speaker change "
                     "or about every 30 seconds, each beginning with its timestamp like [mm:ss]. Keep technical "
                     "terms, numbers and formulas exactly. Mark unclear words [inaudible]. If there is no "
                     "speech, output exactly NO_SPEECH.")


def gemini_transcript(wav: Path) -> str:
    key = os.environ.get("GEMINI_API_KEY", "")
    model = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
    h = {"x-goog-api-key": key}
    size = wav.stat().st_size
    if size < 14_000_000:
        part = {"inlineData": {"mimeType": "audio/wav", "data": base64.b64encode(wav.read_bytes()).decode()}}
        name = None
    else:
        s = requests.post(f"{GEMINI}/upload/v1beta/files", headers={**h, "X-Goog-Upload-Protocol": "resumable",
                          "X-Goog-Upload-Command": "start", "X-Goog-Upload-Header-Content-Length": str(size),
                          "X-Goog-Upload-Header-Content-Type": "audio/wav"},
                          json={"file": {"display_name": "class-audio"}}, timeout=60)
        s.raise_for_status()
        with open(wav, "rb") as f:
            u = requests.post(s.headers["x-goog-upload-url"], data=f, timeout=(15, 3600), headers={
                "Content-Length": str(size), "X-Goog-Upload-Offset": "0", "X-Goog-Upload-Command": "upload, finalize"})
        u.raise_for_status()
        info = u.json()["file"]
        name = info["name"]
        for _ in range(200):
            if info.get("state") != "PROCESSING":
                break
            time.sleep(3)
            info = requests.get(f"{GEMINI}/v1beta/{name}", headers=h, timeout=30).json()
        part = {"fileData": {"mimeType": "audio/wav", "fileUri": info["uri"]}}
    try:
        r = requests.post(f"{GEMINI}/v1beta/models/{model}:generateContent", headers=h, timeout=(15, 900),
                          json={"contents": [{"role": "user", "parts": [part, {"text": TRANSCRIBE_PROMPT}]}],
                                "generationConfig": {"maxOutputTokens": 65536}})
        if r.status_code != 200:
            raise RuntimeError(f"Gemini error {r.status_code}: {r.text[:300]}")
        c = r.json()["candidates"][0]
        text = "".join(p.get("text", "") for p in c["content"]["parts"] if not p.get("thought")).strip()
        return "" if text == "NO_SPEECH" else text
    finally:
        if name:  # don't leave the audio on Google's servers
            requests.delete(f"{GEMINI}/v1beta/{name}", headers=h, timeout=30)


NOTES_PROMPT = """You are an expert teacher. Attached is material from a class I recorded{what}.
Turn it into complete study notes that teach the topic FROM SCRATCH, so I can learn everything even if I missed the class:

1. TL;DR summary and a list of what I'll learn
2. Prerequisites, each with a short refresher
3. A big-picture diagram of how the ideas connect (Mermaid or a clear text diagram)
4. Each concept in teaching order: plain-language intuition + analogy, the formal definition/formula with every symbol explained, a detailed diagram, and common mistakes
5. EVERY example from the class, solved step by step, explaining why each step is done (cite the timestamp)
6. 3 HARDER examples than the class did (exam-style, multi-step), with full worked solutions
7. 5 practice problems from easy to hard, with answers at the end
8. A cheat sheet table of formulas/rules and a glossary
Copy any text, equations or drawings visible on the board/slides into the notes. If you add information that wasn't in the class, label it "Beyond the class"."""


def process(rid: str) -> None:
    d = recs.path(rid)
    try:
        m = recs.update(rid, status="compiling", step="Building the video…", error=None)
        frames = _frames(d)
        wav, audio_dur = _wav(d, m.get("audio_rate", 16000))
        if not frames and not wav:
            recs.update(rid, status="error", error="Nothing arrived from the ESP32 during this recording.")
            return
        duration = max(audio_dur, frames[-1][0] if frames else 0)
        safe = re.sub(r"[^A-Za-z0-9 _-]", "", m.get("title", "class")).strip().replace(" ", "_")[:50] or "class"
        video = d / f"{safe}.mp4"
        make_video(d, frames, wav, audio_dur, video)

        recs.update(rid, step="Making the slides PDF…")
        pdf = d / f"{safe}_slides.pdf"
        n_slides = make_slides_pdf(frames, pdf) if frames else 0

        transcript_note = ""
        if wav and os.environ.get("GEMINI_API_KEY"):
            recs.update(rid, step="Writing the transcript (Gemini)…")
            try:
                text = gemini_transcript(wav)
                (d / f"{safe}_transcript.txt").write_text(text or "(no speech detected)", "utf-8")
            except Exception as e:
                transcript_note = f"Transcript failed: {e}"

        what = []
        if n_slides:
            what.append("a PDF of photos of the board/slides with timestamps")
        if (d / f"{safe}_transcript.txt").exists():
            what.append("a timestamped transcript of everything said")
        prompt = NOTES_PROMPT.format(what=(": " + " and ".join(what)) if what else "")
        (d / "ai_prompt.txt").write_text(prompt, "utf-8")

        # The Claude pack: everything a non-video AI needs, in one download
        import zipfile
        with zipfile.ZipFile(d / f"{safe}_AI_pack.zip", "w", zipfile.ZIP_DEFLATED) as z:
            for f in [pdf, d / f"{safe}_transcript.txt", d / "ai_prompt.txt"]:
                if f.exists():
                    z.write(f, f.name)

        # Raw material is no longer needed: delete it to save space
        shutil.rmtree(d / "frames", ignore_errors=True)
        for f in (wav,):
            if f and f.exists():
                f.unlink()
        recs.update(rid, status="ready", step="", duration=duration, slides=n_slides,
                    video=video.name, pdf=pdf.name if n_slides else None,
                    transcript=f"{safe}_transcript.txt" if (d / f"{safe}_transcript.txt").exists() else None,
                    pack=f"{safe}_AI_pack.zip", note=transcript_note)
    except Exception as e:
        traceback.print_exc()
        recs.update(rid, status="error", error=str(e)[:500])


def process_async(rid: str) -> None:
    threading.Thread(target=process, args=(rid,), daemon=True).start()
