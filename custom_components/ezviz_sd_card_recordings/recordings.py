"""Pure helpers for normalizing EZVIZ SD-card descriptors."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone, tzinfo
from typing import Any, Iterator

_START_KEYS = (
    "startTime", "start_time", "startTimestamp", "start_timestamp",
    "beginTime", "begin_time", "begin", "start", "startDate",
)
_END_KEYS = (
    "stopTime", "stop_time", "endTime", "end_time", "endTimestamp",
    "end_timestamp", "stop", "end", "endDate",
)


def _walk(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _parse_datetime(value: Any, local_timezone: tzinfo | None = None) -> datetime | None:
    if value is None or isinstance(value, bool):
        return None
    if local_timezone is None:
        local_timezone = datetime.now().astimezone().tzinfo or timezone.utc
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
        digits = str(value).strip()
        # Compact wall-clock timestamps are sometimes returned as YYYYMMDDHHMMSS.
        if isinstance(value, str) and len(digits) == 14 and digits[:4] in ("2024", "2025", "2026", "2027", "2028", "2029"):
            try:
                return datetime.strptime(digits, "%Y%m%d%H%M%S").replace(tzinfo=local_timezone)
            except ValueError:
                pass
        number = float(value)
        if number > 100_000_000_000:  # millisecond epoch
            number /= 1000
        if number > 100_000_000:
            try:
                return datetime.fromtimestamp(number, tz=timezone.utc).astimezone(local_timezone)
            except (OverflowError, OSError, ValueError):
                return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    for candidate in (text, text.replace("/", "-")):
        try:
            parsed = datetime.fromisoformat(candidate)
            if parsed.tzinfo is None:
                # EZVIZ responses commonly contain local, offset-free wall time.
                parsed = parsed.replace(tzinfo=local_timezone)
            return parsed.astimezone(local_timezone)
        except ValueError:
            pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y%m%d%H%M%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=local_timezone).astimezone(local_timezone)
        except ValueError:
            continue
    return None


def _first(record: dict[str, Any], names: tuple[str, ...]) -> Any:
    for key in names:
        if key in record and record[key] not in (None, ""):
            return record[key]
    return None


def record_key(record: dict[str, Any]) -> str:
    """Stable opaque key for a descriptor; doesn't expose URLs in HA identifiers."""
    # Remove URL/ticket/auth fields because these may be short-lived and change
    # between two otherwise identical archive queries.
    volatile_terms = ("url", "uri", "ticket", "token", "expires", "signature", "auth")

    def stable(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): stable(child)
                for key, child in sorted(value.items(), key=lambda pair: str(pair[0]))
                if not any(term in str(key).lower() for term in volatile_terms)
            }
        if isinstance(value, list):
            return [stable(child) for child in value]
        return value

    serialized = json.dumps(stable(record), sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(serialized.encode()).hexdigest()[:20]



def resolve_api_serial(configured_serial: str, available_serials: Any) -> str:
    """Map a full EZVIZ sticker serial or short API serial to an account serial.

    EZVIZ accounts may expose a short API serial such as ``BG4091592`` while
    configuration uses the full camera serial ending in that value.
    """
    requested = str(configured_serial).strip().upper()
    available = [str(serial).strip() for serial in available_serials if str(serial).strip()]
    exact = [serial for serial in available if serial.upper() == requested]
    if len(exact) == 1:
        return exact[0]

    matches = [
        serial for serial in available
        if requested.endswith(serial.upper()) or serial.upper().endswith(requested)
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ValueError(
            "The configured camera serial does not match any device serial returned by EZVIZ. "
            "Edit the integration and enter the short API serial (for example BG4091592) "
            "or the full serial printed on the camera."
        )
    raise ValueError(
        "The configured camera serial matches more than one EZVIZ device; enter its exact short API serial."
    )

def serial_identity(configured_serial: str) -> str:
    """Normalize full sticker serials and short API serials to a dedupe key.

    EZVIZ stickers can contain the short account/API serial as their final
    nine characters, e.g. ``...CCRRBC8610065`` and ``BC8610065``. Using the
    suffix prevents adding the same camera twice with two serial formats.
    """
    import re

    requested = str(configured_serial).strip().upper()
    match = re.search(r"([A-Z]{2}\d{7})$", requested)
    return match.group(1) if match else requested


def normalize_records(payload: Any, local_timezone: tzinfo | None = None) -> list[dict[str, Any]]:
    """Find record-like objects in diverse CLI JSON wrappers and normalize times."""
    found: dict[str, dict[str, Any]] = {}
    for record in _walk(payload):
        start = _parse_datetime(_first(record, _START_KEYS), local_timezone)
        end = _parse_datetime(_first(record, _END_KEYS), local_timezone)
        if start is None:
            continue
        # Some API results expose a start only. Keep these records visible, with
        # zero duration, instead of silently dropping potentially useful metadata.
        if end is None:
            end = start
        if end < start:
            continue
        key = record_key(record)
        found[key] = {
            "key": key,
            "start": start,
            "end": end,
            "raw": record,
        }
    return sorted(found.values(), key=lambda item: item["start"])



def format_native_playback_timestamp(value: datetime, local_timezone: tzinfo | None = None) -> str:
    """Format a camera/API wall-clock timestamp for native EZVIZ VTM playback.

    EZVIZ's playback endpoint uses the local wall-clock components supplied by its
    record-list API. Although its compact wire format ends in ``Z``, converting the
    timestamp to UTC makes playback fail on tested regional cameras. When a timezone
    is provided, first represent the instant in that local timezone, then format the
    wall-clock fields without another UTC conversion.
    """
    if local_timezone is not None:
        if value.tzinfo is None:
            value = value.replace(tzinfo=local_timezone)
        else:
            value = value.astimezone(local_timezone)
    return value.strftime("%Y%m%dT%H%M%SZ")

def format_record_title(record: dict[str, Any]) -> str:
    """Format one descriptor as a readable local-time interval."""
    start: datetime = record["start"]
    end: datetime = record["end"]
    if end.date() == start.date():
        return f"{start:%H:%M:%S}–{end:%H:%M:%S}"
    return f"{start:%Y-%m-%d %H:%M:%S}–{end:%Y-%m-%d %H:%M:%S}"


def direct_https_url(value: Any) -> str | None:
    """Return a direct HTTPS media URL if a descriptor has one.

    EZVIZ's SD-card API often returns native playback descriptors instead of a
    URL. The generic ``url`` field is intentionally ignored to avoid treating
    thumbnails, help links, or unrelated API URLs as downloadable videos.
    """
    allowed_terms = ("download", "media", "video", "record", "file", "playback")
    excluded_terms = ("thumb", "snapshot", "image", "photo", "preview")

    if isinstance(value, dict):
        for key, child in value.items():
            key_lower = str(key).lower().replace("_", "")
            if (
                isinstance(child, str)
                and child.strip().startswith("https://")
                and any(term in key_lower for term in allowed_terms)
                and not any(term in key_lower for term in excluded_terms)
                and any(term in key_lower for term in ("url", "uri", "link"))
            ):
                return child.strip()
            nested = direct_https_url(child)
            if nested:
                return nested
    elif isinstance(value, list):
        for child in value:
            nested = direct_https_url(child)
            if nested:
                return nested
    return None
