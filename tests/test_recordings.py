"""Offline tests for the descriptor normalization helpers."""

import unittest
import importlib.util
from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

_HELPER_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components" / "ezviz_sd_card_recordings" / "recordings.py"
)
_SPEC = importlib.util.spec_from_file_location("ezviz_recordings_helpers", _HELPER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_HELPERS = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_HELPERS)
direct_https_url = _HELPERS.direct_https_url
format_native_playback_timestamp = _HELPERS.format_native_playback_timestamp
resolve_api_serial = _HELPERS.resolve_api_serial
format_record_title = _HELPERS.format_record_title
normalize_records = _HELPERS.normalize_records
record_key = _HELPERS.record_key
serial_identity = _HELPERS.serial_identity


class RecordingNormalizationTests(unittest.TestCase):
    def test_normalizes_epoch_milliseconds_and_sorts(self):
        payload = {
            "records": [
                {"startTime": 1792109100000, "endTime": 1792109160000, "seqId": "b"},
                {"startTime": 1792105500000, "endTime": 1792105560000, "seqId": "a"},
            ]
        }
        items = normalize_records(payload)
        self.assertEqual(len(items), 2)
        self.assertLessEqual(items[0]["start"], items[1]["start"])
        self.assertEqual(len(items[0]["key"]), 20)

    def test_iso_time_and_missing_end(self):
        items = normalize_records([{"startTime": "2026-10-16T04:12:03+07:00"}], local_timezone=timezone(timedelta(hours=7)))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["start"], items[0]["end"])
        self.assertIn("04:12:03", format_record_title(items[0]))

    def test_skips_non_record_dicts_and_invalid_ranges(self):
        items = normalize_records({"meta": {"code": 200}, "rows": [
            {"startTime": "not a time", "endTime": "also bad"},
            {"startTime": "2026-10-16T05:00:00+07:00", "endTime": "2026-10-16T04:00:00+07:00"},
        ]})
        self.assertEqual(items, [])

    def test_key_is_stable(self):
        raw = {"seqId": "abc", "startTime": 123}
        self.assertEqual(record_key(raw), record_key({"startTime": 123, "seqId": "abc"}))
        self.assertEqual(
            record_key({"seqId": "abc", "downloadUrl": "https://one.example/a"}),
            record_key({"seqId": "abc", "downloadUrl": "https://two.example/b"}),
        )

    def test_compact_timestamp(self):
        items = normalize_records([{"startTime": "20261016041203"}], local_timezone=timezone(timedelta(hours=7)))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["start"].strftime("%Y-%m-%d %H:%M:%S"), "2026-10-16 04:12:03")

    def test_resolves_full_sticker_serial_to_short_api_serial(self):
        full = "CS-H8c-R100-1K2WKFL0120250807CCRRBG4091592"
        self.assertEqual(resolve_api_serial(full, ["BK8998654", "BG4091592", "BC8610065"]), "BG4091592")
        self.assertEqual(resolve_api_serial("BC8610065", ["BK8998654", "BC8610065"]), "BC8610065")

    def test_serial_identity_deduplicates_full_and_short_serials(self):
        full = "CS-C6N-R101-1G2WF0120240506CCRRBC8610065"
        self.assertEqual(serial_identity(full), "BC8610065")
        self.assertEqual(serial_identity("bc8610065"), "BC8610065")
        self.assertEqual(
            serial_identity("CS-H8c-R100-1K2WKFL0120250807CCRRBG4091592"),
            "BG4091592",
        )

    def test_unmatched_serial_is_actionable(self):
        with self.assertRaisesRegex(ValueError, "does not match"):
            resolve_api_serial("UNKNOWN123", ["BK8998654", "BG4091592"])

    def test_native_sdcard_record_descriptor_normalizes(self):
        items = normalize_records({"records": [{
            "channelType": "D",
            "begin": "2026-10-15T08:07:31",
            "end": "2026-10-15T08:08:15",
            "type": "ALARM",
        }]}, local_timezone=timezone(timedelta(hours=7)))
        self.assertEqual(len(items), 1)
        self.assertIn("08:07:31", format_record_title(items[0]))

    def test_native_playback_timestamp_preserves_api_wall_clock(self):
        jakarta = ZoneInfo("Asia/Jakarta")
        utc_instant = datetime(2026, 10, 9, 2, 6, 23, tzinfo=timezone.utc)
        self.assertEqual(
            format_native_playback_timestamp(utc_instant, jakarta),
            "20261009T090623Z",
        )
        local_instant = datetime(2026, 10, 9, 9, 6, 23, tzinfo=jakarta)
        self.assertEqual(
            format_native_playback_timestamp(local_instant, jakarta),
            "20261009T090623Z",
        )

    def test_only_direct_https_urls_are_accepted(self):
        self.assertEqual(
            direct_https_url({"detail": {"downloadUrl": "https://media.example/clip.mp4"}}),
            "https://media.example/clip.mp4",
        )
        self.assertIsNone(direct_https_url({"playUrl": "ezopen://ABC/1.rec?begin=1"}))
        self.assertIsNone(direct_https_url({"streamUrl": "https://native.example:8554"}))
        self.assertIsNone(direct_https_url({"thumbnailUrl": "https://media.example/preview.jpg"}))


if __name__ == "__main__":
    unittest.main()
