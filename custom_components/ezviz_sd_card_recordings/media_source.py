"""Expose EZVIZ SD-card records through Home Assistant Media Source."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from aiohttp import web
from homeassistant.components import persistent_notification
from homeassistant.components.http import HomeAssistantView
from homeassistant.components.media_player import BrowseError, MediaClass, MediaType
from homeassistant.components.media_source.error import Unresolvable
from homeassistant.components.media_source.models import BrowseMediaSource, MediaSource, MediaSourceItem, PlayMedia
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from .api import EzvizArchiveApi
from .const import (
    CACHE_VIEW_URL,
    CONF_COLD_STORAGE_PATH,
    CONF_MEDIA_SYNC,
    CONF_ORDER_DAYS,
    CONF_ORDER_RECORDINGS,
    CONF_SYNC_HOURS,
    CONF_SYNC_NOTIFY,
    DEFAULT_ORDER_DAYS,
    DEFAULT_ORDER_RECORDINGS,
    DEFAULT_MEDIA_SYNC,
    DEFAULT_SYNC_NOTIFY,
    DOMAIN,
    LOGGER,
    MEDIA_EVENT_DOWNLOADED,
    NOTIFICATION_PREFIX,
)
from .recordings import direct_https_url, format_record_title

STARTED_MESSAGE = "Download runs in the background; check this notification for progress.\n\nWhen browsing during downloading, only downloaded recordings are visible."


def _identifier(*parts: str) -> str:
    return "/".join(quote(str(part), safe="-_.") for part in parts)


def _parse_identifier(identifier: str | None) -> list[str]:
    return identifier.split("/") if identifier else []


def _directory(title: str, identifier: str | None, children: list[BrowseMediaSource] | None = None,
               children_media_class: MediaClass = MediaClass.DIRECTORY) -> BrowseMediaSource:
    return BrowseMediaSource(domain=DOMAIN, identifier=identifier, title=title,
        media_class=MediaClass.DIRECTORY, media_content_type="", can_play=False,
        can_expand=True, children_media_class=children_media_class, children=children or [])


class EzvizCacheView(HomeAssistantView):
    """Serve cached media through Home Assistant's authenticated API, not /local/."""

    url = f"/api/{DOMAIN}/media/{{entry_id}}/{{day}}/{{key}}/{{kind}}"
    name = f"api:{DOMAIN}:media"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request, entry_id: str, day: str, key: str, kind: str):
        if kind not in {"video", "thumb"}:
            raise web.HTTPNotFound()
        source = self.hass.data.get(DOMAIN, {}).get("_media_source")
        data = self.hass.data.get(DOMAIN, {}).get(entry_id)
        if source is None or not data:
            raise web.HTTPNotFound()
        try:
            selected_day = date.fromisoformat(day)
        except ValueError as err:
            raise web.HTTPNotFound() from err
        path = source._cached_path(entry_id, selected_day, key) if kind == "video" else source._thumbnail_path(entry_id, selected_day, key)
        if not path.is_file():
            raise web.HTTPNotFound()
        content_type = "video/mp4" if kind == "video" else "image/jpeg"
        return web.FileResponse(path, headers={"Content-Type": content_type, "Cache-Control": "private, max-age=3600"})


class EzvizMediaSource(MediaSource):
    """Calendar-style browser and background downloader for EZVIZ SD-card records."""

    name = "EZVIZ SD Card Recordings"

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(DOMAIN)
        self.hass = hass
        self._downloads: dict[tuple[str, str, str], object] = {}
        self._syncing: set[str] = set()
        self._thumbnail_jobs: set[tuple[str, str, str]] = set()

    def _entry_data(self, entry_id: str) -> tuple[ConfigEntry, EzvizArchiveApi]:
        data = self.hass.data.get(DOMAIN, {}).get(entry_id)
        if not data or "entry" not in data:
            raise Unresolvable("The configured EZVIZ archive source is not loaded")
        return data["entry"], data["api"]

    def _root_path(self, entry: ConfigEntry) -> Path:
        cold_path = str(entry.options.get(CONF_COLD_STORAGE_PATH, "")).strip()
        if cold_path:
            return Path(cold_path).expanduser().resolve()
        # The HA-generated entry ID is unique per camera. Its token lives at
        # <entry_id>/token.json; cached media lives beside it in video/ and thumb/.
        return Path(self.hass.config.path(".storage", DOMAIN, entry.entry_id)).resolve()

    async def async_migrate_legacy_cache(self, entry: ConfigEntry) -> None:
        """Move cache files from the previous public www/ cache into private storage."""
        old_root = Path(self.hass.config.path("www", "ezviz_sd_card_recordings", entry.entry_id))
        new_root = self._root_path(entry) / "video"
        if old_root.exists():
            await self.hass.async_add_executor_job(_migrate_legacy_cache_tree, old_root, new_root)

    def _cached_path(self, entry_id: str, selected_day: date, key: str) -> Path:
        entry, _ = self._entry_data(entry_id)
        return self._root_path(entry) / "video" / f"{selected_day:%Y}" / f"{selected_day:%m}" / f"{selected_day:%d}" / f"{key}.mp4"

    def _thumbnail_path(self, entry_id: str, selected_day: date, key: str) -> Path:
        entry, _ = self._entry_data(entry_id)
        return self._root_path(entry) / "thumb" / f"{selected_day:%Y}" / f"{selected_day:%m}" / f"{selected_day:%d}" / f"{key}.jpg"

    def _media_url(self, entry_id: str, selected_day: date, key: str, kind: str) -> str:
        return CACHE_VIEW_URL.format(entry_id=quote(entry_id, safe=""), day=selected_day.isoformat(), key=quote(key, safe=""), kind=kind)

    async def async_browse_media(self, item: MediaSourceItem | None) -> BrowseMediaSource:
        """Browse cameras, calendar folders, then recording descriptors."""
        parts = _parse_identifier(item.identifier if item else None)
        entries = self.hass.data.get(DOMAIN, {})
        if not parts:
            children = [_directory(data["entry"].title, _identifier(entry_id))
                        for entry_id, data in entries.items() if isinstance(data, dict) and "entry" in data]
            return _directory(self.name, None, children)

        entry_id = parts[0]
        entry, api = self._entry_data(entry_id)
        try:
            today = datetime.now(ZoneInfo(self.hass.config.time_zone)).date()
        except (ZoneInfoNotFoundError, TypeError):
            today = datetime.now().date()
        now_year = today.year

        if len(parts) == 1:
            years = [_directory(str(year), _identifier(entry_id, "year", str(year))) for year in range(now_year, now_year - 5, -1)]
            return _directory(entry.title, item.identifier, years)

        if parts[1] == "year" and len(parts) == 3:
            year = _safe_int(parts[2], 2000, now_year + 1)
            last_month = today.month if year == now_year else 12
            months = list(range(1, last_month + 1))
            if entry.options.get(CONF_ORDER_DAYS, DEFAULT_ORDER_DAYS) == "descending":
                months.reverse()
            children = [_directory(f"{month:02d} — {_month_name(month)}", _identifier(entry_id, "month", f"{year:04d}-{month:02d}")) for month in months]
            return _directory(str(year), item.identifier, children)

        if parts[1] == "month" and len(parts) == 3:
            try:
                year, month = (int(part) for part in parts[2].split("-", 1))
                selected_month = date(year, month, 1)
            except (ValueError, TypeError) as err:
                raise BrowseError("Invalid month identifier") from err
            days_in_month = (date(year + (month == 12), (month % 12) + 1, 1) - selected_month).days
            last_day = min(days_in_month, today.day) if (year == today.year and month == today.month) else days_in_month
            days = list(range(1, last_day + 1))
            if entry.options.get(CONF_ORDER_DAYS, DEFAULT_ORDER_DAYS) == "descending":
                days.reverse()
            children = [_directory(f"{day:02d}", _identifier(entry_id, "day", f"{year:04d}-{month:02d}-{day:02d}")) for day in days]
            return _directory(selected_month.strftime("%B %Y"), item.identifier, children)

        if parts[1] == "day" and len(parts) == 3:
            try:
                selected_day = date.fromisoformat(parts[2])
            except ValueError as err:
                raise BrowseError("Invalid day identifier") from err
            try:
                records = await api.async_records_for_day(selected_day)
            except Exception as err:
                LOGGER.warning("Unable to list EZVIZ recordings for selected day (%s)", type(err).__name__)
                raise BrowseError("Unable to retrieve EZVIZ recordings for that day. Check logs and account settings.") from err
            records = sorted(records, key=lambda record: record["start"], reverse=entry.options.get(CONF_ORDER_RECORDINGS, DEFAULT_ORDER_RECORDINGS) == "descending")
            children = []
            for record in records:
                rec_id = _identifier(entry_id, "record", selected_day.isoformat(), record["key"])
                cached = self._cached_path(entry_id, selected_day, record["key"])
                thumbnail = self._thumbnail_path(entry_id, selected_day, record["key"])
                mark = "✓ " if cached.is_file() and cached.stat().st_size else ""
                thumb_job = (entry_id, selected_day.isoformat(), record["key"])
                if mark and not thumbnail.is_file() and thumb_job not in self._thumbnail_jobs:
                    self._thumbnail_jobs.add(thumb_job)
                    self.hass.async_create_task(self._async_ensure_thumbnail(cached, thumbnail, thumb_job))
                children.append(BrowseMediaSource(
                    domain=DOMAIN, identifier=rec_id, title=f"{mark}{format_record_title(record)}",
                    media_class=MediaClass.VIDEO, media_content_type=MediaType.VIDEO,
                    can_play=True, can_expand=False,
                    thumbnail=self._media_url(entry_id, selected_day, record["key"], "thumb") if thumbnail.is_file() else None,
                ))
            return _directory(selected_day.strftime("%A, %d %B %Y"), item.identifier, children, MediaClass.VIDEO)

        raise BrowseError("Unknown EZVIZ media folder")

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        """Play cached files; queue uncached recordings and notify instead of blocking the UI."""
        parts = _parse_identifier(item.identifier)
        if len(parts) != 4 or parts[1] != "record":
            raise Unresolvable("Select a recording to play")
        entry_id, _, day_text, key = parts
        try:
            selected_day = date.fromisoformat(day_text)
        except ValueError as err:
            raise Unresolvable("Invalid recording date") from err
        entry, api = self._entry_data(entry_id)
        record = await api.async_find_record(selected_day, key)
        if not record:
            raise Unresolvable("The recording descriptor expired from the archive list. Reopen the day and try again.")

        destination = self._cached_path(entry_id, selected_day, key)
        if destination.is_file() and destination.stat().st_size > 0:
            return PlayMedia(self._media_url(entry_id, selected_day, key, "video"), "video/mp4")

        task_key = (entry_id, selected_day.isoformat(), key)
        if task_key not in self._downloads:
            notification_id = self._notification_id(entry_id, key)
            self._update_download_notification(entry, record, notification_id, state="starting")
            task = self.hass.async_create_task(self._async_download_record(entry, api, record, destination, selected_day, notification_id=notification_id))
            self._downloads[task_key] = task
            task.add_done_callback(lambda _task, selected_key=task_key: self._downloads.pop(selected_key, None))
        raise Unresolvable(STARTED_MESSAGE)

    @staticmethod
    def _notification_id(entry_id: str, key: str) -> str:
        return f"{NOTIFICATION_PREFIX}_{entry_id}_{key}"

    @callback
    def _update_download_notification(
        self, entry: ConfigEntry, record: dict, notification_id: str,
        state: str = "downloading", downloaded: int = 0, total: int | None = None,
    ) -> None:
        """Render Tapo-style notification updates on Home Assistant's event loop."""
        start = record["start"]
        end = record["end"]
        if end.date() == start.date():
            interval = f"{start:%Y-%m-%d %H:%M:%S} - {end:%H:%M:%S}"
        else:
            interval = f"{start:%Y-%m-%d %H:%M:%S} - {end:%Y-%m-%d %H:%M:%S}"

        if state == "starting":
            status = "Downloading..."
            message = f"{entry.title} - {interval}\n\n{status}\n\n{STARTED_MESSAGE}"
            title = f"{entry.title}: Downloading..."
        elif state == "progress":
            if total and total > 0:
                percent = min(100, int(downloaded * 100 / total))
                progress = f"Downloading... {percent}% ({_format_size(downloaded)} / {_format_size(total)})"
            else:
                progress = f"Downloading... ({_format_size(downloaded)} received)"
            message = f"{entry.title} - {interval}\n\n{progress}\n\n{STARTED_MESSAGE}"
            title = f"{entry.title}: Downloading..."
        elif state == "success":
            message = (
                f"{entry.title} - {interval}\n\nDownload media success.\n\n"
                f"Recording downloaded and cached successfully ({_format_size(downloaded)}). "
                "Return to Media Browser and select it again to play."
            )
            title = f"{entry.title}: Download media success"
        else:
            message = f"{entry.title} - {interval}\n\nDownload media failed. Check Home Assistant logs for details."
            title = f"{entry.title}: Download failed"
        persistent_notification.async_create(
            self.hass, message, title=title, notification_id=notification_id,
        )

    async def _async_download_record(self, entry: ConfigEntry, api: EzvizArchiveApi, record: dict,
                                     destination: Path, selected_day: date, notification_id: str | None = None) -> bool:
        """Download one clip, generate its thumbnail, and update persistent status."""
        try:
            def progress(downloaded: int, total: int | None) -> None:
                if notification_id:
                    self.hass.add_job(
                        self._update_download_notification, entry, record, notification_id,
                        "progress", downloaded, total,
                    )

            url = direct_https_url(record["raw"])
            if url:
                await self.hass.async_add_executor_job(self._download_https, url, destination, progress)
            else:
                await api.async_download_native_record(
                    record, destination, progress_callback=progress if notification_id else None
                )
            if not destination.is_file() or destination.stat().st_size == 0:
                raise RuntimeError("EZVIZ returned an empty recording")
            try:
                os.chmod(destination, 0o600)
                os.chmod(destination.parent, 0o700)
            except OSError:
                pass
            thumbnail = self._thumbnail_path(entry.entry_id, selected_day, record["key"])
            await self.hass.async_add_executor_job(self._generate_thumbnail, destination, thumbnail)
            try:
                if thumbnail.is_file():
                    os.chmod(thumbnail, 0o600)
                    os.chmod(thumbnail.parent, 0o700)
            except OSError:
                pass
            self.hass.bus.async_fire(MEDIA_EVENT_DOWNLOADED, {
                "entry_id": entry.entry_id, "date": selected_day.isoformat(),
                "record_key": record["key"], "file_path": str(destination),
                "thumbnail_path": str(thumbnail),
            })
            LOGGER.info("EZVIZ recording cached for %s (%d bytes)", entry.title, destination.stat().st_size)
            if notification_id:
                self._update_download_notification(
                    entry, record, notification_id, state="success", downloaded=destination.stat().st_size,
                )
            return True
        except Exception as err:
            destination.unlink(missing_ok=True)
            LOGGER.warning("EZVIZ recording download failed (%s)", type(err).__name__, exc_info=True)
            if notification_id:
                self._update_download_notification(entry, record, notification_id, state="failed")
            return False

    @staticmethod
    def _download_https(url: str, destination: Path, progress_callback=None) -> None:
        """Stream an explicitly returned media URL into cache without partial files."""
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".ezviz-", suffix=".part", dir=destination.parent)
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            with requests.get(url, stream=True, timeout=(15, 120), allow_redirects=True) as response:
                response.raise_for_status()
                if not response.url.startswith("https://"):
                    raise ValueError("Refusing a media download redirected away from HTTPS")
                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) > 2 * 1024 * 1024 * 1024:
                    raise ValueError("The media descriptor exceeds the 2 GiB per-file safety limit")
                content_total = int(content_length) if content_length and content_length.isdigit() else None
                if progress_callback:
                    progress_callback(0, content_total)
                downloaded = 0
                with tmp.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        downloaded += len(chunk)
                        if downloaded > 2 * 1024 * 1024 * 1024:
                            raise ValueError("The media descriptor exceeds the 2 GiB per-file safety limit")
                        handle.write(chunk)
                        if progress_callback:
                            progress_callback(downloaded, content_total)
            if tmp.stat().st_size == 0:
                raise ValueError("EZVIZ returned an empty media file")
            os.replace(tmp, destination)
        finally:
            tmp.unlink(missing_ok=True)

    async def _async_ensure_thumbnail(self, video: Path, thumbnail: Path, job_key: tuple[str, str, str]) -> None:
        """Generate a missing thumbnail for a previously cached recording."""
        try:
            await self.hass.async_add_executor_job(self._generate_thumbnail, video, thumbnail)
        finally:
            self._thumbnail_jobs.discard(job_key)

    @staticmethod
    def _generate_thumbnail(video: Path, thumbnail: Path) -> None:
        """Extract a small JPEG preview from the cached MP4 using FFmpeg."""
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            LOGGER.debug("FFmpeg unavailable; skipping EZVIZ thumbnail generation")
            return
        thumbnail.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(thumbnail.parent, 0o700)
        except OSError:
            pass
        temp = thumbnail.with_suffix(".tmp.jpg")
        try:
            result = subprocess.run(
                [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-ss", "1", "-i", str(video),
                 "-frames:v", "1", "-vf", "scale=320:-1", "-q:v", "4", str(temp)],
                check=False, capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0 and temp.is_file() and temp.stat().st_size:
                os.replace(temp, thumbnail)
            else:
                LOGGER.debug("Could not generate EZVIZ thumbnail: %s", (result.stderr or "").strip()[:300])
        except (OSError, subprocess.SubprocessError) as err:
            LOGGER.debug("Could not generate EZVIZ thumbnail (%s)", type(err).__name__)
        finally:
            temp.unlink(missing_ok=True)

    async def async_sync_all(self) -> None:
        """Synchronize recent recordings for entries with sync enabled."""
        for entry_id, data in list(self.hass.data.get(DOMAIN, {}).items()):
            if not isinstance(data, dict) or "entry" not in data:
                continue
            entry_options = data["entry"].options
            if entry_options.get(CONF_MEDIA_SYNC, DEFAULT_MEDIA_SYNC) == "on":
                await self.async_sync_entry(entry_id)

    async def async_sync_entry(self, entry_id: str) -> None:
        """Cache recordings in the configured rolling window and prune expired cache.

        The loop tolerates per-day failures: if one day fails (camera busy,
        cloud index lagging for today, or the day is in api.py's cooldown
        window), the loop logs and continues with the remaining days instead
        of aborting the whole sync window. After the loop, cached files whose
        recording start is older than the rolling cutoff are removed so that
        a 24-hour sync truly keeps only ~24 hours on disk instead of growing
        without bound.
        """
        if entry_id in self._syncing:
            return
        self._syncing.add(entry_id)
        try:
            entry, api = self._entry_data(entry_id)
            if entry.options.get(CONF_MEDIA_SYNC, DEFAULT_MEDIA_SYNC) != "on":
                return
            hours = int(entry.options.get(CONF_SYNC_HOURS, 24))
            if hours <= 0:
                hours = 24
            try:
                tz = ZoneInfo(self.hass.config.time_zone)
            except (ZoneInfoNotFoundError, TypeError):
                tz = datetime.now().astimezone().tzinfo
            now = datetime.now(tz)
            cutoff = now - timedelta(hours=hours)
            first_day = cutoff.date()
            day = first_day
            attempted = completed = 0
            skipped_days: list[date] = []

            while day <= now.date():
                try:
                    records = await api.async_records_for_day(day)
                except RuntimeError as err:
                    # A single failed day must not abort the whole sync.
                    # The camera may be busy, the cloud index may lag for
                    # today, or the day may be in api.py's cooldown window.
                    skipped_days.append(day)
                    LOGGER.debug(
                        "EZVIZ media sync skipped %s for %s: %s",
                        day, entry.title, err,
                    )
                    day += timedelta(days=1)
                    continue

                for record in records:
                    if record["end"] < cutoff or record["start"] > now:
                        continue
                    destination = self._cached_path(entry_id, day, record["key"])
                    if destination.is_file() and destination.stat().st_size:
                        continue
                    attempted += 1
                    if await self._async_download_record(entry, api, record, destination, day):
                        completed += 1
                day += timedelta(days=1)

            # Prune cached media whose recording start is outside the rolling
            # window. This is what makes "24 hours" mean 24 hours on disk, not
            # an ever-growing pile of recordings.
            removed_videos, removed_thumbs = await self._async_purge_old_cache(entry_id, cutoff)

            if attempted:
                message = (
                    f"Recent-recording synchronization finished. Cached "
                    f"{completed} of {attempted} missing recordings in the "
                    f"last {hours} hours."
                )
            elif skipped_days:
                message = (
                    f"Recent-recording synchronization finished without new "
                    f"downloads. {len(skipped_days)} day(s) could not be "
                    "queried: "
                    + ", ".join(str(d) for d in skipped_days)
                    + ". The camera may be busy or the cloud index is not "
                    "ready yet."
                )
            else:
                message = (
                    f"Recent-recording synchronization finished. No new "
                    f"recordings in the last {hours} hours."
                )
            if removed_videos or removed_thumbs:
                message += (
                    f" Pruned {removed_videos} expired recording(s) and "
                    f"{removed_thumbs} thumbnail(s) outside the "
                    f"{hours}-hour window."
                )
                LOGGER.info(
                    "EZVIZ cache pruned for %s: %d video(s), %d thumbnail(s)",
                    entry.title, removed_videos, removed_thumbs,
                )

            notification_id = f"{DOMAIN}_sync_{entry_id}"
            if entry.options.get(CONF_SYNC_NOTIFY, DEFAULT_SYNC_NOTIFY) == "on":
                persistent_notification.async_create(
                    self.hass,
                    message,
                    title=f"{entry.title}: media synchronization",
                    notification_id=notification_id,
                )
            else:
                LOGGER.debug(
                    "EZVIZ sync summary for %s suppressed (notifications off): %s",
                    entry.title, message,
                )
                # Dismiss any stale notification from a previous run so the
                # toggle feels immediate for the user.
                try:
                    persistent_notification.async_dismiss(self.hass, notification_id)
                except Exception:
                    pass
        except Exception as err:
            LOGGER.warning(
                "EZVIZ recent media synchronization failed (%s)",
                type(err).__name__,
                exc_info=True,
            )
        finally:
            self._syncing.discard(entry_id)

    async def _async_purge_old_cache(self, entry_id: str, cutoff: datetime) -> tuple[int, int]:
        """Remove cached videos and thumbnails older than the rolling cutoff.

        Folders strictly older than ``cutoff.date()`` are removed wholesale.
        The cutoff day itself is pruned file-by-file using the HHMMSS prefix
        of the cached filename (``HHMMSS_HHMMSS_hash8.mp4``), so a 24-hour
        window at 14:23 only keeps files from yesterday 14:23 onwards. Files
        whose names do not match that pattern fall back to their mtime, which
        cleans up pre-v0.4 orphans without touching recently downloaded clips.

        Empty year/month/day folders are pruned on the way out.
        """
        entry, _ = self._entry_data(entry_id)
        root = self._root_path(entry)
        cutoff_date = cutoff.date()
        cutoff_ts = cutoff.timestamp()
        zone = cutoff.tzinfo

        def _purge() -> tuple[int, int]:
            removed_videos = 0
            removed_thumbs = 0
            for kind, ext in (("video", ".mp4"), ("thumb", ".jpg")):
                base = root / kind
                if not base.exists():
                    continue
                try:
                    year_dirs = sorted(p for p in base.iterdir() if p.is_dir())
                except OSError:
                    continue
                for year_dir in year_dirs:
                    try:
                        year = int(year_dir.name)
                    except ValueError:
                        continue
                    try:
                        month_dirs = sorted(p for p in year_dir.iterdir() if p.is_dir())
                    except OSError:
                        continue
                    for month_dir in month_dirs:
                        try:
                            month = int(month_dir.name)
                        except ValueError:
                            continue
                        try:
                            day_dirs = sorted(p for p in month_dir.iterdir() if p.is_dir())
                        except OSError:
                            continue
                        for day_dir in day_dirs:
                            try:
                                day = int(day_dir.name)
                                folder_date = date(year, month, day)
                            except ValueError:
                                continue

                            # Fast path: entire folder is older than cutoff.
                            if folder_date < cutoff_date:
                                try:
                                    files = [f for f in day_dir.rglob("*") if f.is_file()]
                                except OSError:
                                    files = []
                                if kind == "video":
                                    removed_videos += len(files)
                                else:
                                    removed_thumbs += len(files)
                                shutil.rmtree(day_dir, ignore_errors=True)
                                continue

                            # Boundary day: prune individual files.
                            if folder_date == cutoff_date:
                                try:
                                    files = list(day_dir.glob(f"*{ext}"))
                                except OSError:
                                    files = []
                                for file in files:
                                    should_delete = False
                                    try:
                                        hh = int(file.name[0:2])
                                        mm = int(file.name[2:4])
                                        ss = int(file.name[4:6])
                                        start_dt = datetime(year, month, day, hh, mm, ss, tzinfo=zone)
                                        if start_dt < cutoff:
                                            should_delete = True
                                    except (ValueError, IndexError):
                                        # Unparseable filename (e.g. legacy
                                        # hash-only naming); fall back to mtime.
                                        try:
                                            if file.stat().st_mtime < cutoff_ts:
                                                should_delete = True
                                        except OSError:
                                            continue
                                    if not should_delete:
                                        continue
                                    try:
                                        file.unlink()
                                        if kind == "video":
                                            removed_videos += 1
                                        else:
                                            removed_thumbs += 1
                                    except OSError as err:
                                        LOGGER.debug("Could not remove %s: %s", file, err)
                                try:
                                    if not any(day_dir.iterdir()):
                                        day_dir.rmdir()
                                except OSError:
                                    pass

                        try:
                            if not any(month_dir.iterdir()):
                                month_dir.rmdir()
                        except OSError:
                            pass
                    try:
                        if not any(year_dir.iterdir()):
                            year_dir.rmdir()
                    except OSError:
                        pass
            return removed_videos, removed_thumbs

        try:
            return await self.hass.async_add_executor_job(_purge)
        except Exception as err:
            LOGGER.warning("EZVIZ cache purge failed (%s)", type(err).__name__, exc_info=True)
            return 0, 0


async def async_get_media_source(hass: HomeAssistant) -> EzvizMediaSource:
    """Return the shared EZVIZ media source instance."""
    source = hass.data.setdefault(DOMAIN, {}).get("_media_source")
    if source is None:
        source = EzvizMediaSource(hass)
        hass.data[DOMAIN]["_media_source"] = source
    return source


def _migrate_legacy_cache_tree(old_root: Path, video_root: Path) -> None:
    """Move old <year>/<month>/<day> MP4 files into the private video/ folder."""
    if not old_root.exists():
        return
    video_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    for source in old_root.rglob("*"):
        if not source.is_file() or source.suffix.lower() != ".mp4":
            continue
        relative = source.relative_to(old_root)
        target = video_root / relative
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not target.exists():
            shutil.move(str(source), str(target))
            try:
                os.chmod(target, 0o600)
                os.chmod(target.parent, 0o700)
            except OSError:
                pass
        else:
            source.unlink()
    shutil.rmtree(old_root, ignore_errors=True)


def _safe_int(value: str, minimum: int, maximum: int) -> int:
    result = int(value)
    if not minimum <= result <= maximum:
        raise BrowseError("Calendar year is outside the supported range")
    return result


def _month_name(month: int) -> str:
    return date(2000, month, 1).strftime("%B")


def _format_size(size: int) -> str:
    """Format download progress sizes with readable binary units."""
    value = max(0.0, float(size))
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"
