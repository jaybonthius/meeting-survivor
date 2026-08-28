from __future__ import annotations

import unittest
from unittest import mock

from meeting_survivor import audio as audio_module
from meeting_survivor.audio import LiveAudio, sounddevice_device_identifier


class SoundDeviceIdentifierTests(unittest.TestCase):
    def test_numeric_string_device_ids_are_treated_as_sounddevice_indexes(self) -> None:
        self.assertEqual(sounddevice_device_identifier("6"), 6)
        self.assertEqual(sounddevice_device_identifier(" 7 "), 7)

    def test_named_devices_and_default_device_pass_through(self) -> None:
        self.assertEqual(sounddevice_device_identifier("BlackHole 2ch"), "BlackHole 2ch")
        self.assertEqual(sounddevice_device_identifier(2), 2)
        self.assertIsNone(sounddevice_device_identifier(None))

    def test_live_audio_normalizes_backend_string_ids_before_querying_sounddevice(self) -> None:
        calls = []

        def fake_query_devices(device=None, kind=None):
            calls.append((device, kind))
            return {"default_samplerate": 48000.0}

        with mock.patch.object(audio_module.sd, "query_devices", side_effect=fake_query_devices):
            live = LiveAudio(input_device="6", output_device="7", delay_ms=400)

        self.assertEqual(calls[:2], [(6, "input"), (7, "output")])
        self.assertEqual(live.input_device, 6)
        self.assertEqual(live.output_device, 7)


if __name__ == "__main__":
    unittest.main()
