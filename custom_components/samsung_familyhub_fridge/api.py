from __future__ import annotations
import os
import asyncio
from datetime import datetime, timezone, timedelta
import logging
import time
from typing import TYPE_CHECKING

from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
import requests

from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_state_change_event

from .const import (
    CID,
    DEFAULT_TIMEOUT,
    CONF_FOOD_TOKEN,
    WHISK_FOODLIST_API,
    FOOD_TOKEN_ENTITY,
    FOOD_TOKEN_FILE,
    DEFAULT_FOOD_UPDATE_INTERVAL,
)

if TYPE_CHECKING:
    from homeassistant.helpers import config_entry_oauth2_flow

_LOGGER = logging.getLogger(__name__)

# Helper entity that the PAT rotator writes the new token to.
_TOKEN_ENTITY = "input_text.smartthings_pat"


class AuthenticationError(Exception):
    """Raised when SmartThings API returns an authentication error (401/403)."""


class DataCoordinator(DataUpdateCoordinator):
    def __init__(self, hass: HomeAssistant, api: FamilyHub):
        super().__init__(
            hass,
            _LOGGER,
            name="File ID refresher",
            update_interval=timedelta(seconds=10),
        )
        self._hass = hass
        self.api = api
        self.last_file_ids = []
        self.last_updated_at = None
        self._consecutive_failures = 0
        self._max_consecutive_failures = 10

        # State change listener to dynamically reload the integration when the PAT changes.
        # This recovers the integration from ConfigEntryAuthFailed states automatically.
        async def _async_update_and_reload(e, t):
            new_data = {**e.data, "token": t}
            self._hass.config_entries.async_update_entry(e, data=new_data)
            await self._hass.config_entries.async_reload(e.entry_id)

        def _handle_token_change(event):
            new_state = event.data.get("new_state")
            if new_state is None or new_state.state in ("unknown", "unavailable", ""):
                return
            new_token = new_state.state.strip()
            if new_token and new_token != self.api.token:
                _LOGGER.info(
                    "Detected changed SmartThings PAT in %s. Updating config entry and reloading integration.",
                    _TOKEN_ENTITY
                )
                self.api.update_token(new_token)

                entries = self._hass.config_entries.async_entries("samsung_familyhub_fridge")
                for entry in entries:
                    self._hass.add_job(_async_update_and_reload, entry, new_token)

        self._unsub_token_listener = async_track_state_change_event(
            hass,
            [_TOKEN_ENTITY],
            _handle_token_change,
        )

        def _handle_food_token_change(event):
            new_state = event.data.get("new_state")
            if new_state is None or new_state.state in ("unknown", "unavailable", ""):
                return
            new_tok = new_state.state.strip()
            if new_tok and getattr(self, "food_coordinator", None) is not None:
                _LOGGER.info(
                    "Detected updated Samsung Food token in %s. Refreshing food inventory...",
                    FOOD_TOKEN_ENTITY,
                )
                self._hass.add_job(self.food_coordinator.async_request_refresh)

        self._unsub_food_token_listener = async_track_state_change_event(
            hass,
            [FOOD_TOKEN_ENTITY],
            _handle_food_token_change,
        )

        def _unsubscribe():
            if hasattr(self, "_unsub_token_listener") and self._unsub_token_listener is not None:
                self._unsub_token_listener()
                self._unsub_token_listener = None
            if hasattr(self, "_unsub_food_token_listener") and self._unsub_food_token_listener is not None:
                self._unsub_food_token_listener()
                self._unsub_food_token_listener = None

        for entry in hass.config_entries.async_entries("samsung_familyhub_fridge"):
            entry.async_on_unload(_unsubscribe)

        # State change listener to dynamically reload the integration when the PAT changes.
        # This recovers the integration from ConfigEntryAuthFailed states automatically.
        def _handle_token_change(event):
            new_state = event.data.get("new_state")
            if new_state is None or new_state.state in ("unknown", "unavailable", ""):
                return
            new_token = new_state.state.strip()
            if new_token and new_token != self.api.token:
                _LOGGER.info(
                    "Detected changed SmartThings PAT in %s. Updating config entry and reloading integration.",
                    _TOKEN_ENTITY
                )
                self.api.update_token(new_token)

                def _update_and_reload(e, t):
                    new_data = {**e.data, "token": t}
                    self._hass.config_entries.async_update_entry(e, data=new_data)
                    self._hass.config_entries.async_schedule_reload(e.entry_id)

                entries = self._hass.config_entries.async_entries("samsung_familyhub_fridge")
                for entry in entries:
                    self._hass.add_job(_update_and_reload, entry, new_token)

        self._unsub_token_listener = async_track_state_change_event(
            hass,
            [_TOKEN_ENTITY],
            _handle_token_change,
        )

        def _unsubscribe():
            if hasattr(self, "_unsub_token_listener") and self._unsub_token_listener is not None:
                self._unsub_token_listener()
                self._unsub_token_listener = None

        for entry in hass.config_entries.async_entries("samsung_familyhub_fridge"):
            entry.async_on_unload(_unsubscribe)

    async def _async_update_data(self):
        """Fetch data from API endpoint."""
        
        # ── Dynamic Token Refresh & Persistence ────────────────────────────
        # If input_text.smartthings_pat holds a valid non-empty value, we sync
        # it into self.api.token and write it back to the config entry on disk.
        # This keeps the integration functional without restarts and avoids
        # "Re-authenticate" repair flows.
        state = self._hass.states.get(_TOKEN_ENTITY)
        if state and state.state not in ("unknown", "unavailable", ""):
            live_token = state.state.strip()
            if live_token and live_token != self.api.token:
                _LOGGER.debug(
                    "Picked up refreshed SmartThings PAT from %s", 
                    _TOKEN_ENTITY
                )
                self.api.update_token(live_token)
                
                # Permanently update config entry data on disk so restarts sync cleanly
                entries = self._hass.config_entries.async_entries("samsung_familyhub_fridge")
                for entry in entries:
                    if entry.data.get("token") != live_token:
                        new_data = {**entry.data, "token": live_token}
                        self._hass.config_entries.async_update_entry(entry, data=new_data)
                        _LOGGER.info("Updated config entry on disk with new SmartThings PAT")
        # ───────────────────────────────────────────────────────────────────

        try:
            # OAuth mode: refresh the access token (if close to expiry) BEFORE
            # any API call. No-op for PAT mode.
            await self.api.async_ensure_fresh_token()
            if self.api.device_id is None:
                _LOGGER.debug("No device_id — fetching device list")
                status = await self._hass.async_add_executor_job(
                    self.api.get_all_device_status
                )
                self.api.set_device_status(status)
            if self.api.should_update:
                _LOGGER.debug("should_update=True → sending refresh command to fridge")
                await self._hass.async_add_executor_job(self.api.update_camera)
                self.api.should_update = False
            elif set(self.last_file_ids) != set(self.api.get_file_ids()):
                new_ids = self.api.get_file_ids()
                _LOGGER.debug(
                    "file IDs changed: %s → %s, downloading images",
                    self.last_file_ids,
                    new_ids,
                )
                success = await self._hass.async_add_executor_job(
                    self.api.download_images
                )
                if success:
                    self.last_updated_at = time.time()
                    self.last_file_ids = new_ids
                    # Trigger food inventory sync upon door close / new camera snapshot upload
                    if getattr(self, "food_coordinator", None) is not None:
                        self.food_coordinator.async_trigger_door_close_sync(12)
                else:
                    _LOGGER.warning(
                        "download_images returned no successes — will retry "
                        "on next poll"
                    )
            else:
                status = await self._hass.async_add_executor_job(
                    self.api.get_current_device_status
                )
                self.api.set_current_device_status(status)
                self.api.extract_device_data()
                _LOGGER.debug(
                    "polled status: last_closed=%s should_update=%s file_ids=%s",
                    self.api.last_closed,
                    self.api.should_update,
                    self.api.get_file_ids(),
                )
            # Reset failure count on success
            self._consecutive_failures = 0
        except AuthenticationError as err:
            state = self._hass.states.get(_TOKEN_ENTITY)
            if state is not None and state.state in ("unknown", "unavailable"):
                _LOGGER.warning(
                    "SmartThings auth failed, but helper entity %s is not ready yet (%s). "
                    "Postponing setup...",
                    _TOKEN_ENTITY,
                    state.state,
                )
                raise ConfigEntryNotReady(
                    f"Waiting for helper entity {_TOKEN_ENTITY} to restore state."
                ) from err

            raise ConfigEntryAuthFailed(
                "SmartThings token expired or is invalid. "
                "Please re-authenticate with a new token."
            ) from err
        except Exception as err:
            self._consecutive_failures += 1
            if self._consecutive_failures < self._max_consecutive_failures:
                _LOGGER.debug(
                    "Transient error fetching fridge data (%d/%d): %s. Retrying on next poll.",
                    self._consecutive_failures,
                    self._max_consecutive_failures,
                    err,
                )
                return self.data
            _LOGGER.error(
                "SmartThings fridge communication failed %d consecutive times: %s",
                self._consecutive_failures,
                err,
            )
            raise UpdateFailed(
                f"SmartThings API failed {self._consecutive_failures} consecutive times: {err}"
            ) from err


class FamilyHub:
    """SmartThings Family Hub fridge API client.

    Two auth modes:

    1. PAT mode (default): caller provides a raw SmartThings token via
       `token=`. Token is static; caller is responsible for refresh via
       `update_token()`.

    2. OAuth mode: after construction, caller attaches an
       ``OAuth2Session`` via `attach_oauth_session(session)`. Before every
       API call the coordinator awaits `async_ensure_fresh_token()` which
       asks HA's OAuth2Session to refresh the access token if it's close
       to expiry — no manual refresh needed.
    """

    def __init__(self, hass: HomeAssistant, token: str, device_id: str) -> None:
        """Initialize."""
        self._device_id = device_id
        self._hass = hass
        self.token = token
        self._headers = {"Authorization": f"Bearer {self.token}"}
        self.images = []
        self._device_status = None
        self._current_device_status = None
        self.last_closed = None
        self.should_update = False
        self.downloaded_images = [None, None, None]
        self._oauth_session: "config_entry_oauth2_flow.OAuth2Session | None" = None
        # Samsung IoT token for client.smartthings.com image downloads.
        # Only set in OAuth mode; PAT tokens already carry Samsung ID.
        self._samsung_iot_token: str | None = None
        self._samsung_iot_headers: dict | None = None
        self._samsung_iot_refresh_token: str | None = None
        self._samsung_iot_auth_server: str = "https://us-auth2.samsungosp.com"
        self._entry = None

    def set_samsung_iot_token(
        self,
        token: str,
        refresh_token: str | None = None,
        auth_server: str | None = None,
        entry=None,
    ) -> None:
        """Set a Samsung IoT token for client.smartthings.com image downloads.

        This token carries Samsung Account identity and is needed because
        the udo/file_links endpoint rejects standard SmartThings OAuth tokens.
        Optionally store a refresh_token, auth_server, and config entry so
        download_images() can silently refresh the token on auth failures.
        """
        self._samsung_iot_token = token
        self._samsung_iot_headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.smartthings+json;v=1",
        }
        if refresh_token is not None:
            self._samsung_iot_refresh_token = refresh_token
        if auth_server is not None:
            self._samsung_iot_auth_server = auth_server
        if entry is not None:
            self._entry = entry

    def _is_no_samsung_id_error(self, response: requests.Response) -> bool:
        """Return True if response body contains 'No samsung id' (400 auth error)."""
        try:
            return "No samsung id" in response.text
        except Exception:
            return False

    def _do_samsung_iot_refresh(self) -> None:
        """Refresh the Samsung IoT token in-place and persist the new refresh token."""
        from .auth import refresh_samsung_iot_token

        try:
            iot_creds = refresh_samsung_iot_token(
                self._samsung_iot_refresh_token,
                self._samsung_iot_auth_server,
            )
        except Exception as err:
            raise AuthenticationError(
                f"Samsung IoT token refresh failed: {err}"
            ) from err

        self.set_samsung_iot_token(
            iot_creds.access_token,
            refresh_token=iot_creds.refresh_token,
            auth_server=self._samsung_iot_auth_server,
            entry=self._entry,
        )

        if self._entry is not None:
            from .const import CONF_SAMSUNG_IOT_REFRESH_TOKEN
            new_data = {
                **self._entry.data,
                CONF_SAMSUNG_IOT_REFRESH_TOKEN: iot_creds.refresh_token,
            }
            self._hass.config_entries.async_update_entry(self._entry, data=new_data)

    def attach_oauth_session(
        self, session: "config_entry_oauth2_flow.OAuth2Session"
    ) -> None:
        """Bind an HA OAuth2Session so tokens refresh automatically.

        Once attached, `async_ensure_fresh_token()` consults this session
        before every API call and updates the bearer header in place.
        """
        self._oauth_session = session

    async def async_ensure_fresh_token(self) -> None:
        """If running in OAuth mode, ensure the bearer token is still valid.

        No-op for PAT mode. Safe to call on every poll — HA's OAuth2Session
        only performs a network refresh when the access_token is within
        a few seconds of expiring.
        """
        if self._oauth_session is None:
            return
        await self._oauth_session.async_ensure_token_valid()
        new_token = self._oauth_session.token.get("access_token")
        if new_token and new_token != self.token:
            self.update_token(new_token)

    def update_token(self, token: str) -> None:
        """Update the API token (used after re-authentication or OAuth refresh)."""
        self.token = token
        self._headers = {"Authorization": f"Bearer {self.token}"}

    @property
    def device_id(self):
        if not self._device_id:
            self.set_device_id()
        return self._device_id

    def _check_response(self, response: requests.Response) -> None:
        """Check HTTP response for auth errors and raise accordingly."""
        if response.status_code in (401, 403):
            _LOGGER.error(
                "SmartThings authentication failed (HTTP %s). "
                "Token may have expired — SmartThings personal access tokens "
                "expire after 24 hours",
                response.status_code,
            )
            raise AuthenticationError(
                f"SmartThings API returned HTTP {response.status_code}. "
                "Token is expired or invalid."
            )
        if response.status_code == 400:
            try:
                body = response.json()
                err = body.get("error", {})
                if (
                    err.get("code") == "BadRequestError"
                    and "No samsung id" in err.get("message", "")
                ):
                    raise AuthenticationError(
                        "No samsung id available — switch to Standalone OAuth or add "
                        "Samsung Account credentials"
                    )
            except AuthenticationError:
                raise
            except Exception:
                pass
        if not response.ok:
            _LOGGER.warning(
                "SmartThings API request failed: HTTP %s - %s",
                response.status_code,
                response.text[:200],
            )

    async def authenticate(self) -> bool:
        """Test if we can authenticate with the host."""
        await self._hass.async_add_executor_job(self.get_all_device_status)
        return True

    def set_device_status(self, status):
        self._device_status = status

    def set_current_device_status(self, status):
        self._current_device_status = status

    def download_images(self) -> bool:
        """Download the actual camera images from SmartThings.

        Returns True if at least one image was downloaded successfully.
        Failed individual downloads preserve the previously-known image,
        so a transient network error on one image doesn't wipe the others.
        """
        if not self._current_device_status or not self.device_id:
            return False

        file_ids = self.get_file_ids()
        # Start from the existing images so a partial failure doesn't wipe
        # slots that we can't refresh this cycle.
        result = list(self.downloaded_images)
        while len(result) < len(file_ids):
            result.append(None)

        successes = 0
        for idx, file_id in enumerate(file_ids):
            try:
                url = (
                    f"https://client.smartthings.com/udo/file_links/"
                    f"{file_id}?cid={CID}&di={self.device_id}"
                )
                # Use Samsung IoT token if available (OAuth mode);
                # otherwise use the main token (PAT mode).
                dl_headers = (
                    self._samsung_iot_headers
                    if self._samsung_iot_headers
                    else self._headers
                )
                r = requests.get(
                    url,
                    headers=dl_headers,
                    timeout=DEFAULT_TIMEOUT,
                )
                # Samsung IoT in-session refresh: when using IoT headers and the
                # server returns 401/403 or a 400 'No samsung id' error, attempt
                # a silent token refresh and retry once before surfacing as
                # ConfigEntryAuthFailed.
                if dl_headers is self._samsung_iot_headers:
                    auth_fail = r.status_code in (401, 403) or (
                        r.status_code == 400 and self._is_no_samsung_id_error(r)
                    )
                    if auth_fail:
                        if not self._samsung_iot_refresh_token:
                            raise AuthenticationError(
                                f"Samsung IoT authentication failed "
                                f"(HTTP {r.status_code}) — no refresh token available."
                            )
                        self._do_samsung_iot_refresh()
                        r = requests.get(
                            url,
                            headers=self._samsung_iot_headers,
                            timeout=DEFAULT_TIMEOUT,
                        )
                self._check_response(r)
                content_type = r.headers.get("content-type", "")
                _LOGGER.debug(
                    "download_images[%d]: file_id=%s url=%s status=%s "
                    "content_type=%s length=%d",
                    idx,
                    file_id[:8],
                    url.split("?")[0][-40:],
                    r.status_code,
                    content_type,
                    len(r.content),
                )
                result[idx] = r.content
                successes += 1
            except AuthenticationError:
                # Auth errors must propagate up so the coordinator can
                # trigger reauth — do not swallow.
                raise
            except Exception as err:
                _LOGGER.warning(
                    "download_images[%d]: failed to download file_id=%s: %s",
                    idx,
                    file_id[:8],
                    err,
                )
                # Keep the previous bytes for this slot (don't overwrite with None)

        self.downloaded_images = result
        _LOGGER.debug(
            "download_images: stored %d/%d images, sizes=%s",
            successes,
            len(file_ids),
            [len(i) if i else 0 for i in result],
        )
        return successes > 0

    def get_all_device_status(self):
        """Get all of the devices in the account."""
        r = requests.get(
            "https://client.smartthings.com/devices/status",
            headers=self._headers,
            timeout=DEFAULT_TIMEOUT,
        )
        self._check_response(r)
        data = r.json()
        if isinstance(data, dict) and "error" in data:
            _LOGGER.error(
                "SmartThings API returned error: %s", data["error"]
            )
        return data

    def get_current_device_status(self):
        """Get the current device status."""
        r = requests.get(
            f"https://api.smartthings.com/v1/devices/{self.device_id}/components/main/status",
            headers=self._headers,
            timeout=DEFAULT_TIMEOUT,
        )
        self._check_response(r)
        data = r.json()
        if isinstance(data, dict) and "error" in data:
            _LOGGER.error(
                "SmartThings device status returned error: %s", data["error"]
            )
        return data

    def extract_device_data(self):
        """Extract contact sensor data to detect door close events."""
        if not self._current_device_status:
            return
        try:
            contact = self._current_device_status["contactSensor"]["contact"]
        except KeyError:
            _LOGGER.debug(
                "contactSensor data not available in device status"
            )
            return
        _LOGGER.debug(
            "contactSensor: value=%s timestamp=%s (last_closed=%s)",
            contact.get("value"),
            contact.get("timestamp"),
            self.last_closed,
        )
        # Trigger a refresh on first poll after startup, so users see fresh
        # images without having to physically open and close the fridge door.
        first_poll = self.last_closed is None
        if contact["value"] == "closed" and (
            first_poll or contact["timestamp"] != self.last_closed
        ):
            self.last_closed = contact["timestamp"]
            self.should_update = True
            if first_poll:
                _LOGGER.debug("First poll after startup — requesting camera refresh")

    def get_file_ids(self):
        """Get the file IDs for the camera images."""
        if not self._current_device_status:
            return []
        try:
            element = self._current_device_status["samsungce.viewInside"]["contents"]
            return [i["fileId"] for i in element["value"]]
        except (KeyError, TypeError):
            _LOGGER.debug(
                "samsungce.viewInside data not available in device status"
            )
            return []

    def set_device_id(self):
        """Extract device ID from the device status list."""
        if not self._device_status:
            return
        try:
            items = self._device_status["items"]
        except (KeyError, TypeError):
            _LOGGER.error(
                "Unexpected device status format — missing 'items' key. "
                "This may indicate an expired token or API error. "
                "Response: %s",
                str(self._device_status)[:200],
            )
            return
        for element in items:
            if (
                element.get("capabilityId") == "samsungce.viewInside"
                and element.get("attributeName") == "contents"
            ):
                self._device_id = element["deviceId"]
                break

    def update_camera(self):
        """Send the reverse-engineered refresh command to the fridge.

        Uses the single OCF resource at /udo/contents/provider/vs/0 which
        contains all three camera images. Throttled by the coordinator via
        contactSensor door-close events.
        """
        if not self.device_id:
            return
        r = requests.post(
            f"https://api.smartthings.com/v1/devices/{self.device_id}/commands",
            headers=self._headers,
            json={
                "commands": [
                    {
                        "component": "main",
                        "capability": "execute",
                        "command": "execute",
                        "arguments": [
                            "/udo/contents/provider/vs/0",
                            {
                                "x.com.samsung.da.control": {
                                    "x.com.samsung.da.command": "refresh"
                                }
                            },
                        ],
                    }
                ]
            },
            timeout=DEFAULT_TIMEOUT,
        )
        self._check_response(r)
        _LOGGER.debug(
            "update_camera: status=%s body=%s",
            r.status_code,
            r.text[:300],
        )


class SamsungFoodClient:
    """Client for querying Samsung Food (Whisk) AI Food Manager inventory."""

    def __init__(self, hass: HomeAssistant, token: str | None = None) -> None:
        self.hass = hass
        self._configured_token = token
        self._cached_inventory: dict | None = None
        self._session: requests.Session | None = None

    def _get_session(self) -> requests.Session:
        """Get or create requests.Session with retries."""
        if self._session is None:
            self._session = requests.Session()
        return self._session

    def _get_file_token_sync(self) -> str | None:
        """Synchronously check file paths on disk in executor thread."""
        candidate_paths = [
            self.hass.config.path(FOOD_TOKEN_FILE),
            self.hass.config.path(f"custom_components/samsung_familyhub_fridge/{FOOD_TOKEN_FILE}"),
            f"/config/{FOOD_TOKEN_FILE}",
            f"/config/custom_components/samsung_familyhub_fridge/{FOOD_TOKEN_FILE}",
            f"/tmp/{FOOD_TOKEN_FILE}",
            os.path.join(os.path.dirname(__file__), FOOD_TOKEN_FILE),
            os.path.join(os.path.dirname(__file__), f"../../scripts/{FOOD_TOKEN_FILE}"),
            os.path.join(os.path.dirname(__file__), f"../{FOOD_TOKEN_FILE}"),
        ]
        for p in candidate_paths:
            try:
                if os.path.exists(p):
                    with open(p, "r", encoding="utf-8") as f:
                        tok = f.read().strip()
                        if tok:
                            _LOGGER.info("SamsungFoodClient: Loaded token from %s", p)
                            return tok
            except Exception as err:
                _LOGGER.debug("Could not read candidate token file %s: %s", p, err)

        return None

    async def async_get_token(self) -> str | None:
        """Retrieve Whisk/Samsung Food token without blocking the event loop."""
        if self._configured_token and self._configured_token.strip():
            return self._configured_token.strip()

        # Check helper entity
        state = self.hass.states.get(FOOD_TOKEN_ENTITY)
        if state and state.state not in ("unknown", "unavailable", ""):
            return state.state.strip()

        # Check file locations in executor thread
        return await self.hass.async_add_executor_job(self._get_file_token_sync)

    async def async_has_token(self) -> bool:
        """Check if a valid token is available without blocking."""
        token = await self.async_get_token()
        return bool(token)

    def _fetch_food_items_sync(self, token: str) -> dict:
        """Synchronously fetch all food items from Whisk/Samsung Food API across all pages."""
        clean_auth = token if token.startswith("Bearer ") or token.startswith("Token ") else f"Bearer {token}"
        raw_token = token.replace("Bearer ", "").replace("Token ", "").strip()

        headers = {
            "Authorization": clean_auth,
            "x-whisk-token": raw_token,
            "Accept": "application/json",
        }

        all_items = []
        after_cursor = ""
        page_num = 1
        fetch_error = None
        session = self._get_session()

        _LOGGER.debug("SamsungFoodClient: Fetching active food inventory from Samsung Food API...")

        while True:
            params = {}
            if after_cursor:
                params["paging.cursors.after"] = after_cursor

            try:
                r = session.get(WHISK_FOODLIST_API, headers=headers, params=params, timeout=25)
                if not r.ok:
                    _LOGGER.warning("Samsung Food API page %d returned HTTP %s: %s", page_num, r.status_code, r.text[:200])
                    fetch_error = f"HTTP {r.status_code}"
                    break

                data = r.json()
                items = data.get("items", [])
                all_items.extend(items)
                _LOGGER.debug("Samsung Food API page %d: %d items (total so far: %d)", page_num, len(items), len(all_items))

                paging = data.get("paging", {})
                next_cursor = paging.get("cursors", {}).get("after")
                if next_cursor and next_cursor != after_cursor and len(items) > 0:
                    after_cursor = next_cursor
                    page_num += 1
                else:
                    break
            except Exception as err:
                _LOGGER.warning("Error fetching Samsung Food items on page %d (%s).", page_num, err)
                fetch_error = str(err)
                break

        # If fetch encountered an error and we have cached data, retain cache
        if fetch_error:
            if self._cached_inventory and self._cached_inventory.get("items"):
                _LOGGER.info(
                    "SamsungFoodClient: Transient fetch error (%s). Preserving %d previously-cached active items.",
                    fetch_error,
                    self._cached_inventory.get("total_items", 0),
                )
                return self._cached_inventory
            if not all_items:
                raise UpdateFailed(f"Failed to fetch Samsung Food items: {fetch_error}")

        # Filter active items
        active_items = []
        for itm in all_items:
            content = itm.get("content", {})
            status = content.get("presence_status", "")
            is_consumed = bool(content.get("consumed_at")) or bool(content.get("deleted_at")) or status == "PRESENCE_STATUS_CONSUMED"

            if not is_consumed or status in ("PRESENCE_STATUS_EXISTING", "PRESENCE_STATUS_PRESENT"):
                active_items.append({
                    "id": itm.get("id"),
                    "name": content.get("name") or "Unnamed Food Item",
                    "presence_status": status,
                    "location": content.get("location"),
                    "added_at": content.get("added_at"),
                    "expiration_date": content.get("expiration_date") or content.get("days_to_expire"),
                    "image_url": content.get("image_url") or content.get("photo_url"),
                    "ai_generated": content.get("ai_generated", False),
                    "ai_suggested_names": content.get("ai_suggested_names", []),
                })

        _LOGGER.info("SamsungFoodClient: Sync complete. %d active items found (out of %d total account items).", len(active_items), len(all_items))

        result = {
            "total_items": len(active_items),
            "last_synced": datetime.now(timezone.utc).isoformat(),
            "items": active_items,
        }
        self._cached_inventory = result
        return result

    async def async_get_active_food_items(self) -> dict:
        """Fetch all active food items asynchronously."""
        token = await self.async_get_token()
        if not token:
            if self._cached_inventory:
                return self._cached_inventory
            return {"total_items": 0, "last_synced": None, "items": []}

        return await self.hass.async_add_executor_job(self._fetch_food_items_sync, token)


class SamsungFoodCoordinator(DataUpdateCoordinator):
    """Coordinator for syncing Samsung Food AI Food Manager inventory."""

    def __init__(self, hass: HomeAssistant, client: SamsungFoodClient) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="Samsung Food Inventory Refresher",
            update_interval=timedelta(seconds=DEFAULT_FOOD_UPDATE_INTERVAL),
        )
        self.client = client
        self._delay_task: asyncio.Task | None = None

    async def _async_update_data(self) -> dict:
        """Fetch updated active food inventory."""
        try:
            return await self.client.async_get_active_food_items()
        except Exception as err:
            raise UpdateFailed(f"Failed to fetch Samsung Food inventory: {err}") from err

    def async_trigger_door_close_sync(self, delay_seconds: int = 12) -> None:
        """Schedule a delayed sync following a door-close event."""
        if self._delay_task and not self._delay_task.done():
            self._delay_task.cancel()

        async def _delayed_refresh():
            try:
                _LOGGER.debug("Door close detected; waiting %ds for AI vision to commit...", delay_seconds)
                await asyncio.sleep(delay_seconds)
                _LOGGER.info("Triggering food inventory refresh after door close...")
                await self.async_request_refresh()
            except asyncio.CancelledError:
                pass
            except Exception as e:
                _LOGGER.warning("Error in door-close food sync: %s", e)

        self._delay_task = self.hass.async_create_task(_delayed_refresh())

