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
import threading
import time as time_module
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

    DEVICE_EXCEPTION_RETRIES = 2
    DEVICE_EXCEPTION_BASE_DELAY = 3
    FAILURE_COOLDOWN = timedelta(minutes=10)
    MAX_QUERY_CALLS = 32
    MAX_SPLIT_DEPTH = 8
    RESULT_BATCH_SIZE = 50
    VTM_RETRY_ATTEMPTS = 2
    VTM_RETRY_BASE_DELAY = 10
    VTM_SESSION_COOLDOWN = 4
    REMUX_RETRY_CYCLES = 2
    REMUX_EXPAND_START_SECONDS = 30
    # VTM cloud client profile. Type 3 is the value that has been verified
    # to work reliably across H8c and C6N cameras in the Singapore region.
    # Type 4 sometimes delivers incomplete slices or fails to bootstrap.
    # decrypt_video must remain False: True makes save_clip fail outright.
    VTM_CLOUD_CLIENT_TYPE = 3

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.serial = str(entry.data[CONF_SERIAL])
        self.channel = int(entry.data.get(CONF_CHANNEL, 1))
        self.region = str(entry.data.get(CONF_REGION, "apiieu.ezvizlife.com"))
        self.username = str(entry.data.get(CONF_USERNAME))
        self.password = str(entry.data[CONF_PASSWORD])
        self._lock = asyncio.Lock()
        self._cache: dict[date, tuple[datetime, list[dict[str, Any]]]] = {}
        self._cache_ttl = timedelta(minutes=5)
        self._failure_cooldown: dict[date, datetime] = {}
        self._vtm_lock = threading.Lock()
        self._vtm_last_attempt = 0.0
        self.token_path = Path(
            hass.config.path(".storage", DOMAIN, entry.entry_id, "token.json")
        )

    async def async_prepare(self) -> None:
        """Write the initial token securely; don't perform a cloud scan on startup."""
        await self.hass.async_add_executor_job(self._ensure_token_file)

    def _ensure_token_file(self) -> None:
        self.token_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.token_path.parent, 0o700)
        except OSError:
            pass
        legacy_token_path = Path(
            self.hass.config.path(".storage", DOMAIN, f"{self.entry.entry_id}.json")
        )
        if not self.token_path.exists() and legacy_token_path.is_file():
            try:
                os.replace(legacy_token_path, self.token_path)
                os.chmod(self.token_path, 0o600)
            except OSError:
                LOGGER.debug(
                    "Could not migrate legacy EZVIZ token file; will use config entry token"
                )
        if not self.token_path.exists():
            token = self.entry.data.get(CONF_SESSION_TOKEN)
            if not isinstance(token, dict):
                raise RuntimeError(
                    "No EZVIZ session token is available; reconfigure the integration"
                )
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
            now = datetime.now().astimezone()

            cooldown_until = self._failure_cooldown.get(selected_day)
            if cooldown_until and now < cooldown_until:
                remaining = int((cooldown_until - now).total_seconds())
                raise RuntimeError(
                    f"EZVIZ archive query for {selected_day} is paused for "
                    f"{remaining}s after a previous failure."
                )
            if cooldown_until and now >= cooldown_until:
                self._failure_cooldown.pop(selected_day, None)

            cached = self._cache.get(selected_day)
            if cached and now - cached[0] < self._cache_ttl:
                return cached[1]

            try:
                records = await self.hass.async_add_executor_job(
                    self._query_day, selected_day
                )
            except RuntimeError:
                self._failure_cooldown[selected_day] = now + self.FAILURE_COOLDOWN
                raise

            self._cache[selected_day] = (now, records)
            return records

    def _query_day(self, selected_day: date) -> list[dict[str, Any]]:
        """Query the legacy ``search_records`` endpoint for one day."""
        self._ensure_token_file()
        from pyezvizapi.client import EzvizClient

        try:
            token = json.loads(self.token_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as err:
            raise RuntimeError(
                "Could not read the stored EZVIZ session token; reconfigure the integration"
            ) from err

        client = None
        try:
            client = EzvizClient(token=token, url=self.region)
            device_records = client.get_device_records()
            api_serial = resolve_api_serial(self.serial, device_records.keys())

            try:
                local_timezone = ZoneInfo(self.hass.config.time_zone)
            except (ZoneInfoNotFoundError, TypeError):
                local_timezone = datetime.now().astimezone().tzinfo

            try:
                today = datetime.now(local_timezone).date()
            except (ZoneInfoNotFoundError, TypeError):
                today = datetime.now().date()

            day_start = datetime.combine(selected_day, time.min)
            day_stop = datetime.combine(selected_day + timedelta(days=1), time.min)
            is_today_or_future = selected_day >= today

            if selected_day > today:
                return []

            if is_today_or_future:
                try:
                    now_local = datetime.now(local_timezone).replace(tzinfo=None)
                except (ZoneInfoNotFoundError, TypeError):
                    now_local = datetime.now()
                if now_local <= day_start:
                    return []
                effective_stop = min(day_stop, now_local)
            else:
                effective_stop = day_stop

            if effective_stop <= day_start:
                return []

            build_marker = "fast-single-query-v1"
            stats = {
                "calls": 0,
                "raw_descriptors": 0,
                "splits": 0,
                "device_exceptions": 0,
                "budget_exhausted": False,
            }
            extractor = getattr(client, "extract_record_list", None)

            def extract_descriptors(payload: Any) -> list[Any]:
                if callable(extractor):
                    found = extractor(payload)
                else:
                    found = (
                        payload.get("records", [])
                        if isinstance(payload, dict)
                        else []
                    )
                if not isinstance(found, list):
                    raise RuntimeError(
                        "EZVIZ returned an unexpected record-list response"
                    )
                return found

            def describe_records(found: list[Any]) -> tuple[int, str, str, str, str]:
                parsed = normalize_records(
                    {"records": found}, local_timezone=local_timezone
                )
                if parsed:
                    first_start = min(
                        item["start"] for item in parsed
                    ).strftime("%H:%M:%S")
                    latest_start = max(
                        item["start"] for item in parsed
                    ).strftime("%H:%M:%S")
                    latest_end = max(
                        item["end"] for item in parsed
                    ).strftime("%H:%M:%S")
                else:
                    first_start = latest_start = latest_end = "unparsed"
                first = next(
                    (item for item in found if isinstance(item, dict)), None
                )
                field_names = (
                    ",".join(sorted(str(k) for k in first.keys()))[:180]
                    if first
                    else "none"
                )
                return (
                    len(parsed),
                    first_start,
                    latest_start,
                    latest_end,
                    field_names,
                )

            def is_device_exception(err: Exception) -> bool:
                message = str(err)
                return "2004" in message and (
                    "DEVICE_EXCEPTION" in message or "设备异常" in message
                )

            def query_interval(
                interval_start: datetime,
                interval_stop: datetime,
                depth: int = 0,
            ) -> list[Any]:
                if stats["calls"] >= self.MAX_QUERY_CALLS:
                    stats["budget_exhausted"] = True
                    return []

                payload = None
                last_error: Exception | None = None
                for attempt in range(self.DEVICE_EXCEPTION_RETRIES):
                    stats["calls"] += 1
                    try:
                        payload = client.search_records(
                            serial=api_serial,
                            channel=self.channel,
                            channel_serial=api_serial,
                            start_time=interval_start.strftime(
                                "%Y-%m-%dT%H:%M:%S"
                            ),
                            stop_time=interval_stop.strftime(
                                "%Y-%m-%dT%H:%M:%S"
                            ),
                            size=self.RESULT_BATCH_SIZE,
                        )
                        last_error = None
                        break
                    except Exception as err:
                        last_error = err
                        if not is_device_exception(err):
                            raise
                        if attempt >= self.DEVICE_EXCEPTION_RETRIES - 1:
                            break
                        delay = (
                            attempt + 1
                        ) * self.DEVICE_EXCEPTION_BASE_DELAY
                        LOGGER.warning(
                            "EZVIZ 2004/70 for %s, %s window %s..%s; "
                            "retrying in %ds (%d/%d)",
                            api_serial,
                            selected_day,
                            interval_start.strftime("%H:%M:%S"),
                            interval_stop.strftime("%H:%M:%S"),
                            delay,
                            attempt + 1,
                            self.DEVICE_EXCEPTION_RETRIES - 1,
                        )
                        time_module.sleep(delay)

                if payload is None:
                    stats["device_exceptions"] += 1
                    LOGGER.warning(
                        "EZVIZ gave up on %s..%s for %s after %d attempts "
                        "(last error: %s)",
                        interval_start.strftime("%H:%M:%S"),
                        interval_stop.strftime("%H:%M:%S"),
                        selected_day,
                        self.DEVICE_EXCEPTION_RETRIES,
                        type(last_error).__name__
                        if last_error
                        else "unknown",
                    )
                    return []

                found = extract_descriptors(payload)
                stats["raw_descriptors"] += len(found)
                (
                    parsed_count,
                    first_start,
                    latest_start,
                    latest_end,
                    field_names,
                ) = describe_records(found)
                LOGGER.warning(
                    "EZVIZ DIAG build=%s camera=%s day=%s window=%s..%s "
                    "raw=%d parsed=%d first_start=%s latest_start=%s "
                    "latest_end=%s fields=[%s]",
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

                is_finished: bool | None = None
                if isinstance(payload, dict):
                    is_finished = payload.get("isFinished")
                has_more = is_finished is False

                if (
                    len(found) < self.RESULT_BATCH_SIZE
                    and not has_more
                ):
                    return found

                duration_seconds = (
                    interval_stop - interval_start
                ).total_seconds()
                if depth >= self.MAX_SPLIT_DEPTH or duration_seconds <= 1:
                    return found

                half_seconds = int(duration_seconds // 2)
                if half_seconds <= 0:
                    return found
                midpoint = interval_start + timedelta(seconds=half_seconds)
                if midpoint <= interval_start or midpoint >= interval_stop:
                    return found

                stats["splits"] += 1
                LOGGER.debug(
                    "EZVIZ splitting %s..%s at %s (depth %d)",
                    interval_start.strftime("%H:%M:%S"),
                    interval_stop.strftime("%H:%M:%S"),
                    midpoint.strftime("%H:%M:%S"),
                    depth,
                )
                left = query_interval(interval_start, midpoint, depth + 1)
                right = query_interval(midpoint, interval_stop, depth + 1)
                return left + right

            descriptors = query_interval(day_start, effective_stop)
            records = normalize_records(
                {"records": descriptors}, local_timezone=local_timezone
            )
            day_records = [
                item for item in records if item["start"].date() == selected_day
            ]

            LOGGER.warning(
                "EZVIZ DIAG completed build=%s camera=%s day=%s records=%d "
                "calls=%d raw=%d device_exceptions=%d splits=%d "
                "budget_exhausted=%s",
                build_marker,
                api_serial,
                selected_day,
                len(day_records),
                stats["calls"],
                stats["raw_descriptors"],
                stats["device_exceptions"],
                stats["splits"],
                stats["budget_exhausted"],
            )

            if not day_records and stats["device_exceptions"] > 0:
                if is_today_or_future:
                    raise RuntimeError(
                        "Today's recordings aren't available through the cloud "
                        "API yet. The EZVIZ app can show them because it talks "
                        "directly to the camera, but the cloud index lags. "
                        "Try again later, or use the EZVIZ app for very recent "
                        "recordings."
                    )
                raise RuntimeError(
                    "EZVIZ cloud could not reach the camera. It may be "
                    "recording, busy writing to the SD card, or briefly "
                    "offline. Please try again in a few minutes."
                )

            if stats["budget_exhausted"]:
                LOGGER.warning(
                    "EZVIZ archive query reached its safety limit (%d "
                    "requests) for %s on %s; the returned list may still be "
                    "incomplete.",
                    self.MAX_QUERY_CALLS,
                    api_serial,
                    selected_day,
                )

            return day_records
        except RuntimeError:
            raise
        except Exception as err:
            detail = self._sanitize_diagnostic(
                f"{type(err).__name__}: {err}"
            )
            LOGGER.warning(
                "Could not list EZVIZ SD-card records for %s (%s): %s",
                selected_day,
                type(err).__name__,
                detail or "No additional error details",
            )
            raise RuntimeError(
                f"Could not query EZVIZ SD-card records "
                f"({type(err).__name__}). See preceding log details."
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
                for key in (
                    "session_id",
                    "rf_session_id",
                    "password",
                    "access_token",
                    "refresh_token",
                    "token",
                ):
                    value = token.get(key)
                    if isinstance(value, str) and len(value) >= 4:
                        secrets.add(value)
        except (OSError, json.JSONDecodeError):
            pass
        for secret in sorted(secrets, key=len, reverse=True):
            if secret:
                detail = detail.replace(secret, "[REDACTED]")

        detail = re.sub(
            r"(?i)(session_?id|rf_session_?id|access_?token|refresh_?token|password|authorization|cookie|secret)(\"?\s*[:=]\s*\"?)([^\s,;\"'}]+)",
            r"\1\2[REDACTED]",
            detail,
        )
        detail = re.sub(r"https?://[^\s?]+\?[^\s]+", "[URL-REDACTED]", detail)
        detail = re.sub(r"\s+", " ", detail)
        return detail[:900]

    async def async_download_native_record(
        self, record: dict[str, Any], destination: Path, progress_callback=None
    ) -> None:
        """Download a native SD playback range and report bytes received when possible."""
        download_future = self.hass.async_add_executor_job(
            self._download_native_record, record, destination
        )
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

    def _open_vtm_session(
        self,
        client: "EzvizClient",
        api_serial: str,
        ps_path: Path,
        begin_time: str,
        end_time: str,
    ) -> bool:
        """Open one VTM session and stream it into ``ps_path`` as MPEG-PS.

        MPEG-PS is the only output format ``save_clip`` supports for
        cloud-playback. Returns True when the session ended with the known
        "VTM socket closed while reading packet" EOF condition after writing
        media. Transient VTM errors are retried up to ``VTM_RETRY_ATTEMPTS``
        times with linear backoff; the final failure propagates.
        """
        from pyezvizapi.exceptions import PyEzvizError

        save_clip = getattr(client, "save_clip", None)
        if not callable(save_clip):
            raise RuntimeError(
                "Installed pyEzvizApi does not include save_clip(); "
                "the native SD-card playback dependency was not installed correctly."
            )

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
                "a7710099624008e113269daed0f7f41e3d0b510e."
            )

        with self._vtm_lock:
            last_error: Exception | None = None

            for attempt in range(self.VTM_RETRY_ATTEMPTS):
                elapsed = time_module.monotonic() - self._vtm_last_attempt
                if elapsed < self.VTM_SESSION_COOLDOWN:
                    wait = self.VTM_SESSION_COOLDOWN - elapsed
                    LOGGER.debug(
                        "Waiting %.1fs before opening a new VTM session "
                        "for camera %s",
                        wait,
                        api_serial,
                    )
                    time_module.sleep(wait)
                self._vtm_last_attempt = time_module.monotonic()

                try:
                    save_clip(
                        api_serial,
                        ps_path,
                        source="cloud-playback",
                        output_format="mpegps",
                        channel=self.channel,
                        cloud_client_type=self.VTM_CLOUD_CLIENT_TYPE,
                        decrypt_video=False,
                        cloud_playback_begin_time=begin_time,
                        cloud_playback_end_time=end_time,
                        timeout=30.0,
                    )
                    size = ps_path.stat().st_size if ps_path.exists() else 0
                    if size <= 0:
                        raise RuntimeError(
                            "EZVIZ save_clip returned without producing media"
                        )
                    last_error = None
                    return False
                except Exception as err:
                    partial_size = ps_path.stat().st_size if ps_path.exists() else 0
                    if (
                        isinstance(err, PyEzvizError)
                        and str(err) == "VTM socket closed while reading packet"
                        and partial_size > 0
                    ):
                        LOGGER.debug(
                            "EZVIZ VTM socket closed after writing %d bytes; "
                            "validating media before accepting EOF",
                            partial_size,
                        )
                        return True

                    last_error = err
                    if attempt >= self.VTM_RETRY_ATTEMPTS - 1:
                        break

                    delay = (attempt + 1) * self.VTM_RETRY_BASE_DELAY
                    LOGGER.warning(
                        "EZVIZ save_clip failed on attempt %d/%d "
                        "for %s: %s; retrying in %ds",
                        attempt + 1,
                        self.VTM_RETRY_ATTEMPTS,
                        api_serial,
                        self._sanitize_diagnostic(str(err)) or type(err).__name__,
                        delay,
                    )
                    ps_path.unlink(missing_ok=True)
                    time_module.sleep(delay)

            if last_error is not None:
                raise last_error
            return False

    @staticmethod
    def _find_start_code_offset(
        path: Path, max_scan: int = 16 * 1024 * 1024
    ) -> int:
        """Find offset of first MPEG start code (00 00 01) in file.

        Scans up to ``max_scan`` bytes. Returns -1 if not found. Handles
        start codes that straddle the 1 MiB chunk boundary by keeping the
        last 2 bytes of each chunk as a tail.
        """
        try:
            with path.open("rb") as handle:
                absolute = 0
                tail = b""
                while absolute < max_scan:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    window = tail + chunk
                    idx = window.find(b"\x00\x00\x01")
                    if idx >= 0:
                        return absolute - len(tail) + idx
                    tail = window[-2:]
                    absolute += len(chunk)
        except OSError:
            pass
        return -1

    def _strip_prefix_to_start_code(self, path: Path) -> bool:
        """Strip bytes before the first MPEG start code.

        VTM frequently writes a handshake preamble before the actual
        MPEG-PS pack header. FFmpeg's demuxer then complains "No start
        code is found" and refuses to process anything. Removing that
        prefix turns the file into a valid PS stream.

        Returns True if the file was modified.
        """
        offset = self._find_start_code_offset(path)
        if offset <= 0:
            return False
        try:
            with path.open("rb") as src:
                src.seek(offset)
                payload = src.read()
            with path.open("wb") as dst:
                dst.write(payload)
            LOGGER.info(
                "EZVIZ stripped %d-byte prefix before first MPEG start code in %s",
                offset,
                path.name,
            )
            return True
        except OSError as err:
            LOGGER.warning(
                "Could not strip prefix from %s (%s)", path.name, err
            )
            return False

    def _remux_to_mp4(
        self,
        ffmpeg: str,
        ps_path: Path,
        mp4_path: Path,
    ) -> None:
        """Remux an MPEG-PS stream into a playable MP4.

        Philosophy: **preserve the original video, do not re-encode unless
        there is no other way.**

        EZVIZ VTM streams are frequently incomplete — missing slices show
        up as green blocks in the bottom third or quarter of the frame.
        Transcoding to H.264 does not fix that missing data; it merely
        re-encodes the green blocks into a permanent artefact and adds a
        second layer of lossy compression on top of the intact parts of
        the frame. Users have reported that the raw HEVC played in VLC
        looks noticeably better than the transcoded H.264 result, which
        is consistent: VLC's decoder conceals missing slices more
        gracefully (freeze frame) than FFmpeg's default green fill, and
        the copy path retains the original HEVC bitstream untouched.

        Strategy order, best-quality first:

        1. ``copy+audio-aac``: copy HEVC video byte-for-byte, transcode
           MP2 audio to AAC. Fast, preserves original video quality.
        2. ``copy+silent``: copy HEVC video, drop audio if it cannot be
           decoded. Fast, preserves original video quality.
        3. ``transcode-h264-aac``: only reached when copy fails at the
           demux level. Re-encodes video to H.264 for browser
           compatibility at the cost of quality.
        4. ``transcode-h264-silent``: last resort, H.264 with no audio.

        Consequence: most recordings will be HEVC-in-MP4. Browsers cannot
        play HEVC, so Home Assistant's Media Browser will show a blank
        page. That is expected. Users should download the file and open
        it in a native player (VLC, mpv, etc.) that supports HEVC.
        This trade-off is intentional: HEVC-in-MP4 preserves quality,
        H.264-in-MP4 plays in browsers but degrades the image.

        ``-ec 0`` is only relevant to the transcode fallback: it stops
        FFmpeg's decoder from patching corrupt slices with green pixels
        during the decode-for-transcode step. In copy mode there is no
        decoding, so the flag has no effect on the output file.

        Each strategy is tried against several demuxers (mpeg, h264,
        hevc, mpegts, auto) so a stream that is actually a raw elementary
        stream still parses.
        """
        # Diagnose: log the first 32 bytes so a failed remux can be
        # distinguished from a corrupt VTM response by looking at the log.
        try:
            with ps_path.open("rb") as handle:
                head = handle.read(32)
            if head:
                LOGGER.info(
                    "EZVIZ raw stream %s first 32 bytes: %s",
                    ps_path.name,
                    head.hex(),
                )
        except OSError:
            pass

        # Strip any handshake prefix before the first start code.
        self._strip_prefix_to_start_code(ps_path)

        demuxer_formats: list[str | None] = [
            "mpeg",
            "h264",
            "hevc",
            "mpegts",
            None,
        ]

        strategies: list[tuple[str, list[str]]] = [
            # 1) Primary: copy video, transcode audio. Fast and lossless on
            #    the video side. Output is HEVC-in-MP4; browser playback is
            #    not expected, users should download and use a native player.
            (
                "copy+audio-aac",
                [
                    "-c:v", "copy",
                    "-map", "0:a?",
                    "-c:a", "aac",
                    "-b:a", "128k",
                ],
            ),
            # 2) Copy video, drop audio if the audio track cannot decode.
            (
                "copy+silent",
                ["-c:v", "copy", "-an"],
            ),
            # 3) Fallback: transcode video to H.264 for browser playback.
            #    Only reached when copy fails at the demux level.
            (
                "transcode-h264-aac",
                [
                    "-c:v", "libx264",
                    "-preset", "veryfast",
                    "-crf", "23",
                    "-threads", "0",
                    "-tag:v", "avc1",
                    "-pix_fmt", "yuv420p",
                    "-map", "0:a?",
                    "-c:a", "aac",
                    "-b:a", "128k",
                ],
            ),
            # 4) Last resort: H.264 without audio.
            (
                "transcode-h264-silent",
                [
                    "-c:v", "libx264",
                    "-preset", "veryfast",
                    "-crf", "23",
                    "-threads", "0",
                    "-tag:v", "avc1",
                    "-pix_fmt", "yuv420p",
                    "-an",
                ],
            ),
        ]

        last_stderr = ""
        for demuxer in demuxer_formats:
            fmt_args = ["-f", demuxer] if demuxer else []
            input_opts = [
                # Disable error concealment: when the transcode fallback
                # actually runs, corrupted slices become black/frozen
                # instead of bright green.
                "-ec", "0",
                "-fflags", "+genpts+discardcorrupt+igndts",
                "-err_detect", "ignore_err",
                "-analyzeduration", "500M",
                "-probesize", "500M",
                *fmt_args,
            ]
            for label, out_args in strategies:
                full_label = f"{(demuxer or 'auto')}-{label}"
                command = [
                    ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                    *input_opts,
                    "-i", str(ps_path),
                    "-map", "0:v?",
                    "-movflags", "+faststart",
                    "-max_muxing_queue_size", "1024",
                    *out_args,
                    str(mp4_path),
                ]
                try:
                    process = subprocess.run(
                        command,
                        check=False,
                        capture_output=True,
                        text=True,
                        timeout=600,
                    )
                except (OSError, subprocess.SubprocessError) as err:
                    last_stderr = f"{type(err).__name__}: {err}"
                    mp4_path.unlink(missing_ok=True)
                    continue

                produced = mp4_path.exists() and mp4_path.stat().st_size > 0
                if process.returncode == 0 and produced:
                    LOGGER.info(
                        "EZVIZ remux succeeded using %s strategy", full_label
                    )
                    return

                last_stderr = (process.stderr or "").strip()
                mp4_path.unlink(missing_ok=True)
                LOGGER.debug(
                    "EZVIZ remux attempt %s failed (rc=%d): %s",
                    full_label,
                    process.returncode,
                    self._sanitize_diagnostic(last_stderr)[:200]
                    or "(no stderr)",
                )

        detail = self._sanitize_diagnostic(last_stderr or "FFmpeg failed")
        raise RuntimeError(f"Could not remux EZVIZ playback to MP4: {detail}")

    def _download_native_record(
        self, record: dict[str, Any], destination: Path
    ) -> None:
        """Blocking native EZVIZ SD-card playback download implementation.

        A failed remux (usually because the HEVC stream started mid-GOP and
        the parameter sets are missing) triggers a second VTM cycle with the
        start time expanded by ``REMUX_EXPAND_START_SECONDS``. This includes
        the previous GOP so the SPS/PPS packets are captured.
        """
        self._ensure_token_file()
        try:
            token = json.loads(self.token_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as err:
            raise RuntimeError(
                "Could not read the stored EZVIZ session token"
            ) from err

        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError(
                "ffmpeg is not available in the Home Assistant runtime; "
                "cannot remux the native recording"
            )

        start: datetime = record["start"]
        end: datetime = record["end"]
        if start.tzinfo is None:
            start = start.replace(tzinfo=datetime.now().astimezone().tzinfo)
        if end.tzinfo is None:
            end = end.replace(tzinfo=datetime.now().astimezone().tzinfo)
        if end <= start:
            raise RuntimeError(
                "The EZVIZ descriptor has an invalid playback interval"
            )

        try:
            playback_timezone = ZoneInfo(self.hass.config.time_zone)
        except (ZoneInfoNotFoundError, TypeError):
            playback_timezone = datetime.now().astimezone().tzinfo
        if playback_timezone is None:
            playback_timezone = start.tzinfo

        from pyezvizapi.client import EzvizClient

        destination.parent.mkdir(parents=True, exist_ok=True)
        client = None
        ps_path: Path | None = None
        mp4_path: Path | None = None
        eof_after_media = False
        last_cycle_error: Exception | None = None
        output_size = 0

        try:
            client = EzvizClient(token=token, url=self.region)
            device_records = client.get_device_records()
            api_serial = resolve_api_serial(self.serial, device_records.keys())

            for cycle in range(self.REMUX_RETRY_CYCLES):
                if ps_path is not None:
                    ps_path.unlink(missing_ok=True)
                if mp4_path is not None:
                    mp4_path.unlink(missing_ok=True)

                fd_ps, ps_name = tempfile.mkstemp(
                    prefix=f".ezviz-native-{destination.stem}-",
                    suffix=".ps",
                    dir=destination.parent,
                )
                os.close(fd_ps)
                fd_mp4, mp4_name = tempfile.mkstemp(
                    prefix=f".ezviz-remux-{destination.stem}-",
                    suffix=".mp4",
                    dir=destination.parent,
                )
                os.close(fd_mp4)
                ps_path = Path(ps_name)
                mp4_path = Path(mp4_name)

                if cycle == 0:
                    attempt_start = start
                else:
                    attempt_start = start - timedelta(
                        seconds=self.REMUX_EXPAND_START_SECONDS
                    )
                    LOGGER.info(
                        "Retrying VTM for %s with start expanded by %ds "
                        "to capture HEVC parameter sets",
                        api_serial,
                        self.REMUX_EXPAND_START_SECONDS,
                    )

                begin_time = format_native_playback_timestamp(
                    attempt_start, playback_timezone
                )
                end_time = format_native_playback_timestamp(end, playback_timezone)

                try:
                    eof_after_media = self._open_vtm_session(
                        client, api_serial, ps_path, begin_time, end_time
                    )

                    size = ps_path.stat().st_size if ps_path.exists() else 0
                    if size <= 0:
                        raise RuntimeError(
                            "EZVIZ playback returned an empty native media stream"
                        )
                    if size > 2 * 1024 * 1024 * 1024:
                        raise RuntimeError(
                            "The selected recording exceeds the 2 GiB safety limit"
                        )

                    self._remux_to_mp4(ffmpeg, ps_path, mp4_path)

                    output_size = (
                        mp4_path.stat().st_size if mp4_path.exists() else 0
                    )
                    if output_size <= 0:
                        raise RuntimeError(
                            "FFmpeg completed without producing a playable MP4"
                        )

                    last_cycle_error = None
                    break
                except Exception as err:
                    last_cycle_error = err
                    LOGGER.warning(
                        "EZVIZ download cycle %d/%d for %s failed (%s): %s",
                        cycle + 1,
                        self.REMUX_RETRY_CYCLES,
                        api_serial,
                        type(err).__name__,
                        self._sanitize_diagnostic(str(err))
                        or "(no detail)",
                    )
                    if cycle + 1 >= self.REMUX_RETRY_CYCLES:
                        raise
                    time_module.sleep(5)

            if last_cycle_error is not None:
                raise last_cycle_error
            if mp4_path is None or not mp4_path.exists():
                raise RuntimeError(
                    "EZVIZ download did not produce an MP4 after retries"
                )

            if eof_after_media:
                expected_duration = (end - start).total_seconds()

                if expected_duration < 60:
                    LOGGER.info(
                        "Skipping duration validation for short recording "
                        "(%.1fs expected); accepting non-empty remux",
                        expected_duration,
                    )
                else:
                    ffprobe = shutil.which("ffprobe")
                    if ffprobe:
                        if expected_duration < 120:
                            required_duration = max(
                                1.0, expected_duration * 0.5
                            )
                        else:
                            required_duration = max(
                                1.0, expected_duration * 0.7
                            )
                        probe = subprocess.run(
                            [
                                ffprobe,
                                "-v",
                                "error",
                                "-show_entries",
                                "format=duration",
                                "-of",
                                "default=noprint_wrappers=1:nokey=1",
                                str(mp4_path),
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
                        if (
                            probe.returncode != 0
                            or actual_duration < required_duration
                        ):
                            raise RuntimeError(
                                "VTM closed after writing media, but the "
                                "remuxed recording is too short "
                                f"({actual_duration:.2f}s; expected about "
                                f"{expected_duration:.2f}s, required "
                                f"{required_duration:.2f}s)"
                            )
                        LOGGER.info(
                            "Accepted VTM EOF after validating %.2f-second "
                            "recording (expected %.2f seconds, required "
                            "%.2f seconds)",
                            actual_duration,
                            expected_duration,
                            required_duration,
                        )
                    else:
                        LOGGER.warning(
                            "ffprobe is unavailable; accepting VTM EOF after "
                            "successful FFmpeg remux"
                        )

            os.replace(mp4_path, destination)
            try:
                os.chmod(destination, 0o640)
            except OSError:
                pass
            LOGGER.info(
                "Downloaded EZVIZ SD-card recording into local cache "
                "(%d bytes)",
                output_size,
            )
        except Exception as err:
            detail = self._sanitize_diagnostic(str(err))
            LOGGER.warning(
                "Native EZVIZ SD-card playback download failed (%s): %s",
                type(err).__name__,
                detail,
            )
            raise RuntimeError(
                "Native EZVIZ SD-card playback download failed; see logs "
                "for details"
            ) from err
        finally:
            if ps_path is not None:
                ps_path.unlink(missing_ok=True)
            if mp4_path is not None:
                mp4_path.unlink(missing_ok=True)
            if client is not None:
                try:
                    client.close_session()
                except Exception:
                    pass

    async def async_find_record(
        self, selected_day: date, key: str
    ) -> dict[str, Any] | None:
        """Resolve an opaque record key, refreshing the day's descriptor cache."""
        records = await self.async_records_for_day(selected_day)
        return next((item for item in records if item["key"] == key), None)
