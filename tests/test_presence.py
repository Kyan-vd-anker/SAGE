import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sage import config, presence, voice


class BlePresenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patches = [
            patch.object(config, "DB_PATH", Path(self.temp.name) / "sage.db"),
            patch.object(config, "BLE_PRESENCE_ENABLED", True),
            patch.object(config, "BLE_NODE_ROOMS", {
                "bedroom-speaker": "bedroom",
                "engineering-c3": "engineering",
            }),
            patch.object(config, "BLE_NEAR_RSSI", -65),
            patch.object(config, "BLE_AWAY_SECONDS", 600),
            patch.object(config, "BLE_NODE_TIMEOUT_SECONDS", 90),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def test_where_reports_the_room_with_the_strongest_signal(self):
        presence.report("bedroom-speaker", -80, now=1000)
        presence.report("engineering-c3", -55, now=1000)
        self.assertEqual(presence.where(now=1010)["room"], "engineering")
        presence.report("bedroom-speaker", -50, now=1020)
        result = presence.where(now=1025)
        self.assertEqual((result["state"], result["room"]), ("room", "bedroom"))

    def test_where_tells_away_from_unknown(self):
        presence.report("bedroom-speaker", None, now=1000)
        presence.report("engineering-c3", None, now=1000)
        self.assertEqual(presence.where(now=1010), {"state": "away", "room": None})
        self.assertEqual(presence.where(now=1500)["state"], "unknown")  # the receivers stopped reporting
        with patch.object(config, "BLE_PRESENCE_ENABLED", False):
            self.assertEqual(presence.where()["state"], "unknown")

    def test_phone_copy_of_a_reminder_is_skipped_only_while_you_are_in_the_speaker_room(self):
        from sage import telegram
        in_room = {"state": "room", "room": "bedroom"}
        with patch.object(config, "SPEAKER_ROOM", "bedroom"), patch.object(voice, "_asleep", False), patch.object(voice, "online", return_value=True):
            with patch.object(presence, "where", return_value=in_room):
                self.assertTrue(telegram._heard_on_speaker("reminders"))
                self.assertFalse(telegram._heard_on_speaker("tasks"))
            with patch.object(presence, "where", return_value={"state": "away", "room": None}):
                self.assertFalse(telegram._heard_on_speaker("reminders"))
            with patch.object(presence, "where", return_value={"state": "room", "room": "engineering"}):
                self.assertFalse(telegram._heard_on_speaker("reminders"))
        with patch.object(voice, "_asleep", True), patch.object(voice, "online", return_value=True), patch.object(presence, "where", return_value=in_room):
            self.assertFalse(telegram._heard_on_speaker("reminders"))

    def test_requires_beacon_to_be_lost_for_ten_minutes_and_nodes_online(self):
        presence.report("bedroom-speaker", -80, now=1000)
        presence.report("engineering-c3", -80, now=1000)
        for timestamp in range(1060, 1600, 60):
            presence.report("bedroom-speaker", None, now=timestamp)
            presence.report("engineering-c3", None, now=timestamp)

        result = presence.report("bedroom-speaker", -65, now=1600)

        self.assertTrue(result["nodes_online"])
        self.assertTrue(result["near"])
        self.assertEqual(result["away_seconds"], 600)
        self.assertTrue(result["greet"])
        self.assertFalse(presence.report("bedroom-speaker", -60, now=1601)["greet"])

    def test_does_not_greet_on_first_sighting_or_before_timeout(self):
        presence.report("bedroom-speaker", -80, now=1000)
        presence.report("engineering-c3", -80, now=1000)
        first = presence.report("bedroom-speaker", -60, now=1001)
        self.assertFalse(first["greet"])
        for timestamp in range(1060, 1600, 60):
            presence.report("bedroom-speaker", None, now=timestamp)
            presence.report("engineering-c3", None, now=timestamp)
        result = presence.report("bedroom-speaker", -60, now=1599)
        self.assertFalse(result["greet"])
        self.assertEqual(result["away_seconds"], 598)

    def test_ignores_offline_room_receivers(self):
        presence.report("bedroom-speaker", -80, now=1000)
        presence.report("engineering-c3", -80, now=1000)
        presence.report("bedroom-speaker", None, now=1600)

        result = presence.report("bedroom-speaker", -60, now=1601)
        self.assertFalse(result["nodes_online"])
        self.assertFalse(result["greet"])

    def test_scanner_outage_restarts_the_continuous_absence_window(self):
        presence.report("bedroom-speaker", -80, now=1000)
        presence.report("engineering-c3", -80, now=1000)
        for timestamp in range(1060, 1600, 60):
            presence.report("bedroom-speaker", None, now=timestamp)
            presence.report("engineering-c3", None, now=timestamp)

        recovered = presence.report("engineering-c3", None, now=1700)
        self.assertFalse(recovered["nodes_online"])
        presence.report("bedroom-speaker", None, now=1700)
        result = presence.report("bedroom-speaker", -60, now=1701)
        self.assertFalse(result["greet"])
        self.assertEqual(result["away_seconds"], 1)

    def test_rejects_unknown_nodes_and_invalid_rssi(self):
        with self.assertRaises(ValueError):
            presence.report("unknown", -50, now=1000)
        with self.assertRaises(ValueError):
            presence.report("bedroom-speaker", -128, now=1000)

    def test_presence_route_requires_explicit_rssi_field(self):
        body = json.dumps({"node": "bedroom-speaker"}).encode()
        handler = SimpleNamespace(
            _read_body=lambda: body,
            path="/api/voice/presence",
            _authorize=lambda received: True,
            client_address=("127.0.0.1", 1),
            _json=lambda value, status=200: (value, status),
        )
        self.assertEqual(
            voice.Handler.do_POST(handler),
            ({"error": "node and rssi are required"}, 400),
        )

    def test_qualified_arrival_queues_the_requested_greeting(self):
        result = {
            "accepted": True, "room": "bedroom", "near": True,
            "nodes_online": True, "away_seconds": 600, "greet": True,
        }
        body = json.dumps({"node": "bedroom-speaker", "rssi": -60}).encode()
        handler = SimpleNamespace(
            _read_body=lambda: body,
            path="/api/voice/presence",
            _authorize=lambda received: True,
            client_address=("192.168.1.50", 1),
            _json=lambda value, status=200: (value, status),
        )
        with patch("sage.presence.report", return_value=result), \
                patch("sage.voice.announce") as announce, \
                patch("sage.voice.audit"):
            self.assertEqual(voice.Handler.do_POST(handler), (result, 200))
        announce.assert_called_once_with("Hello sir.", "presence", lang="en", ttl=60)


if __name__ == "__main__":
    unittest.main()
