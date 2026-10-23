"""Constants for the Samsung FamilyHub Fridge integration."""
from enum import StrEnum
from typing import Final

DOMAIN: Final = "samsung_familyhub_fridge"
CID: Final = "5Hic3rk1FP"
DEFAULT_TIMEOUT: Final = 10

# Config entry `data` keys
CONF_AUTH_MODE: Final = "auth_mode"
CONF_TOKEN: Final = "token"
CONF_DEVICE_ID: Final = "device_id"
CONF_SAMSUNG_IOT_REFRESH_TOKEN: Final = "samsung_iot_refresh_token"
CONF_SAMSUNG_IOT_AUTH_SERVER: Final = "samsung_iot_auth_server"


class AuthMode(StrEnum):
    """Values of the config entry's ``auth_mode``."""

    OAUTH = "oauth"                      # removed: borrowed the core SmartThings OAuth credentials
    PAT = "pat"                          # SmartThings Personal Access Token (renewed by the rotator)
    STANDALONE_OAUTH = "standalone_oauth"  # custom SmartThings app credentials


AUTH_MODE_OAUTH: Final = AuthMode.OAUTH
AUTH_MODE_PAT: Final = AuthMode.PAT
AUTH_MODE_STANDALONE_OAUTH: Final = AuthMode.STANDALONE_OAUTH

# Standalone OAuth config keys
CONF_OAUTH_CLIENT_ID: Final = "oauth_client_id"
CONF_OAUTH_CLIENT_SECRET: Final = "oauth_client_secret"
CONF_OAUTH_REFRESH_TOKEN: Final = "oauth_refresh_token"

# Samsung Account signin secret (optional; from SmartThings APK decompilation)
CONF_SAMSUNG_SIGNIN_CLIENT_SECRET: Final = "samsung_signin_client_secret"

# Samsung Food (Whisk) AI Food Manager constants
CONF_FOOD_TOKEN: Final = "food_token"
WHISK_FOODLIST_API: Final = "https://api.whisk.com/foodlist/v2"
DEFAULT_FOOD_UPDATE_INTERVAL: Final = 600  # 10 minutes

# Samsung Family Hub Live Food Circles (KICS EPA) constants
KICS_CLIENT_ID: Final = "qumapq783u"
KICS_FOODLIST_ENDPOINT: Final = "https://kics-epa.samsungepa.com/api/foodlist"
KICS_SA_AUTH_URL: Final = "us-auth2.samsungosp.com"
CONF_KICS_TOKEN: Final = "kics_food_token"
CONF_LOCATION_ID: Final = "location_id"

# Rotator microservice & options constants
CONF_ROTATOR_URL: Final = "rotator_url"
CONF_ROTATOR_API_KEY: Final = "rotator_api_key"
DEFAULT_ROTATOR_URL: Final = "http://127.0.0.1:8765"
ROTATOR_TIMEOUT: Final = 2  # seconds; the rotator is a local/VPN service
CONF_FOOD_UPDATE_INTERVAL: Final = "food_update_interval"
CONF_POLL_INTERVAL: Final = "poll_interval"
DEFAULT_POLL_INTERVAL: Final = 60  # seconds between SmartThings status polls
MIN_POLL_INTERVAL: Final = 10
MAX_POLL_INTERVAL: Final = 3600
CONF_ENABLE_BOTTOM_CAMERA: Final = "enable_bottom_camera"
DEFAULT_ENABLE_BOTTOM_CAMERA: Final = False

