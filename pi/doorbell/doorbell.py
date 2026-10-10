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
# Pi can't play that live (no RTSP; HLS EOFs; portrait TS fails the DRM renderer),
# so we record a short clip through go2rtc and re-encode to a 16:9 baseline MP4.
NEST_TS = "http://127.0.0.1:1984/api/stream.ts?src=doorbell"
RAW = Path("/tmp/doorbell-raw.ts")
CLIP = Path.home() / "doorbell.mp4"
KODI_URL = "http://127.0.0.1:8080/jsonrpc"
KODI_USER = "kodi"
RECORD_SECONDS = 10
MIN_BYTES = 60_000
WATCH_SECONDS = 90
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


def notify(title: str, message: str) -> None:
    kodi("GUI.ShowNotification", {"title": title, "message": message, "displaytime": 8000})


def record_raw(path: Path) -> None:
    path.unlink(missing_ok=True)
    req = urllib.request.Request(NEST_TS, headers={"User-Agent": "homelab-doorbell/1"})
    with urllib.request.urlopen(req, timeout=30) as src, path.open("wb") as out:
        deadline = time.time() + RECORD_SECONDS
        while time.time() < deadline:
            chunk = src.read(65536)
            if not chunk:
                break
            out.write(chunk)
    size = path.stat().st_size if path.exists() else 0
    log.info("recorded raw %s (%s bytes)", path, size)
    if size < MIN_BYTES:
        raise RuntimeError(f"nest clip too small ({size} bytes)")


def transcode(raw: Path, out: Path) -> None:
    out.unlink(missing_ok=True)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(raw),
        "-an",
        "-vf", "scale=-2:480:force_original_aspect_ratio=decrease,"
               "pad=854:480:(ow-iw)/2:(oh-ih)/2:black,setsar=1",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-profile:v", "baseline",
        "-level", "3.0",
        "-preset", "ultrafast",
        "-movflags", "+faststart",
        str(out),
    ]
    subprocess.run(cmd, check=True, timeout=120, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log.info("transcoded %s (%s bytes)", out, out.stat().st_size)


def handle_chime() -> None:
    log.info("doorbell chime")
    notify("Doorbell", "Someone's at the door")
    paused = []
    for player in active_players():
        pid = player["playerid"]
        speed = (
            kodi("Player.GetProperties", {"playerid": pid, "properties": ["speed"]})
            .get("result", {})
            .get("speed", 0)
        )
        if speed:
            kodi("Player.PlayPause", {"playerid": pid, "play": False})
            paused.append(pid)
            log.info("paused player %s", pid)

    try:
        notify("Doorbell", "Getting camera…")
        record_raw(RAW)
        notify("Doorbell", "Preparing clip…")
        transcode(RAW, CLIP)
        log.info("opening %s", CLIP)
        kodi("Player.Open", {"item": {"file": str(CLIP)}})
        deadline = time.time() + WATCH_SECONDS
        while time.time() < deadline:
            time.sleep(2)
            if not active_players():
                break
        else:
            for player in active_players():
                kodi("Player.Stop", {"playerid": player["playerid"]})
    except Exception:
        log.exception("doorbell clip failed")
        notify("Doorbell", "Couldn't show the camera")
    finally:
        RAW.unlink(missing_ok=True)

    for pid in paused:
        try:
            kodi("Player.PlayPause", {"playerid": pid, "play": True})
            log.info("resumed player %s", pid)
        except Exception:
            log.exception("failed to resume player %s", pid)


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
