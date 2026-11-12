"""Pick the few frames worth keeping from a recording.

Idea: split the recording into "scenes" whenever a noticeable part of the picture
changes (new slide, board wiped, new diagram, camera moved). For each scene keep its
LAST frame — on a whiteboard that is the moment with the most writing on it.
"""
from __future__ import annotations

from pathlib import Path

try:
    from PIL import Image, ImageChops, ImageFilter, ImageOps
except ImportError:  # Pillow missing: fall back to evenly spaced frames
    Image = None

THUMB = (160, 120)


def _thumb(path: Path):
    with Image.open(path) as im:
        return ImageOps.grayscale(im.convert("RGB")).resize(THUMB).filter(ImageFilter.GaussianBlur(1))


def _changed(a, b, level: int = 28) -> float:
    """Fraction of pixels that changed noticeably (robust to sensor noise and lighting flicker)."""
    mask = ImageChops.difference(a, b).point(lambda v: 255 if v > level else 0)
    return mask.histogram()[255] / (THUMB[0] * THUMB[1])


def _even(frames, n):
    if len(frames) <= n:
        return list(frames)
    step = len(frames) / n
    return [frames[int(i * step + step / 2)] for i in range(n)]


def select(frames: list[tuple[float, Path]], max_n: int = 24, threshold: float = 0.03,
           min_scene_s: float = 2.0, max_gap_s: float = 180.0,
           abrupt: float = 0.04) -> list[tuple[float, Path]]:
    if not frames or max_n <= 0:
        return []
    if Image is None:
        return _even(frames, max_n)

    thumbs = []
    for t, p in frames:
        try:
            thumbs.append((t, p, _thumb(p)))
        except Exception:
            continue  # corrupt JPEG (Wi-Fi hiccup)
    if not thumbs:
        return []

    # 1) Split into scenes: a new scene starts when the picture has drifted far enough from
    #    the scene's first frame. Remember how abrupt the cut was (frame-to-frame change).
    scenes = []  # [start_t, end_t, last_frame, cut_strength]
    ref = thumbs[0][2]
    cur = [thumbs[0][0], thumbs[0][0], thumbs[0], 1.0]
    prev = thumbs[0][2]
    for t, p, th in thumbs[1:]:
        if _changed(ref, th) > threshold or t - cur[0] > max_gap_s:
            scenes.append(cur)
            cur = [t, t, (t, p, th), _changed(prev, th)]
            ref = th
        else:
            cur[1], cur[2] = t, (t, p, th)
        prev = th
    scenes.append(cur)

    # 2) Gradual changes (someone writing line after line) are one scene: merge them and keep
    #    the fullest board. Abrupt cuts (new slide, board wiped) start a new keyframe.
    merged = [scenes[0]]
    for sc in scenes[1:]:
        last = merged[-1]
        if sc[3] < abrupt and sc[1] - last[0] <= max_gap_s:
            last[1], last[2] = sc[1], sc[2]
        else:
            merged.append(sc)
    scenes = merged

    # Drop very short scenes (someone walking past the camera) unless that's all we have.
    long_scenes = [s for s in scenes if s[1] - s[0] >= min_scene_s] or scenes

    # Too many? keep the longest / most distinct ones.
    if len(long_scenes) > max_n:
        long_scenes = sorted(long_scenes, key=lambda s: (s[1] - s[0] + 1) * (1 + 10 * s[3]), reverse=True)[:max_n]

    # Remove near-duplicates (the same board reappears after someone stepped in front of it).
    picked: list = []
    for s in sorted(long_scenes, key=lambda s: s[2][0]):
        th = s[2][2]
        if any(_changed(th, q[2]) < threshold * 0.4 for q in picked):
            continue
        picked.append(s[2])
    return [(t, p) for t, p, _ in picked]


def save_resized(src: Path, dst: Path, max_w: int = 1280) -> None:
    if Image is None:
        dst.write_bytes(src.read_bytes())
        return
    with Image.open(src) as im:
        im = im.convert("RGB")
        if im.width > max_w:
            im = im.resize((max_w, int(im.height * max_w / im.width)))
        im.save(dst, "JPEG", quality=85)
