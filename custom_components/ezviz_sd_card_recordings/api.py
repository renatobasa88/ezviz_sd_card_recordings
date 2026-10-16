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
            start = datetime.combine(selected_day, time.min).strftime("%Y-%m-%dT%H:%M:%S")
            stop = datetime.combine(selected_day + timedelta(days=1), time.min).strftime("%Y-%m-%dT%H:%M:%S")
            payload = client.search_records(
                serial=api_serial,
                channel=self.channel,
                channel_serial=api_serial,
                start_time=start,
                stop_time=stop,
                size=100,
            )
            extractor = getattr(client, "extract_record_list", None)
            if callable(extractor):
                descriptors = extractor(payload)
            else:
                descriptors = payload.get("records", []) if isinstance(payload, dict) else []
            if not isinstance(descriptors, list):
                raise RuntimeError("EZVIZ returned an unexpected record-list response")

            try:
                local_timezone = ZoneInfo(self.hass.config.time_zone)
            except (ZoneInfoNotFoundError, TypeError):
                local_timezone = datetime.now().astimezone().tzinfo
            records = normalize_records({"records": descriptors}, local_timezone=local_timezone)
            LOGGER.debug(
                "EZVIZ archive query returned %d records for %s on %s",
                len(records),
                api_serial,
                selected_day,
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
