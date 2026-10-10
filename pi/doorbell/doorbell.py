#!/usr/bin/env python3
"""Pull Nest doorbell events and show them on Kodi."""
from __future__ import annotations

import base64
import json
import logging
import signal
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CONF = Path.home() / ".config" / "homelab"
# Kodi from Debian has no RTSP client. HLS returns a playlist immediately (so Kodi's
# HEAD/stat succeeds), while go2rtc pulls Nest WebRTC in the background.
STREAM_URL = "http://127.0.0.1:1984/api/stream.m3u8?src=doorbell"
WARM_URL = "http://127.0.0.1:1984/api/stream.ts?src=doorbell"
KODI_URL = "http://127.0.0.1:8080/jsonrpc"
KODI_USER = "kodi"
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
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def active_players() -> list[dict]:
    return kodi("Player.GetActivePlayers").get("result") or []


def notify(title: str, message: str) -> None:
    kodi("GUI.ShowNotification", {"title": title, "message": message, "displaytime": 8000})


def warm_stream() -> None:
    """Kick Nest awake before Kodi opens the playlist."""
    try:
        req = urllib.request.Request(WARM_URL)
        with urllib.request.urlopen(req, timeout=25) as r:
            r.read(65536)
        log.info("nest stream warmed")
    except Exception:
        log.exception("stream warm failed; Kodi will still try to open")


def is_doorbell_item(item: dict) -> bool:
    path = item.get("file") or item.get("path") or ""
    return "1984/api/stream" in path or path.endswith("doorbell")


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

    warm_stream()
    kodi("Player.Open", {"item": {"file": STREAM_URL}})
    deadline = time.time() + WATCH_SECONDS
    while time.time() < deadline:
        time.sleep(2)
        playing = False
        for player in active_players():
            item = kodi("Player.GetItem", {"playerid": player["playerid"]}).get("result", {}).get("item", {})
            if is_doorbell_item(item):
                playing = True
                break
        if not playing:
            break
    else:
        for player in active_players():
            item = kodi("Player.GetItem", {"playerid": player["playerid"]}).get("result", {}).get("item", {})
            if is_doorbell_item(item):
                kodi("Player.Stop", {"playerid": player["playerid"]})
                log.info("stopped doorbell stream after timeout")

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
    # STARTED and a lone event with no thread state; skip ENDED so we don't fire twice.
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
