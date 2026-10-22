"""Offline tests for the descriptor normalization helpers."""

import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

_COMPONENT_DIR = (
    Path(__file__).resolve().parents[1]
    / "custom_components" / "ezviz_sd_card_recordings"
)


def _load_module(name: str):
    """Load a module from the integration folder without requiring HA imports.

    The two modules exercised here (``recordings`` and ``const``) only use
    the Python standard library, so they can be loaded directly by path and
    do not need a running Home Assistant instance in the CI sandbox.
    """
    path = _COMPONENT_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"ezviz_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_HELPERS = _load_module("recordings")
direct_https_url = _HELPERS.direct_https_url
format_native_playback_timestamp = _HELPERS.format_native_playback_timestamp
resolve_api_serial = _HELPERS.resolve_api_serial
format_record_title = _HELPERS.format_record_title
normalize_records = _HELPERS.normalize_records
record_key = _HELPERS.record_key
serial_identity = _HELPERS.serial_identity

_CONST = _load_module("const")


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
        # v0.4+ format: HHMMSS_HHMMSS_<hash8> — 22 characters, not the
        # previous 20-character opaque hash. Assert the shape rather than
        # the length so future tweaks to the timestamp prefix are visible
        # in review.
        self.assertRegex(items[0]["key"], r"^\d{6}_\d{6}_[0-9a-f]{8}$")

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
        # v0.4+ signature: record_key(raw, start, end). The hash8 component
        # is derived from a stable descriptor representation, so dict order
        # and unrelated fields such as downloadUrl must not change the key.
        start = datetime(2026, 10, 16, 4, 12, 3, tzinfo=timezone.utc)
        end = datetime(2026, 10, 16, 4, 13, 3, tzinfo=timezone.utc)
        self.assertEqual(
            record_key({"seqId": "abc", "startTime": 123}, start, end),
            record_key({"startTime": 123, "seqId": "abc"}, start, end),
        )
        self.assertEqual(
            record_key({"seqId": "abc", "downloadUrl": "https://one.example/a"}, start, end),
            record_key({"seqId": "abc", "downloadUrl": "https://two.example/b"}, start, end),
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


class ConstantsTests(unittest.TestCase):
    def test_sync_notify_defaults_to_on(self):
        """v0.6+ preserves previous behaviour: users who never touched the
        toggle keep receiving the sync summary after each 15-minute cycle."""
        self.assertEqual(_CONST.CONF_SYNC_NOTIFY, "sync_notify")
        self.assertEqual(_CONST.DEFAULT_SYNC_NOTIFY, "on")


if __name__ == "__main__":
    unittest.main()
