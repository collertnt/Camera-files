"""Class Recorder website: the ESP32 uploads here; you control recording and download the results."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
from pathlib import Path

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .recorder import recs, process_async, REC

STATIC = Path(__file__).parent / "static"
PASSWORD = os.environ.get("SITE_PASSWORD", "")
DEVICE_TOKEN = os.environ.get("DEVICE_TOKEN", "")
SECRET = hashlib.sha256(f"{PASSWORD}|{DEVICE_TOKEN}|{os.environ.get('SECRET_KEY', '')}".encode()).digest()
COOKIE = "cr_session"
FREE_HOSTING = os.environ.get("RENDER") is not None or os.environ.get("FREE_HOSTING") == "1"

lock = threading.Lock()
device = {"last_seen": 0.0, "rssi": None, "mic": None, "dropped": 0, "frame": None, "id": None}
state = {"rid": None, "started": 0.0, "preview_until": 0.0, "fps": float(os.environ.get("FPS", "1")),
         "framesize": os.environ.get("FRAMESIZE", "VGA"), "quality": int(os.environ.get("JPEG_QUALITY", "12"))}

# Recordings cut off by a restart get compiled from whatever arrived.
for _m in recs.list():
    if _m.get("status") in ("recording", "compiling"):
        process_async(_m["id"])


# ------------------------------------------------------------------ auth
def _sign(v: str) -> str:
    return hmac.new(SECRET, v.encode(), hashlib.sha256).hexdigest()


def logged_in(request: Request) -> bool:
    c = request.cookies.get(COOKIE, "")
    if "." not in c:
        return False
    v, sig = c.rsplit(".", 1)
    return hmac.compare_digest(sig, _sign(v)) and time.time() - float(v.split(":")[0]) < 30 * 86400


def need_login(fn):
    async def wrapper(request: Request):
        if not logged_in(request):
            if request.url.path.startswith("/api/"):
                return JSONResponse({"error": "Please log in again"}, status_code=401)
            return RedirectResponse("/login")
        return await fn(request)
    return wrapper


async def login(request: Request):
    if request.method == "GET":
        html = (STATIC / "login.html").read_text("utf-8")
        if not PASSWORD:
            html = html.replace("<!--WARN-->", "<p class='err'>The site owner hasn't set SITE_PASSWORD yet. "
                                "Add it in your hosting dashboard (Environment).</p>")
        return HTMLResponse(html)
    form = await request.form()
    if PASSWORD and hmac.compare_digest(str(form.get("password", "")).encode(), PASSWORD.encode()):
        v = f"{time.time()}:{secrets.token_hex(4)}"
        r = RedirectResponse("/", status_code=303)
        r.set_cookie(COOKIE, f"{v}.{_sign(v)}", max_age=30 * 86400, httponly=True, samesite="lax",
                     secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https")
        return r
    time.sleep(1.5)  # slows down password guessing
    return RedirectResponse("/login?bad=1", status_code=303)


async def logout(request: Request):
    r = RedirectResponse("/login", status_code=303)
    r.delete_cookie(COOKIE)
    return r


# ------------------------------------------------------------ device API
def _device_ok(request: Request) -> bool:
    tok = request.headers.get("x-device-token", "")
    ok = bool(DEVICE_TOKEN) and hmac.compare_digest(tok.encode(), DEVICE_TOKEN.encode())
    if ok:
        with lock:
            device.update(last_seen=time.time(), rssi=request.headers.get("x-rssi"),
                          mic=request.headers.get("x-mic") == "1", id=request.headers.get("x-device-id"),
                          dropped=int(request.headers.get("x-dropped", "0") or 0))
    return ok


def _cmd() -> dict:
    with lock:
        return {"record": state["rid"] is not None, "session": state["rid"] or "",
                "preview": time.time() < state["preview_until"], "fps": state["fps"],
                "framesize": state["framesize"], "quality": state["quality"]}


def device_route(fn):
    async def wrapper(request: Request):
        if not _device_ok(request):
            return JSONResponse({"error": "bad device token"}, status_code=401)
        return await fn(request)
    return wrapper


@device_route
async def d_poll(request):
    return JSONResponse(_cmd())


@device_route
async def d_frame(request):
    body = await request.body()
    if body[:2] != b"\xff\xd8":
        return JSONResponse({"error": "not jpeg"}, status_code=400)
    sid = request.query_params.get("session", "")
    with lock:
        device["frame"] = body
        active = state["rid"]
    if sid and sid == active:
        await run_in_threadpool(recs.add_frame, sid, int(request.query_params.get("t", "0")), body)
    return JSONResponse(_cmd())


@device_route
async def d_audio(request):
    body = await request.body()
    sid = request.query_params.get("session", "")
    with lock:
        active = state["rid"]
    if sid and sid == active and len(body) % 2 == 0:
        await run_in_threadpool(recs.add_audio, sid, int(request.query_params.get("offset", "0")),
                                int(request.query_params.get("rate", "16000")), body)
    return JSONResponse(_cmd())


@device_route
async def d_toggle(request):
    if state["rid"]:
        await run_in_threadpool(_stop)
    else:
        _start("Class recording")
    return JSONResponse(_cmd())


# ------------------------------------------------------------- controls
def _start(title: str) -> str:
    with lock:
        if not state["rid"]:
            state["rid"] = recs.create(title)
            state["started"] = time.time()
        return state["rid"]


def _stop() -> str | None:
    with lock:
        rid = state["rid"]
        state["rid"] = None
    if rid:
        time.sleep(2.0)  # let the last uploads from the ESP32 arrive
        recs.update(rid, status="compiling", step="Waiting…", stopped=time.time())
        process_async(rid)
    return rid


@need_login
async def api_start(request):
    data = await request.json()
    if time.time() - device["last_seen"] > 15:
        return JSONResponse({"error": "The ESP32 isn't connected. Turn it on and check its Wi-Fi."}, status_code=400)
    return JSONResponse({"id": _start((data.get("title") or "").strip()[:120])})


@need_login
async def api_stop(request):
    rid = await run_in_threadpool(_stop)
    return JSONResponse({"id": rid}) if rid else JSONResponse({"error": "Not recording"}, status_code=400)


@need_login
async def api_status(request):
    with lock:
        dev = {k: v for k, v in device.items() if k != "frame"}
        rid, started = state["rid"], state["started"]
        settings = {k: state[k] for k in ("fps", "framesize", "quality")}
    dev["online"] = time.time() - dev["last_seen"] < 8
    cur = None
    if rid:
        m = recs.meta(rid)
        cur = {"id": rid, "title": m.get("title"), "elapsed": time.time() - started, "frames": m.get("frames", 0),
               "audio": m.get("audio_samples", 0) / max(1, m.get("audio_rate", 16000))}
    return JSONResponse({"device": dev, "recording": cur, "recordings": [r for r in recs.list() if r["id"] != rid],
                         "settings": settings, "free_hosting": FREE_HOSTING})


@need_login
async def api_settings(request):
    data = await request.json()
    with lock:
        if "fps" in data:
            state["fps"] = min(10.0, max(0.2, float(data["fps"])))
        if data.get("framesize") in ("QVGA", "CIF", "VGA", "SVGA", "XGA", "HD", "SXGA", "UXGA"):
            state["framesize"] = data["framesize"]
        if "quality" in data:
            state["quality"] = min(40, max(6, int(data["quality"])))
    return JSONResponse({"ok": True})


@need_login
async def api_preview(request):
    with lock:
        state["preview_until"] = time.time() + 6
        frame = device["frame"]
    return Response(frame, media_type="image/jpeg", headers={"Cache-Control": "no-store"}) if frame else Response(status_code=204)


@need_login
async def api_file(request):
    rid, name = request.path_params["rid"], request.path_params["name"]
    d = recs.path(rid).resolve()
    p = (d / name).resolve()
    if p.parent != d or not p.is_file() or p.suffix not in (".mp4", ".pdf", ".txt", ".zip"):
        return JSONResponse({"error": "not found"}, status_code=404)
    inline = request.query_params.get("inline") == "1"
    return FileResponse(p, filename=None if inline else p.name)


@need_login
async def api_delete(request):
    rid = request.path_params["rid"]
    if rid == state["rid"]:
        return JSONResponse({"error": "Stop the recording first"}, status_code=400)
    recs.delete(rid)
    return JSONResponse({"ok": True})


@need_login
async def index(request):
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


async def health(request):
    return JSONResponse({"ok": True})


async def bad_value(request, exc):
    return JSONResponse({"error": str(exc)}, status_code=400)


app = Starlette(routes=[
    Route("/", index),
    Route("/login", login, methods=["GET", "POST"]),
    Route("/logout", logout),
    Route("/health", health),
    Route("/api/device/poll", d_poll),
    Route("/api/device/frame", d_frame, methods=["POST"]),
    Route("/api/device/audio", d_audio, methods=["POST"]),
    Route("/api/device/toggle", d_toggle, methods=["POST"]),
    Route("/api/start", api_start, methods=["POST"]),
    Route("/api/stop", api_stop, methods=["POST"]),
    Route("/api/status", api_status),
    Route("/api/settings", api_settings, methods=["POST"]),
    Route("/api/preview.jpg", api_preview),
    Route("/api/rec/{rid}/{name}", api_file),
    Route("/api/rec/{rid}", api_delete, methods=["DELETE"]),
    Mount("/static", StaticFiles(directory=STATIC)),
], exception_handlers={ValueError: bad_value})
