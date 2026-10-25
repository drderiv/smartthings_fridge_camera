"""SmartThings Family Hub fridge client (no Home Assistant dependencies)."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, cast

import aiohttp

from .auth import AuthError, refresh_samsung_iot_token
from .const import CID, DEFAULT_TIMEOUT
from .models import DeviceList, DeviceStatus

_LOGGER = logging.getLogger(__name__)

API_URL = "https://api.smartthings.com/v1"
CLIENT_URL = "https://client.smartthings.com"
SAMSUNG_AUTH_SERVER = "https://us-auth2.samsungosp.com"
TOKEN_REFRESH_MARGIN = 300  # refresh OAuth access tokens this many seconds before expiry
DEFAULT_TOKEN_LIFETIME = 3600
CAMERA_REFRESH_RESOURCE = "/udo/contents/provider/vs/0"


class AuthenticationError(Exception):
    """Raised when SmartThings or Samsung rejects the credentials (HTTP 401/403, bad refresh)."""


class RateLimitError(Exception):
    """Raised when SmartThings answers HTTP 429 (too many requests)."""


class FamilyHub:
    """SmartThings Family Hub fridge API client.

    The caller supplies a bearer token (a PAT, or an OAuth access token) and may attach a
    ``token_refresher`` so OAuth access tokens are renewed before they expire. Network
    failures surface as ``aiohttp.ClientError`` / ``TimeoutError`` / ``ValueError``.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        token: str,
        device_id: str | None = None,
        *,
        on_iot_refresh_token: Callable[[str], None] | None = None,
    ) -> None:
        self._session = session
        self._device_id = device_id
        self.token = token
        self._headers = {"Authorization": f"Bearer {token}"}
        self._device_status: DeviceList | None = None
        self._current_device_status: DeviceStatus | None = None
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

    async def _do_samsung_iot_refresh(self) -> None:
        """Refresh the Samsung IoT token in place and report the rotated refresh token."""
        if not self._samsung_iot_refresh_token:
            raise AuthenticationError("No Samsung IoT refresh token available")
        try:
            credentials = await refresh_samsung_iot_token(
                self._session, self._samsung_iot_refresh_token, self._samsung_iot_auth_server
            )
        except (AuthError, aiohttp.ClientError, TimeoutError, KeyError, ValueError) as err:
            raise AuthenticationError(f"Samsung IoT token refresh failed: {err}") from err
        self.set_samsung_iot_token(
            credentials.access_token,
            refresh_token=credentials.refresh_token,
            auth_server=self._samsung_iot_auth_server,
        )
        if self._on_iot_refresh_token is not None:
            self._on_iot_refresh_token(credentials.refresh_token)

    @property
    def device_id(self) -> str | None:
        if not self._device_id:
            self.set_device_id()
        return self._device_id

    # --- HTTP -----------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        payload: dict[str, Any] | None = None,
    ) -> tuple[int, bytes, Any]:
        """Send a request; returns (status, body, response headers)."""
        async with self._session.request(
            method,
            url,
            headers=headers,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT),
        ) as response:
            return response.status, await response.read(), response.headers

    def _check_response(self, status: int, body: bytes = b"") -> None:
        """Raise for authentication failures and rate limiting; warn on other HTTP errors."""
        if status in (401, 403):
            _LOGGER.error(
                "SmartThings authentication failed (HTTP %s). The token may have expired; "
                "SmartThings personal access tokens expire after 24 hours",
                status,
            )
            raise AuthenticationError(f"SmartThings API returned HTTP {status}. Token is expired or invalid.")
        if status == 429:
            raise RateLimitError("SmartThings API returned HTTP 429")
        if status == 400:
            try:
                error = json.loads(body).get("error", {})
            except (ValueError, AttributeError):
                error = {}
            if (
                isinstance(error, dict)
                and error.get("code") == "BadRequestError"
                and "No samsung id" in error.get("message", "")
            ):
                raise AuthenticationError(
                    "No samsung id available: switch to Standalone OAuth or add Samsung Account credentials"
                )
        if status >= 400:
            _LOGGER.warning("SmartThings API request failed: HTTP %s", status)

    async def _get_json(self, url: str, what: str) -> Any:
        status, body, _headers = await self._request("GET", url, headers=self._headers)
        self._check_response(status, body)
        data = json.loads(body)
        if isinstance(data, dict) and "error" in data:
            _LOGGER.error("SmartThings %s returned an error: %s", what, data["error"])
        return data

    async def get_all_device_status(self) -> DeviceList:
        """Get all of the devices in the account."""
        return cast(DeviceList, await self._get_json(f"{CLIENT_URL}/devices/status", "device list"))

    async def get_current_device_status(self) -> DeviceStatus:
        """Get the current device status."""
        return cast(
            DeviceStatus,
            await self._get_json(f"{API_URL}/devices/{self.device_id}/components/main/status", "device status"),
        )

    async def get_location_id(self) -> str | None:
        """Get the SmartThings locationId for the fridge device."""
        if self._location_id:
            return self._location_id
        if not self.device_id:
            return None
        try:
            status, body, _headers = await self._request(
                "GET", f"{API_URL}/devices/{self.device_id}", headers=self._headers
            )
            if status == 200 and (location := json.loads(body).get("locationId")):
                _LOGGER.debug("Discovered SmartThings locationId for fridge")
                self._location_id = location
                return location
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            _LOGGER.debug("Could not fetch locationId for device %s: %s", self.device_id, err)
        return None

    async def update_camera(self) -> None:
        """Send the refresh command to the fridge.

        The single OCF resource at /udo/contents/provider/vs/0 holds all of the camera
        images; the coordinator sends this on door-close events.
        """
        if not self.device_id:
            return
        status, body, _headers = await self._request(
            "POST",
            f"{API_URL}/devices/{self.device_id}/commands",
            headers=self._headers,
            payload={
                "commands": [
                    {
                        "component": "main",
                        "capability": "execute",
                        "command": "execute",
                        "arguments": [
                            CAMERA_REFRESH_RESOURCE,
                            {"x.com.samsung.da.control": {"x.com.samsung.da.command": "refresh"}},
                        ],
                    }
                ]
            },
        )
        self._check_response(status, body)
        _LOGGER.debug("update_camera: status=%s body=%s", status, body[:300])

    # --- status handling --------------------------------------------------------------

    def set_device_status(self, status: DeviceList | None) -> None:
        """Update the device list if the payload is valid."""
        if not status or not isinstance(status, dict) or "error" in status:
            return
        self._device_status = status
        self.set_device_id()

    def set_current_device_status(self, status: DeviceStatus | None) -> None:
        """Update the current device status if the payload is valid."""
        if not status or not isinstance(status, dict) or "error" in status:
            return
        self._current_device_status = status

    def set_device_id(self) -> None:
        """Extract the fridge's device ID from the device list."""
        if not self._device_status:
            return
        try:
            items = self._device_status["items"]
        except (KeyError, TypeError):
            _LOGGER.error(
                "Unexpected device status format: missing 'items'. This may indicate an expired "
                "token or API error. Response keys: %s",
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

    def extract_device_data(self) -> None:
        """Detect door-close events from the contact sensor."""
        if not self._current_device_status:
            return
        try:
            contact = self._current_device_status["contactSensor"]["contact"]
        except KeyError:
            _LOGGER.debug("contactSensor data not available in device status")
            return
        _LOGGER.debug(
            "contactSensor: value=%s timestamp=%s (last_closed=%s)",
            contact.get("value"), contact.get("timestamp"), self.last_closed,
        )
        # Refresh on the first poll after startup so users see fresh images without having to
        # physically open and close the door.
        first_poll = self.last_closed is None
        if contact["value"] == "closed" and (first_poll or contact["timestamp"] != self.last_closed):
            self.last_closed = contact["timestamp"]
            self.should_update = True
            if first_poll:
                _LOGGER.debug("First poll after startup; requesting a camera refresh")

    def get_file_ids(self) -> list[str]:
        """File IDs of the current camera images."""
        if not self._current_device_status:
            return []
        try:
            element = self._current_device_status["samsungce.viewInside"]["contents"]
            return [item["fileId"] for item in element["value"]]
        except (KeyError, TypeError):
            _LOGGER.debug("samsungce.viewInside data not available in device status")
            return []

    # --- images -----------------------------------------------------------------------

    async def _download(self, url: str) -> bytes:
        """Download one image, refreshing the Samsung IoT token once if it was rejected."""
        headers = self._samsung_iot_headers or self._headers
        status, body, _response_headers = await self._request("GET", url, headers=headers)
        if headers is self._samsung_iot_headers and (
            status in (401, 403) or (status == 400 and b"No samsung id" in body)
        ):
            if not self._samsung_iot_refresh_token:
                raise AuthenticationError(
                    f"Samsung IoT authentication failed (HTTP {status}); no refresh token available."
                )
            await self._do_samsung_iot_refresh()
            status, body, _response_headers = await self._request(
                "GET", url, headers=self._samsung_iot_headers
            )
        self._check_response(status, body)
        if status >= 400 or not body:
            raise RuntimeError(f"Download failed with status {status}")
        return body

    async def download_images(self) -> bool:
        """Download the camera images; True if at least one succeeded.

        A failed download keeps the previous image for that slot, so one transient error
        doesn't wipe the others.
        """
        if not self._current_device_status or not self.device_id:
            return False
        file_ids = self.get_file_ids()
        if not file_ids:
            return False

        result = list(self.downloaded_images)
        result.extend([None] * (len(file_ids) - len(result)))
        successes = 0
        for index, file_id in enumerate(file_ids):
            url = f"{CLIENT_URL}/udo/file_links/{file_id}?cid={CID}&di={self.device_id}"
            try:
                result[index] = await self._download(url)
                successes += 1
            except (AuthenticationError, RateLimitError):
                raise  # the coordinator handles reauth / rate limiting
            except (aiohttp.ClientError, TimeoutError, RuntimeError) as err:
                _LOGGER.warning("download_images[%d]: failed to download file_id=%s: %s", index, file_id[:8], err)

        self.downloaded_images = result
        _LOGGER.debug(
            "download_images: stored %d/%d images, sizes=%s",
            successes, len(file_ids), [len(image) if image else 0 for image in result],
        )
        return successes > 0
