#!/usr/bin/env python3
"""Pull Nest doorbell events and show them on Kodi."""
from __future__ import annotations

import base64
import json
import logging
import shutil
import signal
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CONF = Path.home() / ".config" / "homelab"
# While a movie is playing we must NOT start Nest WebRTC via go2rtc — that starves
# Kodi on this Pi 3 and makes the video buffer. Use CameraEventImage instead (a still
# from the chime event, downloaded over HTTPS, no live stream).
SNAP = Path.home() / "doorbell.jpg"
# Permanent copies for the Kodi "Doorbell" pictures tab (Ubuntu media over sshfs).
ARCHIVE = Path("/mnt/storage/Doorbell")
KODI_URL = "http://127.0.0.1:8080/jsonrpc"
KODI_USER = "kodi"
PIP_SECONDS = 12
FULLSCREEN_SECONDS = 20
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
    with urllib.request.urlopen(req, timeout=30) as r:
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


def chime_event_id(event: dict) -> str | None:
    chime = event.get("resourceUpdate", {}).get("events", {}).get(CHIME) or {}
    return chime.get("eventId") or None


def archive_path(when: float | None = None) -> Path:
    """Filename for a kept doorbell still: 2026-10-10_212530.jpg"""
    stamp = time.strftime("%Y-%m-%d_%H%M%S", time.localtime(when or time.time()))
    dest = ARCHIVE / f"{stamp}.jpg"
    n = 1
    while dest.exists():
        dest = ARCHIVE / f"{stamp}_{n}.jpg"
        n += 1
    return dest


def archive_snap(snap: Path) -> Path | None:
    """Copy the latest still into /mnt/storage/Doorbell for the Kodi tab."""
    try:
        ARCHIVE.mkdir(parents=True, exist_ok=True)
        dest = archive_path()
        shutil.copy2(snap, dest)
        log.info("archived doorbell photo %s", dest)
        return dest
    except Exception:
        log.exception("could not archive doorbell photo to %s", ARCHIVE)
        return None


def fetch_event_image(path: Path, event_id: str, access: str) -> None:
    """Download the Nest event still. No WebRTC / go2rtc — safe while Kodi is playing."""
    path.unlink(missing_ok=True)
    device = read("nest-device-id")
    project = read("sdm-project-id")
    url = (
        f"https://smartdevicemanagement.googleapis.com/v1/enterprises/"
        f"{project}/devices/{device}:executeCommand"
    )
    result = google_post(
        url,
        {
            "command": "sdm.devices.commands.CameraEventImage.GenerateImage",
            "params": {"eventId": event_id},
        },
        access,
    )["results"]
    img_url = result["url"]
    img_token = result["token"]
    # Nest expects Basic auth with an empty username and the token as the password.
    basic = base64.b64encode(f":{img_token}".encode()).decode()
    req = urllib.request.Request(img_url, headers={"Authorization": f"Basic {basic}"})
    with urllib.request.urlopen(req, timeout=20) as src, path.open("wb") as out:
        out.write(src.read())
    if path.stat().st_size < 1000:
        raise RuntimeError("event image too small")
    log.info("event image %s (%s bytes) eventId=%s", path, path.stat().st_size, event_id)
    archive_snap(path)


def handle_chime_pip(event_id: str | None, access: str) -> None:
    """Keep current playback going; overlay a doorbell photo (no live camera)."""
    log.info("doorbell chime (pip)")
    if not event_id:
        notify("Doorbell", "Someone's at the door")
        return
    try:
        fetch_event_image(SNAP, event_id, access)
        notify("Doorbell", "Someone's at the door", image=str(SNAP), displaytime=PIP_SECONDS * 1000)
        time.sleep(PIP_SECONDS)
    except Exception:
        log.exception("pip doorbell failed")
        # Never fall back to go2rtc while playing — that causes buffering.
        notify("Doorbell", "Someone's at the door")


def handle_chime_fullscreen(event_id: str | None, access: str) -> None:
    """Nothing else on screen — show the event still fullscreen briefly."""
    log.info("doorbell chime (fullscreen)")
    notify("Doorbell", "Someone's at the door")
    if not event_id:
        return
    try:
        fetch_event_image(SNAP, event_id, access)
        log.info("showing event image")
        kodi("Player.Open", {"item": {"file": str(SNAP)}})
        deadline = time.time() + FULLSCREEN_SECONDS
        time.sleep(1)
        while time.time() < deadline and active_players():
            time.sleep(1)
        for player in active_players():
            item = kodi("Player.GetItem", {"playerid": player["playerid"]}).get("result", {}).get("item", {})
            if "doorbell" in (item.get("file") or item.get("label") or ""):
                kodi("Player.Stop", {"playerid": player["playerid"]})
    except Exception:
        log.exception("fullscreen doorbell failed")
        notify("Doorbell", "Someone's at the door — camera unavailable")


def handle_chime(event_id: str | None, access: str) -> None:
    if something_is_playing():
        handle_chime_pip(event_id, access)
    else:
        handle_chime_fullscreen(event_id, access)


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
            event_id = None
            chime = False
            for message in messages:
                event = decode_event(message)
                if should_handle(event):
                    chime = True
                    event_id = chime_event_id(event) or event_id
            ack(access, messages)
            if chime:
                handle_chime(event_id, access)
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
