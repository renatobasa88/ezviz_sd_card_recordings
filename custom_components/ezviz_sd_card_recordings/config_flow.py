"""Config and options flows for EZVIZ SD Card Recordings."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_CAMERA_NAME,
    CONF_CHANNEL,
    CONF_COLD_STORAGE_PATH,
    CONF_ORDER_DAYS,
    CONF_ORDER_RECORDINGS,
    CONF_MEDIA_SYNC,
    CONF_SYNC_MODE,
    CONF_REGION,
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_SERIAL,
    CONF_SESSION_TOKEN,
    CONF_SYNC_HOURS,
    DEFAULT_CHANNEL,
    DEFAULT_COLD_STORAGE_PATH,
    DEFAULT_ORDER_DAYS,
    DEFAULT_ORDER_RECORDINGS,
    DEFAULT_MEDIA_SYNC,
    DEFAULT_REGION,
    DEFAULT_SYNC_HOURS,
    DOMAIN,
    ERROR_AUTH,
    ERROR_CANNOT_CONNECT,
    ERROR_MFA,
    ERROR_UNKNOWN,
    LOGGER,
)
from .recordings import serial_identity

REGION_OPTIONS = [
    {"label": "Singapore — apiisgp.ezvizlife.com", "value": "apiisgp.ezvizlife.com"},
    {"label": "Asia — apiias.ezvizlife.com", "value": "apiias.ezvizlife.com"},
    {"label": "Europe — apiieu.ezvizlife.com", "value": "apiieu.ezvizlife.com"},
    {"label": "South America — apiisa.ezvizlife.com", "value": "apiisa.ezvizlife.com"},
    {"label": "North America / US — apiius.ezvizlife.com", "value": "apiius.ezvizlife.com"},
    {"label": "India — apiiindia.ezvizlife.com", "value": "apiiindia.ezvizlife.com"},
    {"label": "China — apiicn.ezvizlife.com", "value": "apiicn.ezvizlife.com"},
    {"label": "Russia — apirus.ezvizru.com", "value": "apirus.ezvizru.com"},
    {"label": "Other region — enter hostname manually", "value": "custom"},
]
_ORDER_OPTIONS = [
    {"label": "Ascending", "value": "ascending"},
    {"label": "Descending", "value": "descending"},
]
_REGION_PATTERN = re.compile(r"^api[a-z0-9-]+\.ezvizlife\.com$", re.IGNORECASE)


def _valid_region(region: str) -> bool:
    """Only accept an EZVIZ API hostname, never a URL/path supplied by user input."""
    return bool(_REGION_PATTERN.fullmatch(region.strip()))


def _authenticate(username: str, password: str, region: str, sms_code: str | None = None) -> dict:
    """Validate credentials and return the pyEzvizApi session token."""
    from pyezvizapi.client import EzvizClient

    client = EzvizClient(account=username, password=password, url=region, timeout=20)
    try:
        if sms_code:
            client.login(sms_code=int(sms_code))
        else:
            client.login()
        token = getattr(client, "_token", None)
        if not isinstance(token, dict) or not token.get("session_id"):
            raise ValueError("EZVIZ login succeeded but no session token was returned")
        return dict(token)
    finally:
        try:
            client.close_session()
        except Exception:
            pass


class EzvizConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle integration setup."""

    VERSION = 1

    def __init__(self) -> None:
        self._pending: dict | None = None

    async def async_step_user(self, user_input: dict | None = None):
        """Collect EZVIZ credentials and camera details."""
        errors: dict[str, str] = {}
        if user_input is not None:
            serial = str(user_input[CONF_SERIAL]).strip().upper()
            region = str(user_input[CONF_REGION]).strip()
            username = str(user_input[CONF_USERNAME]).strip()
            password = str(user_input[CONF_PASSWORD])
            camera_name = str(user_input.get(CONF_CAMERA_NAME) or serial).strip()
            channel = int(user_input[CONF_CHANNEL])
            self._pending = {
                CONF_USERNAME: username, CONF_PASSWORD: password, CONF_REGION: region,
                CONF_SERIAL: serial, CONF_CAMERA_NAME: camera_name, CONF_CHANNEL: channel,
            }
            if region == "custom":
                return self.async_show_form(step_id="region", data_schema=vol.Schema({vol.Required("custom_region"): str}), errors={})
            if not _valid_region(region):
                errors[CONF_REGION] = ERROR_UNKNOWN
            else:
                return await self._async_authenticate_pending()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_USERNAME): str,
                vol.Required(CONF_PASSWORD): str,
                vol.Required(CONF_REGION, default=DEFAULT_REGION): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=REGION_OPTIONS, mode=selector.SelectSelectorMode.DROPDOWN)
                ),
                vol.Required(CONF_SERIAL): str,
                vol.Optional(CONF_CAMERA_NAME): str,
                vol.Required(CONF_CHANNEL, default=DEFAULT_CHANNEL): vol.All(vol.Coerce(int), vol.Range(min=1, max=32)),
            }),
            errors=errors,
        )

    async def async_step_region(self, user_input: dict | None = None):
        """Ask for the hostname when the user selected a custom region."""
        if not self._pending:
            return await self.async_step_user()
        errors: dict[str, str] = {}
        if user_input is not None:
            region = str(user_input.get("custom_region", "")).strip().lower()
            if not _valid_region(region):
                errors["custom_region"] = ERROR_UNKNOWN
            else:
                self._pending[CONF_REGION] = region
                return await self._async_authenticate_pending()
        return self.async_show_form(step_id="region", data_schema=vol.Schema({vol.Required("custom_region"): str}), errors=errors)

    async def _async_authenticate_pending(self):
        """Set normalized identity and authenticate the pending configuration."""
        assert self._pending is not None
        username = self._pending[CONF_USERNAME]
        serial = self._pending[CONF_SERIAL]
        await self.async_set_unique_id(f"{username.lower()}-{serial_identity(serial)}")
        self._abort_if_unique_id_configured()
        try:
            token = await self.hass.async_add_executor_job(
                _authenticate, username, self._pending[CONF_PASSWORD], self._pending[CONF_REGION]
            )
            return self._create_entry(token)
        except Exception as err:
            if "EzvizAuthVerificationCode" in type(err).__name__ or "MFA enabled" in str(err):
                return await self.async_step_mfa()
            if "Incorrect Username" in str(err) or "Incorrect Password" in str(err):
                errors = {"base": ERROR_AUTH}
            elif isinstance(err, (TimeoutError, ConnectionError, OSError)):
                errors = {"base": ERROR_CANNOT_CONNECT}
            else:
                LOGGER.debug("EZVIZ authentication failed: %s", type(err).__name__)
                errors = {"base": ERROR_UNKNOWN}
            region_default = self._pending[CONF_REGION]
            if region_default not in {option["value"] for option in REGION_OPTIONS}:
                region_default = "custom"
            return self.async_show_form(
                step_id="user",
                data_schema=vol.Schema({
                    vol.Required(CONF_USERNAME, default=username): str,
                    vol.Required(CONF_PASSWORD): str,
                    vol.Required(CONF_REGION, default=region_default): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=REGION_OPTIONS, mode=selector.SelectSelectorMode.DROPDOWN)
                    ),
                    vol.Required(CONF_SERIAL, default=serial): str,
                    vol.Optional(CONF_CAMERA_NAME, default=self._pending[CONF_CAMERA_NAME]): str,
                    vol.Required(CONF_CHANNEL, default=self._pending[CONF_CHANNEL]): vol.All(vol.Coerce(int), vol.Range(min=1, max=32)),
                }), errors=errors,
            )

    async def async_step_mfa(self, user_input: dict | None = None):
        """Ask for the EZVIZ MFA code if the account requires it."""
        errors: dict[str, str] = {}
        if not self._pending:
            return await self.async_step_user()
        if user_input is not None:
            try:
                token = await self.hass.async_add_executor_job(
                    _authenticate, self._pending[CONF_USERNAME], self._pending[CONF_PASSWORD],
                    self._pending[CONF_REGION], str(user_input["sms_code"]).strip(),
                )
                return self._create_entry(token)
            except Exception as err:
                LOGGER.debug("EZVIZ MFA verification failed: %s", type(err).__name__)
                errors["base"] = ERROR_MFA
        return self.async_show_form(
            step_id="mfa", data_schema=vol.Schema({vol.Required("sms_code"): str}), errors=errors,
            description_placeholders={"username": self._pending[CONF_USERNAME]},
        )

    @callback
    def _create_entry(self, token: dict):
        """Create the config entry after successful login."""
        assert self._pending is not None
        data = {**self._pending, CONF_SESSION_TOKEN: token}
        return self.async_create_entry(title=self._pending[CONF_CAMERA_NAME], data=data)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Create an options flow using Home Assistant's managed config_entry.

        Recent Home Assistant releases assign ``config_entry`` to OptionsFlow
        themselves, so the flow must not try to set that read-only property.
        """
        return EzvizOptionsFlow()


class EzvizOptionsFlow(config_entries.OptionsFlow):
    """Configure sorting, opt-in synchronization, storage, and cache cleanup."""

    def __init__(self) -> None:
        self._pending_options: dict | None = None

    async def async_step_init(self, user_input: dict | None = None):
        return self.async_show_menu(step_id="init", menu_options=["configure_media", "delete_cache"])

    async def async_step_configure_media(self, user_input: dict | None = None):
        """Configure browser ordering and explicitly enable/disable media sync."""
        options = self.config_entry.options
        if user_input is not None:
            updated = dict(options)
            updated.update({
                CONF_ORDER_DAYS: user_input[CONF_ORDER_DAYS],
                CONF_ORDER_RECORDINGS: user_input[CONF_ORDER_RECORDINGS],
                CONF_MEDIA_SYNC: user_input[CONF_MEDIA_SYNC],
            })
            updated.setdefault(CONF_SYNC_HOURS, DEFAULT_SYNC_HOURS)
            updated.setdefault(CONF_COLD_STORAGE_PATH, DEFAULT_COLD_STORAGE_PATH)
            self._pending_options = updated
            if user_input[CONF_MEDIA_SYNC] == "on":
                return await self.async_step_sync_window()
            return self.async_create_entry(title="", data=updated)

        media_sync_options = [
            {"label": "On", "value": "on"},
            {"label": "Off", "value": "off"},
        ]
        schema = vol.Schema({
            vol.Required(CONF_ORDER_DAYS, default=options.get(CONF_ORDER_DAYS, DEFAULT_ORDER_DAYS)): selector.SelectSelector(
                selector.SelectSelectorConfig(options=_ORDER_OPTIONS, mode=selector.SelectSelectorMode.DROPDOWN)
            ),
            vol.Required(CONF_ORDER_RECORDINGS, default=options.get(CONF_ORDER_RECORDINGS, DEFAULT_ORDER_RECORDINGS)): selector.SelectSelector(
                selector.SelectSelectorConfig(options=_ORDER_OPTIONS, mode=selector.SelectSelectorMode.DROPDOWN)
            ),
            vol.Required(CONF_MEDIA_SYNC, default=options.get(CONF_MEDIA_SYNC, DEFAULT_MEDIA_SYNC)): selector.SelectSelector(
                selector.SelectSelectorConfig(options=media_sync_options, mode=selector.SelectSelectorMode.DROPDOWN)
            ),
        })
        return self.async_show_form(step_id="configure_media", data_schema=schema)

    async def async_step_sync_window(self, user_input: dict | None = None):
        """Offer automatic recent sync or a custom rolling-hour window."""
        if self._pending_options is None:
            self._pending_options = dict(self.config_entry.options)
        if user_input is not None:
            cold_path = str(user_input.get(CONF_COLD_STORAGE_PATH, "")).strip()
            error = self._validate_cold_path(cold_path)
            if error:
                return self.async_show_form(
                    step_id="sync_window", data_schema=self._sync_window_schema(), errors={CONF_COLD_STORAGE_PATH: error}
                )
            if cold_path:
                self._pending_options[CONF_COLD_STORAGE_PATH] = cold_path
            else:
                self._pending_options.pop(CONF_COLD_STORAGE_PATH, None)
            self._pending_options[CONF_SYNC_MODE] = user_input[CONF_SYNC_MODE]
            if user_input[CONF_SYNC_MODE] == "always":
                # Poll every 15 minutes and keep the latest rolling 24 hours cached.
                self._pending_options[CONF_SYNC_HOURS] = 24
                return await self._async_save_pending_options()
            return await self.async_step_sync_hours()
        return self.async_show_form(
            step_id="sync_window", data_schema=self._sync_window_schema()
        )

    def _sync_window_schema(self):
        options = self._pending_options or self.config_entry.options
        default_mode = options.get(CONF_SYNC_MODE)
        if default_mode not in {"always", "manual"}:
            default_mode = "manual" if int(options.get(CONF_SYNC_HOURS, 24) or 24) != 24 else "always"
        return vol.Schema({
            vol.Required(CONF_SYNC_MODE, default=default_mode): selector.SelectSelector(
                selector.SelectSelectorConfig(options=[
                    {"label": "Always up to date (rolling last 24 hours)", "value": "always"},
                    {"label": "Custom number of hours", "value": "manual"},
                ], mode=selector.SelectSelectorMode.DROPDOWN)
            ),
            vol.Optional(CONF_COLD_STORAGE_PATH, default=options.get(CONF_COLD_STORAGE_PATH, "")): str,
        })

    async def async_step_sync_hours(self, user_input: dict | None = None):
        """Get the user's custom rolling window before saving sync settings.

        Uses a free-form integer input instead of a 1-168 hour slider so users
        with very large SD cards can synchronise longer windows (for example a
        2 TB card that holds several years of recordings). An empty value falls
        back to the default 24-hour window.
        """
        if self._pending_options is None:
            self._pending_options = dict(self.config_entry.options)
        errors: dict[str, str] = {}
        if user_input is not None:
            raw = str(user_input.get(CONF_SYNC_HOURS) or "").strip()
            hours: int | None = None
            if raw == "":
                # Blank means "use the default 24-hour rolling window".
                hours = DEFAULT_SYNC_HOURS
            else:
                try:
                    parsed = int(raw)
                except (TypeError, ValueError):
                    parsed = 0
                if parsed >= 1:
                    hours = parsed
            if hours is None:
                errors[CONF_SYNC_HOURS] = "invalid_hours"
            else:
                self._pending_options[CONF_SYNC_HOURS] = hours
                return await self._async_save_pending_options()

        current_hours = self._pending_options.get(CONF_SYNC_HOURS)
        if not isinstance(current_hours, int) or current_hours < 1:
            current_hours = DEFAULT_SYNC_HOURS
        return self.async_show_form(
            step_id="sync_hours",
            data_schema=vol.Schema({
                vol.Optional(CONF_SYNC_HOURS, default=str(current_hours)): str,
            }),
            errors=errors,
        )

    def _validate_cold_path(self, cold_path: str) -> str | None:
        """Require a unique absolute cold-storage path if one was provided."""
        if not cold_path:
            return None
        target = Path(cold_path).expanduser()
        if not target.is_absolute():
            return "invalid_path"
        target = target.resolve()
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            if entry.entry_id == self.config_entry.entry_id:
                continue
            other = str(entry.options.get(CONF_COLD_STORAGE_PATH, "")).strip()
            if not other:
                continue
            other_target = Path(other).expanduser().resolve()
            if target == other_target or target in other_target.parents or other_target in target.parents:
                return "path_in_use"
        return None

    async def _async_save_pending_options(self):
        """Migrate the existing cache if the chosen storage root changed, then save."""
        assert self._pending_options is not None
        old_root = _cache_root(self.hass, self.config_entry)
        new_root = _cache_root(self.hass, self.config_entry, self._pending_options)
        if old_root != new_root:
            await self.hass.async_add_executor_job(_move_cache_root, old_root, new_root)
        return self.async_create_entry(title="", data=self._pending_options)

    async def async_step_delete_cache(self, user_input: dict | None = None):
        """Delete only locally cached videos and thumbnails for this camera."""
        if user_input is not None and user_input.get("confirm"):
            root = _cache_root(self.hass, self.config_entry)
            await self.hass.async_add_executor_job(_delete_cache_root, root)
            from homeassistant.components import persistent_notification
            persistent_notification.async_create(
                self.hass,
                f"Deleted all locally cached EZVIZ videos and thumbnails for **{self.config_entry.title}**. "
                "Recordings on the camera's SD card were not deleted.",
                title="EZVIZ cache cleared",
                notification_id=f"{DOMAIN}_cache_deleted_{self.config_entry.entry_id}",
            )
            return self.async_create_entry(title="", data=dict(self.config_entry.options))
        return self.async_show_form(
            step_id="delete_cache",
            data_schema=vol.Schema({vol.Required("confirm", default=False): bool}),
        )


def _cache_root(hass, entry, options=None) -> Path:
    """Return the camera's root folder containing video/ and thumb/."""
    selected_options = entry.options if options is None else options
    cold_path = str(selected_options.get(CONF_COLD_STORAGE_PATH, "")).strip()
    if cold_path:
        return Path(cold_path).expanduser().resolve()
    return Path(hass.config.path(".storage", DOMAIN, entry.entry_id)).resolve()


def _move_cache_root(source: Path, destination: Path) -> None:
    """Move existing video and thumbnail trees when the storage path changes."""
    if source == destination or not source.exists():
        return
    destination.mkdir(parents=True, exist_ok=True)
    for subdir in ("video", "thumb"):
        old = source / subdir
        if not old.exists():
            continue
        new = destination / subdir
        new.mkdir(parents=True, exist_ok=True)
        for file in old.rglob("*"):
            if not file.is_file():
                continue
            relative = file.relative_to(old)
            target = new / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                shutil.move(str(file), str(target))
            else:
                file.unlink()
        shutil.rmtree(old, ignore_errors=True)
    try:
        source.rmdir()
    except OSError:
        pass


def _delete_cache_root(root: Path) -> None:
    """Remove all locally cached media for one camera."""
    for subdir in ("video", "thumb"):
        shutil.rmtree(root / subdir, ignore_errors=True)
