#!/usr/bin/env python3
"""Streams Kodi's audio to the 3DS remote app.

While a 3DS is connected, Kodi plays into the "3ds" PipeWire sink and that sink is sent to the 3DS as raw
16-bit stereo PCM. The 3DS sends "delay <ms>" about once a second: how late its sound arrives. Kodi's audio
offset for the playing video is kept at minus that, so the sound lines up with the picture on the TV.
When the 3DS disconnects or goes quiet, Kodi goes back to its previous output.
"""
import base64
import json
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

PORT = 8091
RATE = 32000
CHUNK = RATE * 2 * 2 // 50  # 20 ms
CLIENT_TIMEOUT = 3
SINK = "3ds"
DEVICE_SETTING = "audiooutput.audiodevice"
CONFIG = Path.home() / ".config" / "homelab"
RECORDER = [
    "pw-record", "--target", SINK, "-P", "{ stream.capture.sink=true }", "--rate", str(RATE),
    "--channels", "2", "--format", "s16", "--latency", "20ms", "--raw", "-",
]


class KodiError(Exception):
    pass


class Kodi:
    def __init__(self, url="http://127.0.0.1:8080/jsonrpc", password_file=CONFIG / "kodi-web-password"):
        self.url = url
        self.password_file = password_file

    def call(self, method, **params):
        auth = base64.b64encode(f"kodi:{self.password_file.read_text().strip()}".encode()).decode()
        request = urllib.request.Request(
            self.url,
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
            {"Content-Type": "application/json", "Authorization": f"Basic {auth}"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            body = json.load(response)
        if "error" in body:
            raise KodiError(f"{method}: {body['error'].get('message')}")
        return body["result"]

    def device_options(self):
        settings = self.call("Settings.GetSettings", filter={"section": "system", "category": "audio"})["settings"]
        setting = next(s for s in settings if s["id"] == DEVICE_SETTING)
        return [option["value"] for option in setting.get("options", [])]


def sink_device(options):
    return next((o for o in options if o.startswith(f"PIPEWIRE:{SINK}|")), None)


def default_device(options):
    return next((o for o in options if o.startswith("PIPEWIRE:Default|")), "Default")


def kodi_offset(delay_ms):
    """Kodi offsets are seconds in steps of 0.025 within +/-10; negative plays the sound earlier."""
    steps = max(-400, min(400, round(delay_ms / 25)))
    return round(-steps * 0.025, 3) + 0.0


def parse_delay(line):
    parts = line.split()
    if len(parts) == 2 and parts[0] == "delay":
        try:
            return int(parts[1])
        except ValueError:
            pass
    return None


class State:
    """What the session thread and the offset keeper share."""

    def __init__(self, dirty_file=CONFIG / "3ds-offset-files.json"):
        self.lock = threading.Lock()
        self.streaming = False
        self.offset = 0.0
        self.dirty_file = dirty_file
        try:
            self.dirty = set(json.loads(dirty_file.read_text()))
        except (OSError, ValueError):
            self.dirty = set()

    def save_dirty(self):
        self.dirty_file.parent.mkdir(parents=True, exist_ok=True)
        self.dirty_file.write_text(json.dumps(sorted(self.dirty)))


def sync_offset(kodi, state):
    """While streaming, holds the playing video at the 3DS offset. Otherwise undoes offsets we left behind,
    since Kodi remembers them per video."""
    players = kodi.call("Player.GetActivePlayers")
    player = next((p for p in players if p.get("type") == "video"), None)
    if not player:
        return
    playerid = player["playerid"]
    file = kodi.call("Player.GetItem", playerid=playerid, properties=["file"])["item"].get("file", "")
    current = kodi.call("Player.GetAudioDelay")["offset"]

    with state.lock:
        streaming, wanted = state.streaming, state.offset
        if streaming and wanted != 0 and file not in state.dirty:
            state.dirty.add(file)
            state.save_dirty()
        elif not streaming and file in state.dirty:
            state.dirty.discard(file)
            state.save_dirty()
        elif not streaming:
            return

    target = wanted if streaming else 0.0
    if abs(current - target) > 0.001:
        kodi.call("Player.SetAudioDelay", playerid=playerid, offset=target)


def keep_offsets(kodi, state):
    while True:
        try:
            sync_offset(kodi, state)
        except (OSError, KodiError, KeyError) as e:
            print(f"offset sync: {e}")
        time.sleep(1)


def read_messages(client, state, closed):
    try:
        for line in client.makefile("r", encoding="ascii", errors="replace"):
            delay = parse_delay(line)
            if delay is not None:
                with state.lock:
                    state.offset = kodi_offset(delay)
    except OSError:
        pass
    closed.set()


def run_session(client, kodi, state, recorder=RECORDER):
    client.settimeout(CLIENT_TIMEOUT)
    options = kodi.device_options()
    sink = sink_device(options)
    if not sink:
        print("Kodi doesn't list the 3ds sink; is the PipeWire config installed?")
        client.close()
        return
    previous = kodi.call("Settings.GetSettingValue", setting=DEVICE_SETTING)["value"]
    if previous == sink:
        previous = default_device(options)

    kodi.call("Settings.SetSettingValue", setting=DEVICE_SETTING, value=sink)
    with state.lock:
        state.streaming = True
    closed = threading.Event()
    threading.Thread(target=read_messages, args=(client, state, closed), daemon=True).start()
    process = subprocess.Popen(recorder, stdout=subprocess.PIPE)
    try:
        while not closed.is_set():
            data = process.stdout.read(CHUNK)
            if not data:
                break
            client.sendall(data)
    except OSError:
        pass
    finally:
        process.terminate()
        process.wait()
        client.close()
        with state.lock:
            state.streaming = False
            state.offset = 0.0
        kodi.call("Settings.SetSettingValue", setting=DEVICE_SETTING, value=previous)


def main():
    kodi = Kodi()
    state = State()
    try:
        options = kodi.device_options()
        if kodi.call("Settings.GetSettingValue", setting=DEVICE_SETTING)["value"] == sink_device(options):
            kodi.call("Settings.SetSettingValue", setting=DEVICE_SETTING, value=default_device(options))
    except (OSError, KodiError, StopIteration) as e:
        print(f"startup check: {e}")
    threading.Thread(target=keep_offsets, args=(kodi, state), daemon=True).start()

    server = socket.create_server(("", PORT))
    while True:
        client, address = server.accept()
        print(f"3DS connected from {address[0]}")
        try:
            run_session(client, kodi, state)
        except (OSError, KodiError, StopIteration) as e:
            print(f"session: {e}")
        print("3DS disconnected")


if __name__ == "__main__":
    main()
