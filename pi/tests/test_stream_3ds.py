import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "audio"))
import stream_3ds  # noqa: E402

HDMI = "PIPEWIRE:alsa_output.platform-3f902000.hdmi.hdmi-stereo|Built-in Audio Digital Stereo (HDMI)"
DEFAULT = "PIPEWIRE:Default|Default Output Device (PIPEWIRE)"
SINK = "PIPEWIRE:3ds|Nintendo 3DS"


class FakeKodi:
    def __init__(self, device=DEFAULT, players=(), file="/srv/media/Movies/a.mkv", offset=0.0):
        self.device = device
        self.players = list(players)
        self.file = file
        self.offset = offset
        self.calls = []

    def device_options(self):
        return [DEFAULT, HDMI, SINK]

    def call(self, method, **params):
        self.calls.append((method, params))
        if method == "Settings.GetSettingValue":
            return {"value": self.device}
        if method == "Settings.SetSettingValue":
            self.device = params["value"]
            return True
        if method == "Player.GetActivePlayers":
            return self.players
        if method == "Player.GetItem":
            return {"item": {"file": self.file}}
        if method == "Player.GetAudioDelay":
            return {"offset": self.offset}
        if method == "Player.SetAudioDelay":
            self.offset = params["offset"]
            return {"offset": self.offset}
        raise AssertionError(method)


class HelpersTest(unittest.TestCase):
    def test_sink_and_default_devices(self):
        self.assertEqual(stream_3ds.sink_device([DEFAULT, SINK]), SINK)
        self.assertIsNone(stream_3ds.sink_device([DEFAULT, HDMI]))
        self.assertEqual(stream_3ds.default_device([HDMI, DEFAULT]), DEFAULT)

    def test_offset_plays_sound_earlier_in_kodi_steps(self):
        self.assertEqual(stream_3ds.kodi_offset(250), -0.25)
        self.assertEqual(stream_3ds.kodi_offset(260), -0.25)
        self.assertEqual(stream_3ds.kodi_offset(0), 0.0)
        self.assertEqual(stream_3ds.kodi_offset(99999), -10.0)

    def test_parse_delay(self):
        self.assertEqual(stream_3ds.parse_delay("delay 250\n"), 250)
        self.assertIsNone(stream_3ds.parse_delay("delay x"))
        self.assertIsNone(stream_3ds.parse_delay("hello"))


class SessionTest(unittest.TestCase):
    def test_switches_to_3ds_and_back(self):
        kodi = FakeKodi()
        with tempfile.TemporaryDirectory() as tmp:
            state = stream_3ds.State(Path(tmp) / "dirty.json")
            server_side, client = socket.socketpair()
            seen = {}

            def handheld():
                client.sendall(b"delay 300\n")
                received = 0
                while received < stream_3ds.CHUNK * 10:
                    received += len(client.recv(65536))
                seen["device"] = kodi.device
                seen["offset"] = state.offset
                seen["streaming"] = state.streaming
                client.close()

            thread = threading.Thread(target=handheld)
            thread.start()
            stream_3ds.run_session(server_side, kodi, state, recorder=["cat", "/dev/zero"])
            thread.join(5)

        self.assertEqual(seen, {"device": SINK, "offset": -0.3, "streaming": True})
        self.assertEqual(kodi.device, DEFAULT)
        self.assertFalse(state.streaming)
        self.assertEqual(state.offset, 0.0)

    def test_gives_up_on_a_silent_3ds_even_if_the_recorder_ignores_sigterm(self):
        kodi = FakeKodi()
        timeout = stream_3ds.CLIENT_TIMEOUT
        stream_3ds.CLIENT_TIMEOUT = 0.5
        try:
            with tempfile.TemporaryDirectory() as tmp:
                state = stream_3ds.State(Path(tmp) / "dirty.json")
                server_side, client = socket.socketpair()
                client.sendall(b"delay 100\n")  # then never reads or writes again
                start = time.monotonic()
                stream_3ds.run_session(
                    server_side, kodi, state, recorder=["sh", "-c", "trap '' TERM; cat /dev/zero"]
                )
                elapsed = time.monotonic() - start
                client.close()
        finally:
            stream_3ds.CLIENT_TIMEOUT = timeout
        self.assertLess(elapsed, 4)
        self.assertEqual(kodi.device, DEFAULT)

    def test_restores_default_if_left_on_the_sink(self):
        kodi = FakeKodi(device=SINK)
        with tempfile.TemporaryDirectory() as tmp:
            state = stream_3ds.State(Path(tmp) / "dirty.json")
            server_side, client = socket.socketpair()
            client.close()
            stream_3ds.run_session(server_side, kodi, state, recorder=["cat", "/dev/zero"])
        self.assertEqual(kodi.device, DEFAULT)


class SyncOffsetTest(unittest.TestCase):
    def test_holds_offset_while_streaming_then_undoes_it(self):
        kodi = FakeKodi(players=[{"playerid": 1, "type": "video"}])
        with tempfile.TemporaryDirectory() as tmp:
            dirty_file = Path(tmp) / "dirty.json"
            state = stream_3ds.State(dirty_file)
            state.streaming, state.offset = True, -0.25

            stream_3ds.sync_offset(kodi, state)
            self.assertEqual(kodi.offset, -0.25)
            self.assertIn(kodi.file, stream_3ds.State(dirty_file).dirty)

            state.streaming, state.offset = False, 0.0
            stream_3ds.sync_offset(kodi, state)
            self.assertEqual(kodi.offset, 0.0)
            self.assertEqual(stream_3ds.State(dirty_file).dirty, set())

    def test_leaves_other_videos_alone_when_not_streaming(self):
        kodi = FakeKodi(players=[{"playerid": 1, "type": "video"}], offset=0.1)
        with tempfile.TemporaryDirectory() as tmp:
            stream_3ds.sync_offset(kodi, stream_3ds.State(Path(tmp) / "dirty.json"))
        self.assertEqual(kodi.offset, 0.1)
        self.assertNotIn("Player.SetAudioDelay", [c[0] for c in kodi.calls])

    def test_nothing_playing(self):
        kodi = FakeKodi()
        with tempfile.TemporaryDirectory() as tmp:
            stream_3ds.sync_offset(kodi, stream_3ds.State(Path(tmp) / "dirty.json"))
        self.assertEqual([c[0] for c in kodi.calls], ["Player.GetActivePlayers"])


if __name__ == "__main__":
    unittest.main()
