import base64
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "doorbell"))
import doorbell  # noqa: E402


def event(kinds=None, state="STARTED", traits=None):
    update = {"name": "enterprises/x/devices/y"}
    if kinds:
        update["events"] = {k: {"eventSessionId": "1", "eventId": "n:1"} for k in kinds}
    if traits:
        update["traits"] = traits
    body = {
        "eventId": "abc",
        "timestamp": "2026-10-10T21:11:10.291920Z",
        "resourceUpdate": update,
        "eventThreadState": state,
    }
    return {
        "ackId": "1",
        "message": {"data": base64.b64encode(json.dumps(body).encode()).decode()},
    }


class DoorbellEventTest(unittest.TestCase):
    def test_chime_started(self):
        msg = event([doorbell.CHIME], "STARTED")
        self.assertTrue(doorbell.is_chime(doorbell.decode_event(msg)))

    def test_chime_ended_is_ignored(self):
        msg = event([doorbell.CHIME], "ENDED")
        decoded = doorbell.decode_event(msg)
        self.assertTrue(doorbell.is_chime(decoded))
        self.assertFalse(doorbell.should_handle(decoded))

    def test_info_update_is_not_chime(self):
        msg = event(traits={"sdm.devices.traits.Info": {"customName": "Front door doorbell"}})
        self.assertFalse(doorbell.is_chime(doorbell.decode_event(msg)))
        self.assertFalse(doorbell.should_handle(doorbell.decode_event(msg)))

    def test_chime_event_id(self):
        msg = event([doorbell.CHIME], "STARTED")
        self.assertEqual(doorbell.chime_event_id(doorbell.decode_event(msg)), "n:1")

    def test_archive_path_uses_timestamp(self):
        path = doorbell.archive_path(when=0)  # 1969/1970 local — just check shape
        self.assertTrue(path.name.endswith(".jpg"))
        self.assertEqual(path.parent, doorbell.ARCHIVE)


if __name__ == "__main__":
    unittest.main()
