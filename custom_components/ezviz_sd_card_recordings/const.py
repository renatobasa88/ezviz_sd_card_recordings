"""Constants for EZVIZ SD Card Recordings."""

from logging import Logger, getLogger

DOMAIN = "ezviz_sd_card_recordings"
LOGGER: Logger = getLogger(__package__)

CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_SERIAL = "serial"
CONF_REGION = "region"
CONF_CHANNEL = "channel"
CONF_CAMERA_NAME = "camera_name"
CONF_SESSION_TOKEN = "session_token"
CONF_ORDER_DAYS = "order_days"
CONF_ORDER_RECORDINGS = "order_recordings"
CONF_MEDIA_SYNC = "media_sync"
CONF_SYNC_MODE = "sync_mode"
CONF_SYNC_HOURS = "sync_hours"
CONF_SYNC_NOTIFY = "sync_notify"
CONF_COLD_STORAGE_PATH = "cold_storage_path"

DEFAULT_ORDER_DAYS = "descending"
DEFAULT_ORDER_RECORDINGS = "descending"
DEFAULT_MEDIA_SYNC = "off"
DEFAULT_SYNC_HOURS = 24
DEFAULT_SYNC_NOTIFY = "on"
DEFAULT_COLD_STORAGE_PATH = ""

DEFAULT_REGION = "apiisgp.ezvizlife.com"
DEFAULT_CHANNEL = 1
DEFAULT_ARCHIVE_YEARS = 5

TOKEN_STORAGE_DIR = DOMAIN
CACHE_DIRECTORY = "ezviz_sd_card_recordings"
MEDIA_EVENT_DOWNLOADED = f"{DOMAIN}_media_downloaded"
NOTIFICATION_PREFIX = f"{DOMAIN}_download"
CACHE_VIEW_URL = f"/api/{DOMAIN}/media/{{entry_id}}/{{day}}/{{key}}/{{kind}}"

ERROR_AUTH = "invalid_auth"
ERROR_CANNOT_CONNECT = "cannot_connect"
ERROR_MFA = "mfa_required"
ERROR_UNKNOWN = "unknown"
