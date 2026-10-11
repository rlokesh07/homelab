#!/usr/bin/env python3
"""Pull Nest doorbell events and show them on Kodi."""
from __future__ import annotations

import base64
import json
import logging
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CONF = Path.home() / ".config" / "homelab"
# Nest battery doorbell is WebRTC H.264 + Opus at 960x1280 (portrait). Kodi on this
# Pi can't play that live. If something is already playing we overlay a large
# notification with the snapshot (playback continues). Otherwise fullscreen clip.
NEST_TS = "http://127.0.0.1:1984/api/stream.ts?src=doorbell"
NEST_JPEG = "http://127.0.0.1:1984/api/frame.jpeg?src=doorbell"
SNAP = Path.home() / "doorbell.jpg"
CLIP = Path.home() / "doorbell.mp4"
KODI_URL = "http://127.0.0.1:8080/jsonrpc"
KODI_USER = "kodi"
RECORD_SECONDS = 5
PIP_SECONDS = 12
WATCH_SECONDS = 45
CHIME = "sdm.devices.events.DoorbellChime.Chime"
log = logging.getLogger("doorbell")


def read(name: str) -> str:
    return (CONF / name).read_text().strip()


def token() -> str:
    data = urllib.parse.urlencode({
        "client_id": read("google-client-id"),
        "client_secret": read("google-client-secret"),
        "refresh_token": read("google-refresh-token"),
        "grant_type": "refresh_token",
    }).encode()
    with urllib.request.urlopen("https://oauth2.googleapis.com/token", data, timeout=30) as r:
        return json.load(r)["access_token"]


def google_post(url: str, body: dict, access: str) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {access}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=70) as r:
        return json.load(r)


def kodi(method: str, params: dict | None = None) -> dict:
    password = read("kodi-web-password")
    payload = {"jsonrpc": "2.0", "method": method, "id": 1}
    if params is not None:
        payload["params"] = params
    data = json.dumps(payload).encode()
    auth = base64.b64encode(f"{KODI_USER}:{password}".encode()).decode()
    req = urllib.request.Request(
        KODI_URL,
        data=data,
        headers={"Content-Type": "application/json", "Authorization": f"Basic {auth}"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def active_players() -> list[dict]:
    return kodi("Player.GetActivePlayers").get("result") or []


def something_is_playing() -> bool:
    for player in active_players():
        if player.get("type") in ("video", "audio"):
            speed = (
                kodi("Player.GetProperties", {"playerid": player["playerid"], "properties": ["speed"]})
                .get("result", {})
                .get("speed", 0)
            )
            if speed:
                return True
    return False


def notify(title: str, message: str, image: str | None = None, displaytime: int = 6000) -> None:
    params: dict = {"title": title, "message": message, "displaytime": displaytime}
    if image:
        params["image"] = image
    kodi("GUI.ShowNotification", params)


def wake_nest() -> None:
    """Battery doorbells sleep; a short TS read kicks WebRTC before we ask for a JPEG."""
    try:
        req = urllib.request.Request(NEST_TS, headers={"User-Agent": "homelab-doorbell/1"})
        with urllib.request.urlopen(req, timeout=40) as src:
            src.read(65536)
        log.info("nest stream awake")
    except Exception:
        log.exception("nest wake failed; trying jpeg anyway")


def fetch_snapshot(path: Path, attempts: int = 3) -> None:
    path.unlink(missing_ok=True)
    last_error: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            if attempt == 1:
                wake_nest()
            req = urllib.request.Request(NEST_JPEG, headers={"User-Agent": "homelab-doorbell/1"})
            # Battery Nest can take 30s+ to start WebRTC when cold, especially while Kodi is playing.
            with urllib.request.urlopen(req, timeout=55) as src, path.open("wb") as out:
                out.write(src.read())
            if path.stat().st_size < 1000:
                raise RuntimeError("snapshot too small")
            log.info("snapshot %s (%s bytes) attempt %s", path, path.stat().st_size, attempt)
            return
        except BaseException as e:
            last_error = e
            log.warning("snapshot attempt %s failed: %s", attempt, e)
            time.sleep(2)
    raise RuntimeError(f"snapshot failed after {attempts} tries") from last_error


def record_and_transcode(out: Path) -> None:
    out.unlink(missing_ok=True)
    ffmpeg = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", "pipe:0",
        "-an",
        "-r", "10",
        "-vf", "scale=-2:240:force_original_aspect_ratio=decrease,"
               "pad=426:240:(ow-iw)/2:(oh-ih)/2:black,setsar=1",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-profile:v", "baseline",
        "-preset", "ultrafast",
        "-tune", "zerolatency",
        "-crf", "35",
        "-movflags", "+faststart",
        str(out),
    ]
    curl = [
        "curl", "-fsS", "--max-time", str(RECORD_SECONDS + 5),
        "-H", "User-Agent: homelab-doorbell/1",
        NEST_TS,
    ]
    log.info("recording %ss and encoding to %s", RECORD_SECONDS, out)
    with subprocess.Popen(curl, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as src:
        proc = subprocess.run(
            ffmpeg,
            stdin=src.stdout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=90,
            check=False,
        )
    if not out.exists() or out.stat().st_size < 5000:
        raise RuntimeError(f"clip encode failed (ffmpeg exit {proc.returncode})")
    log.info("clip ready %s (%s bytes)", out, out.stat().st_size)


def wait_playback_end(timeout: float) -> None:
    deadline = time.time() + timeout
    time.sleep(1)
    while time.time() < deadline:
        if not active_players():
            return
        time.sleep(1)
    for player in active_players():
        kodi("Player.Stop", {"playerid": player["playerid"]})


def handle_chime_pip() -> None:
    """Keep current playback going; overlay a large doorbell photo notification."""
    log.info("doorbell chime (pip)")
    notify("Doorbell", "Getting camera…")
    try:
        fetch_snapshot(SNAP)
        # Long displaytime — Estuary shows the image in the notification card.
        notify("Doorbell", "Someone's at the door", image=str(SNAP), displaytime=PIP_SECONDS * 1000)
        time.sleep(PIP_SECONDS)
    except Exception:
        log.exception("pip doorbell failed")
        notify("Doorbell", "Someone's at the door — camera unavailable")


def handle_chime_fullscreen() -> None:
    """Nothing else on screen — snapshot, then a short clip."""
    log.info("doorbell chime (fullscreen)")
    notify("Doorbell", "Someone's at the door")

    clip_error: list[BaseException] = []

    def _make_clip():
        try:
            record_and_transcode(CLIP)
        except BaseException as e:
            clip_error.append(e)
            log.exception("clip failed")

    clip_thread = threading.Thread(target=_make_clip, daemon=True)
    clip_thread.start()

    try:
        fetch_snapshot(SNAP)
        log.info("showing snapshot")
        kodi("Player.Open", {"item": {"file": str(SNAP)}})
    except Exception:
        log.exception("snapshot failed")
        notify("Doorbell", "Getting camera…")

    clip_thread.join(timeout=90)
    if CLIP.exists() and CLIP.stat().st_size >= 5000 and not clip_error:
        log.info("showing clip")
        kodi("Player.Open", {"item": {"file": str(CLIP)}})
        wait_playback_end(WATCH_SECONDS)
    elif clip_error:
        notify("Doorbell", "Couldn't get video — showing photo only")
        wait_playback_end(15)
    else:
        wait_playback_end(15)


def handle_chime() -> None:
    if something_is_playing():
        handle_chime_pip()
    else:
        handle_chime_fullscreen()


def decode_event(message: dict) -> dict:
    return json.loads(base64.b64decode(message["message"]["data"]))


def is_chime(event: dict) -> bool:
    events = event.get("resourceUpdate", {}).get("events", {})
    return CHIME in events


def should_handle(event: dict) -> bool:
    return is_chime(event) and event.get("eventThreadState") != "ENDED"


def pull_once(access: str) -> list[dict]:
    sub = (
        f"https://pubsub.googleapis.com/v1/projects/{read('gcp-project-id')}"
        f"/subscriptions/nest-events-sub"
    )
    return google_post(
        sub + ":pull",
        {"maxMessages": 10, "returnImmediately": False},
        access,
    ).get("receivedMessages", [])


def ack(access: str, messages: list[dict]) -> None:
    if not messages:
        return
    sub = (
        f"https://pubsub.googleapis.com/v1/projects/{read('gcp-project-id')}"
        f"/subscriptions/nest-events-sub"
    )
    google_post(sub + ":acknowledge", {"ackIds": [m["ackId"] for m in messages]}, access)


def loop(stop: threading.Event) -> None:
    access = None
    access_expires = 0.0
    while not stop.is_set():
        try:
            if time.time() >= access_expires:
                access = token()
                access_expires = time.time() + 3000
            messages = pull_once(access)
            chime = any(should_handle(decode_event(m)) for m in messages)
            ack(access, messages)
            if chime:
                handle_chime()
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:300]
            log.error("http %s: %s", e.code, body)
            access_expires = 0
            time.sleep(5)
        except Exception:
            log.exception("doorbell loop error")
            access_expires = 0
            time.sleep(5)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    stop = threading.Event()

    def _stop(*_args):
        stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    log.info("listening for Nest doorbell events")
    loop(stop)


if __name__ == "__main__":
    main()
