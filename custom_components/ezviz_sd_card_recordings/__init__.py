"""EZVIZ SD Card Recordings integration."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.loader import async_get_integration

try:  # HA >= 2024.6
    from homeassistant.components.http import StaticPathConfig
except ImportError:  # pragma: no cover - fallback for older HA
    StaticPathConfig = None  # type: ignore[assignment]

from .api import EzvizArchiveApi
from .const import DOMAIN, LOGGER
from .media_source import EzvizCacheView, async_get_media_source

PLATFORMS: list[str] = []

# URL prefix used to expose files from this integration folder.
# The final card URL becomes: /ezviz_sd_card_recordings/ezviz-timeline-card.js
CARD_URL_PATH = "/ezviz_sd_card_recordings"
CARD_JS_FILENAME = "ezviz-timeline-card.js"


async def _async_register_card(hass: HomeAssistant) -> None:
    """Expose the bundled Lovelace card and auto-load it in the frontend."""
    if hass.data.get(DOMAIN, {}).get("_card_registered"):
        return

    card_dir = Path(__file__).parent
    card_file = card_dir / CARD_JS_FILENAME
    if not card_file.is_file():
        LOGGER.warning(
            "Timeline card file not found at %s; card auto-load skipped",
            card_file,
        )
        return

    try:
        if StaticPathConfig is not None and hasattr(
            hass.http, "async_register_static_paths"
        ):
            await hass.http.async_register_static_paths(
                [StaticPathConfig(CARD_URL_PATH, str(card_dir), False)]
            )
        else:  # HA < 2024.6
            hass.http.register_static_path(CARD_URL_PATH, str(card_dir), False)
    except Exception:
        LOGGER.exception("Could not register static path for the EZVIZ timeline card")
        return

    try:
        integration = await async_get_integration(hass, DOMAIN)
        version = integration.version or "0"
    except Exception:  # pragma: no cover - very defensive
        version = "0"

    add_extra_js_url(hass, f"{CARD_URL_PATH}/{CARD_JS_FILENAME}?v={version}")
    hass.data.setdefault(DOMAIN, {})["_card_registered"] = True
    LOGGER.info(
        "EZVIZ timeline card available at %s/%s (v%s)",
        CARD_URL_PATH,
        CARD_JS_FILENAME,
        version,
    )


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the integration domain, private cache endpoint, and sync scheduler."""
    hass.data.setdefault(DOMAIN, {})
    source = await async_get_media_source(hass)
    hass.http.register_view(EzvizCacheView(hass))

    await _async_register_card(hass)

    async def _periodic_sync(_now) -> None:
        await source.async_sync_all()

    hass.data[DOMAIN]["_sync_unsub"] = async_track_time_interval(
        hass, _periodic_sync, timedelta(minutes=15)
    )
    async_call_later(
        hass, 30, lambda _now: hass.async_create_task(source.async_sync_all())
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a configured EZVIZ archive source."""
    hass.data.setdefault(DOMAIN, {})
    api = EzvizArchiveApi(hass, entry)
    try:
        await api.async_prepare()
    except Exception:
        LOGGER.exception(
            "Could not prepare EZVIZ archive API for %s", entry.entry_id
        )
        raise

    source = await async_get_media_source(hass)
    await source.async_migrate_legacy_cache(entry)
    hass.data[DOMAIN][entry.entry_id] = {"entry": entry, "api": api}
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload after options/config updates."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload an entry."""
    data = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if data:
        return True
    return True


async def async_unload(hass: HomeAssistant) -> None:
    """Stop periodic synchronization when the integration is unloaded."""
    unsubscribe = hass.data.get(DOMAIN, {}).pop("_sync_unsub", None)
    if unsubscribe:
        unsubscribe()
