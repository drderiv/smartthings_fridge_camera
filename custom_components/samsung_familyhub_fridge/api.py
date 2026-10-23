"""SmartThings Family Hub fridge client (no Home Assistant dependencies)."""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import requests

from .const import CID, DEFAULT_TIMEOUT

_LOGGER = logging.getLogger(__name__)

SAMSUNG_AUTH_SERVER = "https://us-auth2.samsungosp.com"
TOKEN_REFRESH_MARGIN = 300  # refresh OAuth access tokens this many seconds before expiry
DEFAULT_TOKEN_LIFETIME = 3600


class AuthenticationError(Exception):
    """Raised when SmartThings or Samsung rejects the credentials (HTTP 401/403, bad refresh)."""


class RateLimitError(Exception):
    """Raised when SmartThings answers HTTP 429 (too many requests)."""


class FamilyHub:
    """SmartThings Family Hub fridge API client.

    The caller supplies a bearer token (a PAT, or an OAuth access token) and may attach a
    ``token_refresher`` so OAuth access tokens are renewed before they expire. All HTTP
    methods block; call them from an executor thread.
    """

    def __init__(
        self,
        token: str,
        device_id: str | None = None,
        *,
        on_iot_refresh_token: Callable[[str], None] | None = None,
    ) -> None:
        self._device_id = device_id
        self.token = token
        self._headers = {"Authorization": f"Bearer {token}"}
        self._device_status: dict[str, Any] | None = None
        self._current_device_status: dict[str, Any] | None = None
        self.last_closed: str | None = None
        self.should_update = False
        self.downloaded_images: list[bytes | None] = [None, None, None]
        self._location_id: str | None = None
        self._refresher: Callable[[], Awaitable[Any]] | None = None
        self._token_expires_at = 0.0
        self._refresh_lock = asyncio.Lock()
        # Samsung IoT token for client.smartthings.com image downloads (Standalone OAuth only;
        # PATs already carry a Samsung ID).
        self._samsung_iot_headers: dict[str, str] | None = None
        self._samsung_iot_refresh_token: str | None = None
        self._samsung_iot_auth_server = SAMSUNG_AUTH_SERVER
        self._on_iot_refresh_token = on_iot_refresh_token

    # --- credentials ---------------------------------------------------------------

    def update_token(self, token: str) -> None:
        """Replace the bearer token (after a rotator pickup, re-auth or OAuth refresh)."""
        self.token = token
        self._headers = {"Authorization": f"Bearer {token}"}

    def set_token_refresher(
        self, refresher: Callable[[], Awaitable[Any]], expires_in: int | None = None
    ) -> None:
        """Renew the access token through ``refresher`` shortly before it expires.

        ``refresher`` is an async callable returning an object with ``access_token`` and
        ``expires_in``; it is also responsible for persisting any rotated refresh token.
        """
        self._refresher = refresher
        self._token_expires_at = time.monotonic() + (expires_in or DEFAULT_TOKEN_LIFETIME)

    async def async_ensure_fresh_token(self) -> None:
        """Refresh the access token if it is about to expire (no-op without a refresher)."""
        if self._refresher is None or time.monotonic() < self._token_expires_at - TOKEN_REFRESH_MARGIN:
            return
        async with self._refresh_lock:
            if time.monotonic() < self._token_expires_at - TOKEN_REFRESH_MARGIN:
                return  # another caller refreshed while we waited
            credentials = await self._refresher()
            self.update_token(credentials.access_token)
            self._token_expires_at = time.monotonic() + (
                getattr(credentials, "expires_in", 0) or DEFAULT_TOKEN_LIFETIME
            )

    def set_samsung_iot_token(
        self,
        token: str,
        refresh_token: str | None = None,
        auth_server: str | None = None,
    ) -> None:
        """Use a Samsung IoT token for image downloads (the file endpoint rejects OAuth tokens)."""
        self._samsung_iot_headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.smartthings+json;v=1",
        }
        if refresh_token is not None:
            self._samsung_iot_refresh_token = refresh_token
        if auth_server is not None:
            self._samsung_iot_auth_server = auth_server

    def _do_samsung_iot_refresh(self) -> None:
        """Refresh the Samsung IoT token in place and report the rotated refresh token."""
        from .auth import refresh_samsung_iot_token

        try:
            credentials = refresh_samsung_iot_token(
                self._samsung_iot_refresh_token, self._samsung_iot_auth_server
            )
        except Exception as err:  # noqa: BLE001 - auth.py raises AuthError/HTTPError/KeyError
            raise AuthenticationError(f"Samsung IoT token refresh failed: {err}") from err
        self.set_samsung_iot_token(
            credentials.access_token,
            refresh_token=credentials.refresh_token,
            auth_server=self._samsung_iot_auth_server,
        )
        if self._on_iot_refresh_token is not None:
            self._on_iot_refresh_token(credentials.refresh_token)

    @staticmethod
    def _is_no_samsung_id_error(response: requests.Response) -> bool:
        return "No samsung id" in response.text

    @property
    def device_id(self) -> str | None:
        if not self._device_id:
            self.set_device_id()
        return self._device_id

    def _check_response(self, response: requests.Response) -> None:
        """Raise for authentication failures and rate limiting; warn on other HTTP errors."""
        if response.status_code in (401, 403):
            _LOGGER.error(
                "SmartThings authentication failed (HTTP %s). The token may have expired; "
                "SmartThings personal access tokens expire after 24 hours",
                response.status_code,
            )
            raise AuthenticationError(
                f"SmartThings API returned HTTP {response.status_code}. Token is expired or invalid."
            )
        if response.status_code == 429:
            raise RateLimitError("SmartThings API returned HTTP 429")
        if response.status_code == 400:
            try:
                error = response.json().get("error", {})
            except (ValueError, AttributeError):
                error = {}
            if error.get("code") == "BadRequestError" and "No samsung id" in error.get("message", ""):
                raise AuthenticationError(
                    "No samsung id available: switch to Standalone OAuth or add Samsung Account credentials"
                )
        if not response.ok:
            _LOGGER.warning("SmartThings API request failed: HTTP %s", response.status_code)

    def set_device_status(self, status):
        """Update device status list if the payload is valid."""
        if not status or not isinstance(status, dict) or "error" in status:
            return
        self._device_status = status
        self.set_device_id()

    def set_current_device_status(self, status):
        """Update current device status if the payload is valid."""
        if not status or not isinstance(status, dict) or "error" in status:
            return
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
        if not file_ids:
            return False
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
                if not r.ok or not r.content:
                    raise RuntimeError(f"Download failed with status {r.status_code}")
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
            except (AuthenticationError, RateLimitError):
                # Let the coordinator handle reauth / rate limiting.
                raise
            except (requests.RequestException, RuntimeError) as err:
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

    def get_location_id(self) -> str | None:
        """Get the SmartThings locationId for the fridge device."""
        if self._location_id:
            return self._location_id
        if not self.device_id:
            return None
        try:
            r = requests.get(
                f"https://api.smartthings.com/v1/devices/{self.device_id}",
                headers=self._headers,
                timeout=DEFAULT_TIMEOUT,
            )
            if r.ok:
                loc = r.json().get("locationId")
                if loc:
                    _LOGGER.debug("Discovered SmartThings locationId for fridge")
                    self._location_id = loc
                    return loc
        except (requests.RequestException, ValueError) as err:
            _LOGGER.debug("Could not fetch locationId for device %s: %s", self.device_id, err)
        return None

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
                "Response keys: %s",
                sorted(self._device_status) if isinstance(self._device_status, dict) else type(self._device_status).__name__,
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


