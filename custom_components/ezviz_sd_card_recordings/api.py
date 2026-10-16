"""pyEzvizApi client adapter and descriptor cache."""

from __future__ import annotations

import asyncio
import json
import os
import inspect
import re
import shutil
import subprocess
import tempfile
from datetime import date, datetime, time, timedelta
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_CHANNEL,
    CONF_REGION,
    CONF_SERIAL,
    CONF_SESSION_TOKEN,
    CONF_USERNAME,
    CONF_PASSWORD,
    DOMAIN,
    LOGGER,
)
from .recordings import (
    format_native_playback_timestamp,
    normalize_records,
    resolve_api_serial,
)


class EzvizArchiveApi:
    """Query EZVIZ SD-card record descriptors through pyEzvizApi."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.serial = str(entry.data[CONF_SERIAL])
        self.channel = int(entry.data.get(CONF_CHANNEL, 1))
        self.region = str(entry.data.get(CONF_REGION, "apiieu.ezvizlife.com"))
        self.username = str(entry.data[CONF_USERNAME])
        self.password = str(entry.data[CONF_PASSWORD])
        self._lock = asyncio.Lock()
        self._cache: dict[date, tuple[datetime, list[dict[str, Any]]]] = {}
        self._cache_ttl = timedelta(minutes=5)
        self.token_path = Path(hass.config.path(".storage", DOMAIN, entry.entry_id, "token.json"))

    async def async_prepare(self) -> None:
        """Write the initial token securely; don't perform a cloud scan on startup."""
        await self.hass.async_add_executor_job(self._ensure_token_file)

    def _ensure_token_file(self) -> None:
        self.token_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.token_path.parent, 0o700)
        except OSError:
            pass
        legacy_token_path = Path(self.hass.config.path(".storage", DOMAIN, f"{self.entry.entry_id}.json"))
        if not self.token_path.exists() and legacy_token_path.is_file():
            try:
                os.replace(legacy_token_path, self.token_path)
                os.chmod(self.token_path, 0o600)
            except OSError:
                LOGGER.debug("Could not migrate legacy EZVIZ token file; will use config entry token")
        if not self.token_path.exists():
            token = self.entry.data.get(CONF_SESSION_TOKEN)
            if not isinstance(token, dict):
                raise RuntimeError("No EZVIZ session token is available; reconfigure the integration")
            tmp = self.token_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(token), encoding="utf-8")
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, self.token_path)

    async def async_records_for_day(self, selected_day: date) -> list[dict[str, Any]]:
        """Return cached SD-card descriptors or query EZVIZ for one local calendar day."""
        async with self._lock:
            cached = self._cache.get(selected_day)
            now = datetime.now().astimezone()
            if cached and now - cached[0] < self._cache_ttl:
                return cached[1]
            records = await self.hass.async_add_executor_job(self._query_day, selected_day)
            self._cache[selected_day] = (now, records)
            return records

    def _query_day(self, selected_day: date) -> list[dict[str, Any]]:
        """Query the legacy recording endpoint using the authenticated Python client.

        The CLI's ``sdcard_videos`` path tried endpoints that return HTTP 404 in
        some EZVIZ regions. The legacy ``search_records`` method is confirmed to
        work for several cameras when ``channel_serial`` uses the short API
        serial (for example ``BG4091592``), not necessarily the full sticker SN.
        """
        self._ensure_token_file()
        from pyezvizapi.client import EzvizClient

        try:
            token = json.loads(self.token_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as err:
            raise RuntimeError("Could not read the stored EZVIZ session token; reconfigure the integration") from err

        client = None
        try:
            client = EzvizClient(token=token, url=self.region)
            device_records = client.get_device_records()
            api_serial = resolve_api_serial(self.serial, device_records.keys())

            # EZVIZ expects wall-clock timestamps for the camera/API region;
            # offset-free local date-times match the working pyEzvizApi call.
            day_start = datetime.combine(selected_day, time.min)
            day_stop = datetime.combine(selected_day + timedelta(days=1), time.min)
            # The legacy endpoint accepts a result ``size`` but exposes no
            # documented offset/page parameter. A full batch of 100 entries is
            # therefore treated as potentially truncated, and that time window
            # is queried again in smaller pieces. This is important for busy
            # cameras: a single 24-hour query can otherwise stop in the middle
            # of the day and silently hide evening recordings.
            query_size = 100
            max_query_calls = 256
            max_split_depth = 16
            # DIAGNOSTIC HOTFIX: query fixed 6-hour windows even if the full-day
            # response contains fewer than 100 items. The previous hotfix assumed
            # truncation only happens when the endpoint returns exactly 100 records;
            # this assumption is not proven and could miss silently-truncated results.
            build_marker = "fixed-6h-diagnostic-v2"
            stats = {
                "calls": 0,
                "raw_descriptors": 0,
                "splits": 0,
                "saturated_windows": 0,
                "budget_exhausted": False,
                "base_windows": 0,
            }
            extractor = getattr(client, "extract_record_list", None)
            try:
                local_timezone = ZoneInfo(self.hass.config.time_zone)
            except (ZoneInfoNotFoundError, TypeError):
                local_timezone = datetime.now().astimezone().tzinfo

            def extract_descriptors(payload: Any) -> list[Any]:
                if callable(extractor):
                    found = extractor(payload)
                else:
                    found = payload.get("records", []) if isinstance(payload, dict) else []
                if not isinstance(found, list):
                    raise RuntimeError("EZVIZ returned an unexpected record-list response")
                return found

            def describe_records(found: list[Any]) -> tuple[int, str, str, str, str]:
                """Return safe metadata only; never log descriptor payload values."""
                parsed = normalize_records({"records": found}, local_timezone=local_timezone)
                if parsed:
                    first_start = min(item["start"] for item in parsed).strftime("%H:%M:%S")
                    latest_start = max(item["start"] for item in parsed).strftime("%H:%M:%S")
                    latest_end = max(item["end"] for item in parsed).strftime("%H:%M:%S")
                else:
                    first_start = latest_start = latest_end = "unparsed"
                first = next((item for item in found if isinstance(item, dict)), None)
                field_names = ",".join(sorted(str(key) for key in first.keys()))[:180] if first else "none"
                return len(parsed), first_start, latest_start, latest_end, field_names

            def query_interval(
                interval_start: datetime,
                interval_stop: datetime,
                depth: int = 0,
            ) -> list[Any] | None:
                """Query one interval, splitting it whenever the result limit is hit.

                ``None`` means the safety call budget prevented this interval from
                being queried. Parent intervals retain their already fetched
                descriptors as a fallback so the browser does not go blank.
                """
                if stats["calls"] >= max_query_calls:
                    stats["budget_exhausted"] = True
                    return None

                stats["calls"] += 1
                payload = client.search_records(
                    serial=api_serial,
                    channel=self.channel,
                    channel_serial=api_serial,
                    start_time=interval_start.strftime("%Y-%m-%dT%H:%M:%S"),
                    stop_time=interval_stop.strftime("%Y-%m-%dT%H:%M:%S"),
                    size=query_size,
                )
                found = extract_descriptors(payload)
                stats["raw_descriptors"] += len(found)
                parsed_count, first_start, latest_start, latest_end, field_names = describe_records(found)
                # Keep these diagnostics at WARNING temporarily so the result is
                # visible in standard Home Assistant logs without enabling debug.
                # The distinct marker confirms which api.py implementation is loaded.
                LOGGER.warning(
                    "EZVIZ ARCHIVE DIAG build=%s camera=%s day=%s window=%s..%s "
                    "raw=%d parsed=%d first_start=%s latest_start=%s latest_end=%s fields=[%s]",
                    build_marker,
                    api_serial,
                    selected_day,
                    interval_start.strftime("%H:%M:%S"),
                    interval_stop.strftime("%H:%M:%S"),
                    len(found),
                    parsed_count,
                    first_start,
                    latest_start,
                    latest_end,
                    field_names,
                )

                # A shorter response is not at the batch ceiling, so it is
                # considered complete. Exactly 100 is split as well, because
                # the server may have returned only the first 100 matches.
                if len(found) < query_size:
                    return found

                duration_seconds = (interval_stop - interval_start).total_seconds()
                if depth >= max_split_depth or duration_seconds <= 1:
                    stats["saturated_windows"] += 1
                    LOGGER.warning(
                        "EZVIZ archive result limit may still truncate %s on %s: "
                        "%d records in %s..%s (split depth %d)",
                        api_serial,
                        selected_day,
                        len(found),
                        interval_start.strftime("%H:%M:%S"),
                        interval_stop.strftime("%H:%M:%S"),
                        depth,
                    )
                    return found

                # Keep API timestamps at whole-second precision. Because adjacent
                # windows meet at the same midpoint, records on the boundary may
                # be returned twice by inclusive server filters; normalize_records
                # deduplicates stable descriptor keys after all windows are merged.
                half_seconds = int(duration_seconds // 2)
                if half_seconds <= 0:
                    stats["saturated_windows"] += 1
                    return found
                midpoint = interval_start + timedelta(seconds=half_seconds)
                if midpoint <= interval_start or midpoint >= interval_stop:
                    stats["saturated_windows"] += 1
                    return found

                stats["splits"] += 1
                LOGGER.debug(
                    "EZVIZ returned the full %d-record batch for %s on %s; "
                    "splitting %s..%s at %s (depth %d)",
                    query_size,
                    api_serial,
                    selected_day,
                    interval_start.strftime("%H:%M:%S"),
                    interval_stop.strftime("%H:%M:%S"),
                    midpoint.strftime("%H:%M:%S"),
                    depth,
                )
                left = query_interval(interval_start, midpoint, depth + 1)
                right = query_interval(midpoint, interval_stop, depth + 1)

                # Preserve the current interval's result if the safety budget is
                # reached below this point. Include any completed child results too;
                # normalization de-duplicates these overlapping API responses.
                if left is None or right is None:
                    fallback = list(found)
                    if left is not None:
                        fallback.extend(left)
                    if right is not None:
                        fallback.extend(right)
                    return fallback
                return left + right

            # Begin with four independent windows. This tests whether the API is
            # honoring start_time/stop_time and avoids relying only on the full-day
            # result hitting exactly the 100-item limit. Any window that reaches
            # 100 records is recursively split further by query_interval().
            descriptors: list[Any] = []
            window_start = day_start
            while window_start < day_stop:
                window_stop = min(window_start + timedelta(hours=6), day_stop)
                stats["base_windows"] += 1
                window_records = query_interval(window_start, window_stop)
                if window_records is not None:
                    descriptors.extend(window_records)
                window_start = window_stop

            records = normalize_records({"records": descriptors}, local_timezone=local_timezone)
            day_records = [item for item in records if item["start"].date() == selected_day]
            records = day_records
            LOGGER.warning(
                "EZVIZ ARCHIVE DIAG completed build=%s camera=%s day=%s records=%d calls=%d "
                "raw=%d base_windows=%d adaptive_splits=%d saturated=%d budget_exhausted=%s",
                build_marker,
                api_serial,
                selected_day,
                len(records),
                stats["calls"],
                stats["raw_descriptors"],
                stats["base_windows"],
                stats["splits"],
                stats["saturated_windows"],
                stats["budget_exhausted"],
            )
            if stats["budget_exhausted"]:
                LOGGER.warning(
                    "EZVIZ archive query reached its safety limit (%d requests) for %s on %s; "
                    "the returned list may still be incomplete. Try another refresh if needed.",
                    max_query_calls,
                    api_serial,
                    selected_day,
                )
            LOGGER.debug(
                "EZVIZ archive query completed for %s on %s: %d normalized records, "
                "%d raw descriptors across %d API requests (%d adaptive splits, "
                "%d still-saturated windows)",
                api_serial,
                selected_day,
                len(records),
                stats["raw_descriptors"],
                stats["calls"],
                stats["splits"],
                stats["saturated_windows"],
            )
            return records
        except Exception as err:
            detail = self._sanitize_diagnostic(f"{type(err).__name__}: {err}")
            LOGGER.warning(
                "Could not list EZVIZ SD-card records for %s (%s): %s",
                selected_day,
                type(err).__name__,
                detail or "No additional error details",
            )
            raise RuntimeError(
                f"Could not query EZVIZ SD-card records ({type(err).__name__}). See preceding log details."
            ) from err
        finally:
            if client is not None:
                try:
                    client.close_session()
                except Exception:
                    pass

    def _sanitize_diagnostic(self, text: str) -> str:
        """Return bounded API diagnostics with known secrets redacted."""
        detail = (text or "").strip()
        if not detail:
            return ""

        secrets = {self.username, self.password, self.serial}
        try:
            token = json.loads(self.token_path.read_text(encoding="utf-8"))
            if isinstance(token, dict):
                for key in ("session_id", "rf_session_id", "password", "access_token", "refresh_token", "token"):
                    value = token.get(key)
                    if isinstance(value, str) and len(value) >= 4:
                        secrets.add(value)
        except (OSError, json.JSONDecodeError):
            pass
        for secret in sorted(secrets, key=len, reverse=True):
            if secret:
                detail = detail.replace(secret, "[REDACTED]")

        # Guard against common key=value / URL query dumps in third-party diagnostics.
        detail = re.sub(
            r"(?i)(session_?id|rf_session_?id|access_?token|refresh_?token|password|authorization|cookie|secret)(\"?\s*[:=]\s*\"?)([^\s,;\"'}]+)",
            r"\1\2[REDACTED]",
            detail,
        )
        detail = re.sub(r"https?://[^\s?]+\?[^\s]+", "[URL-REDACTED]", detail)
        detail = re.sub(r"\s+", " ", detail)
        return detail[:900]

    async def async_download_native_record(self, record: dict[str, Any], destination: Path, progress_callback=None) -> None:
        """Download a native SD playback range and report bytes received when possible.

        The pyEzvizApi playback method doesn't expose Content-Length/progress. While
        it runs in HA's executor, watch its dedicated temporary MPEG-PS file and
        report the byte count to the caller. The final MP4 is published atomically.
        """
        download_future = self.hass.async_add_executor_job(self._download_native_record, record, destination)
        last_size = -1
        if progress_callback:
            while not download_future.done():
                await asyncio.sleep(1)
                size = await self.hass.async_add_executor_job(
                    self._native_temp_size, destination.parent, destination.stem
                )
                if size > last_size:
                    last_size = size
                    progress_callback(size, None)
        await download_future

    @staticmethod
    def _native_temp_size(directory: Path, destination_stem: str) -> int:
        """Return bytes written to this recording's private temporary stream."""
        prefix = f".ezviz-native-{destination_stem}-"
        total = 0
        try:
            for path in directory.glob(f"{prefix}*.ps"):
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
        except OSError:
            pass
        return total

    def _download_native_record(self, record: dict[str, Any], destination: Path) -> None:
        """Blocking native EZVIZ SD-card playback download implementation."""
        self._ensure_token_file()
        try:
            token = json.loads(self.token_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as err:
            raise RuntimeError("Could not read the stored EZVIZ session token") from err

        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("ffmpeg is not available in the Home Assistant runtime; cannot remux the native recording")

        start: datetime = record["start"]
        end: datetime = record["end"]
        if start.tzinfo is None:
            start = start.replace(tzinfo=datetime.now().astimezone().tzinfo)
        if end.tzinfo is None:
            end = end.replace(tzinfo=datetime.now().astimezone().tzinfo)
        if end <= start:
            raise RuntimeError("The EZVIZ descriptor has an invalid playback interval")
        # Probe results from the user's C6N prove EZVIZ VTM expects the camera/API
        # wall-clock fields, not UTC-converted fields. UTC conversion returns result
        # 5402 with no VTM session; preserving Asia/Jakarta wall time returns result=0
        # and media packets. The wire format has a trailing "Z", but in this API it
        # must not trigger a timezone conversion.
        try:
            playback_timezone = ZoneInfo(self.hass.config.time_zone)
        except (ZoneInfoNotFoundError, TypeError):
            playback_timezone = datetime.now().astimezone().tzinfo
        if playback_timezone is None:
            playback_timezone = start.tzinfo
        begin_time = format_native_playback_timestamp(start, playback_timezone)
        end_time = format_native_playback_timestamp(end, playback_timezone)

        from pyezvizapi.client import EzvizClient

        destination.parent.mkdir(parents=True, exist_ok=True)
        fd_ps, ps_name = tempfile.mkstemp(prefix=f".ezviz-native-{destination.stem}-", suffix=".ps", dir=destination.parent)
        os.close(fd_ps)
        fd_mp4, mp4_name = tempfile.mkstemp(prefix=f".ezviz-remux-{destination.stem}-", suffix=".mp4", dir=destination.parent)
        os.close(fd_mp4)
        ps_path = Path(ps_name)
        mp4_path = Path(mp4_name)
        client = None
        try:
            client = EzvizClient(token=token, url=self.region)
            device_records = client.get_device_records()
            api_serial = resolve_api_serial(self.serial, device_records.keys())
            save_clip = getattr(client, "save_clip", None)
            if not callable(save_clip):
                raise RuntimeError(
                    "Installed pyEzvizApi does not include save_clip(); "
                    "the native SD-card playback dependency was not installed correctly."
                )

            # A few Home Assistant installations keep the PyPI build already present in
            # the base image even when a custom integration declares a VCS dependency.
            # Fail with actionable diagnostics instead of leaking Python's unexpected-
            # keyword TypeError to the media browser.
            required_parameters = {
                "source",
                "cloud_playback_begin_time",
                "cloud_playback_end_time",
            }
            try:
                save_parameters = set(inspect.signature(save_clip).parameters)
            except (TypeError, ValueError):
                save_parameters = set()
            missing_parameters = sorted(required_parameters - save_parameters)
            if missing_parameters:
                try:
                    installed_version = package_version("pyezvizapi")
                except PackageNotFoundError:
                    installed_version = "unknown"
                try:
                    loaded_from = inspect.getfile(type(client))
                except (TypeError, OSError):
                    loaded_from = "unknown location"
                raise RuntimeError(
                    "Incompatible pyEzvizApi build loaded: "
                    f"version={installed_version}, path={loaded_from}, "
                    f"missing save_clip parameters={', '.join(missing_parameters)}. "
                    "This integration requires the cloud-playback commit "
                    "a7710099624008e113269daed0f7f41e3d0b510e. "
                    "Restart Home Assistant after updating the integration requirements; "
                    "if it persists, force-install the pinned archive inside the HA container."
                )

            # pyEzvizApi currently raises PyEzvizError when the peer closes the
            # VTM TCP socket while trying to read the *next* packet. The user's
            # direct probe showed this can happen after the complete recording has
            # already been written. Do not monkey-patch VtmStreamClient: preserve
            # packet validation and only tolerate this exact EOF error when there
            # is a non-empty PS file; validate duration after remux below.
            eof_after_media = False
            try:
                save_clip(
                    api_serial,
                    ps_path,
                    source="cloud-playback",
                    output_format="mpegps",
                    channel=self.channel,
                    # The successful C6N VTM probe uses client_type=3. pyEzvizApi's
                    # save_clip default is 9, which can bootstrap then close early.
                    cloud_client_type=3,
                    # Raw stream produced a decodable H.264/AAC PS in C6N diagnostics;
                    # optional NAL decryption produced invalid H.264 parameter sets.
                    decrypt_video=False,
                    cloud_playback_begin_time=begin_time,
                    cloud_playback_end_time=end_time,
                    timeout=30.0,
                )
            except Exception as err:
                from pyezvizapi.exceptions import PyEzvizError

                partial_size = ps_path.stat().st_size if ps_path.exists() else 0
                if (
                    isinstance(err, PyEzvizError)
                    and str(err) == "VTM socket closed while reading packet"
                    and partial_size > 0
                ):
                    eof_after_media = True
                    LOGGER.warning(
                        "EZVIZ VTM socket closed after writing %d bytes; validating the media before accepting EOF",
                        partial_size,
                    )
                else:
                    raise
            size = ps_path.stat().st_size if ps_path.exists() else 0
            if size <= 0:
                raise RuntimeError("EZVIZ playback returned an empty native media stream")
            if size > 2 * 1024 * 1024 * 1024:
                raise RuntimeError("The selected recording exceeds the 2 GiB safety limit")

            # MPEG-PS from EZVIZ is remuxed without transcoding, preserving native
            # video/audio codecs wherever MP4 supports them.
            process = subprocess.run(
                [
                    ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                    "-fflags", "+genpts", "-f", "mpeg", "-i", str(ps_path),
                    "-map", "0:v?", "-map", "0:a?", "-c", "copy",
                    "-movflags", "+faststart", str(mp4_path),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=180,
            )
            if process.returncode != 0:
                detail = self._sanitize_diagnostic(process.stderr or "FFmpeg failed")
                raise RuntimeError(f"Could not remux EZVIZ playback to MP4: {detail}")
            output_size = mp4_path.stat().st_size if mp4_path.exists() else 0
            if output_size <= 0:
                raise RuntimeError("FFmpeg completed without producing a playable MP4")

            # If save_clip ended with the known VTM EOF condition, verify the
            # remuxed duration still covers most of the selected descriptor. This
            # rejects short/truncated output instead of treating every socket close
            # as success. If ffprobe is not installed, successful remux remains the
            # available validation signal.
            if eof_after_media:
                ffprobe = shutil.which("ffprobe")
                if ffprobe:
                    probe = subprocess.run(
                        [
                            ffprobe, "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=noprint_wrappers=1:nokey=1", str(mp4_path),
                        ],
                        check=False,
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    try:
                        actual_duration = float(probe.stdout.strip())
                    except (TypeError, ValueError):
                        actual_duration = 0.0
                    expected_duration = (end - start).total_seconds()
                    required_duration = max(1.0, expected_duration * 0.7)
                    if probe.returncode != 0 or actual_duration < required_duration:
                        raise RuntimeError(
                            "VTM closed after writing media, but the remuxed recording is too short "
                            f"({actual_duration:.2f}s; expected about {expected_duration:.2f}s)"
                        )
                    LOGGER.info(
                        "Accepted VTM EOF after validating %.2f-second recording (expected %.2f seconds)",
                        actual_duration,
                        expected_duration,
                    )
                else:
                    LOGGER.warning("ffprobe is unavailable; accepting VTM EOF after successful FFmpeg remux")
            os.replace(mp4_path, destination)
            try:
                os.chmod(destination, 0o640)
            except OSError:
                pass
            LOGGER.info("Downloaded EZVIZ SD-card recording into local cache (%d bytes)", output_size)
        except Exception as err:
            detail = self._sanitize_diagnostic(str(err))
            LOGGER.warning("Native EZVIZ SD-card playback download failed (%s): %s", type(err).__name__, detail)
            raise RuntimeError("Native EZVIZ SD-card playback download failed; see logs for details") from err
        finally:
            ps_path.unlink(missing_ok=True)
            mp4_path.unlink(missing_ok=True)
            if client is not None:
                try:
                    client.close_session()
                except Exception:
                    pass

    async def async_find_record(self, selected_day: date, key: str) -> dict[str, Any] | None:
        """Resolve an opaque record key, refreshing the day's descriptor cache if needed."""
        records = await self.async_records_for_day(selected_day)
        return next((item for item in records if item["key"] == key), None)
