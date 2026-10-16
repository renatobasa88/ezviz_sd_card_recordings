"""EZVIZ SD Card Recordings integration."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_call_later, async_track_time_interval

from .api import EzvizArchiveApi
from .const import DOMAIN, LOGGER
from .media_source import EzvizCacheView, async_get_media_source

PLATFORMS: list[str] = []


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the integration domain, private cache endpoint, and sync scheduler."""
    hass.data.setdefault(DOMAIN, {})
    source = await async_get_media_source(hass)
    hass.http.register_view(EzvizCacheView(hass))

    async def _periodic_sync(_now) -> None:
        await source.async_sync_all()

    hass.data[DOMAIN]["_sync_unsub"] = async_track_time_interval(
        hass, _periodic_sync, timedelta(minutes=15)
    )
    async_call_later(hass, 30, lambda _now: hass.async_create_task(source.async_sync_all()))
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a configured EZVIZ archive source."""
    hass.data.setdefault(DOMAIN, {})
    api = EzvizArchiveApi(hass, entry)
    try:
        await api.async_prepare()
    except Exception:
        LOGGER.exception("Could not prepare EZVIZ archive API for %s", entry.entry_id)
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
