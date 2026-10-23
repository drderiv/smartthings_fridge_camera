"""Integration tests for Samsung IoT token in-session refresh (SCRUM-84).

Verifies that download_images() silently refreshes an expired Samsung IoT
token and retries the download rather than surfacing ConfigEntryAuthFailed.
"""
from __future__ import annotations

import pytest
from custom_components.samsung_familyhub_fridge.api import (
    AuthenticationError,
    FamilyHub,
)
from custom_components.samsung_familyhub_fridge.const import CID

# Minimal device status that produces one file_id
DEVICE_STATUS = {
    "samsungce.viewInside": {
        "contents": {"value": [{"fileId": "file-abc123"}]}
    }
}
DEVICE_ID = "device-123"
FILE_ID = "file-abc123"
SAMSUNG_IOT_REFRESH_URL = "https://us-auth2.samsungosp.com/auth/oauth2/token"
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 100


def _file_link_url(file_id: str = FILE_ID, device_id: str = DEVICE_ID) -> str:
    return (
        f"https://client.smartthings.com/udo/file_links/{file_id}"
        f"?cid={CID}&di={device_id}"
    )


# ---------------------------------------------------------------------------
# Test 1: hub has IoT token + refresh token — 401 → refresh → retry → success
# ---------------------------------------------------------------------------


def _hub(session, **kwargs):
    hub = FamilyHub(session, token="st-token", device_id=DEVICE_ID, **kwargs)
    hub._current_device_status = DEVICE_STATUS
    return hub


class TestIoTRefreshOnAuthFailure:
    """401 during download triggers silent refresh and successful retry."""

    async def test_download_retries_after_401_and_persists_new_refresh_token(self, session, mocked):
        """401 on first IoT download → refresh → retry returns image → True."""
        persisted = []
        hub = _hub(session, on_iot_refresh_token=persisted.append)
        hub.set_samsung_iot_token(
            "old-iot-access-token",
            refresh_token="old-iot-refresh-token",
            auth_server="https://us-auth2.samsungosp.com",
        )

        url = _file_link_url()
        # First call: 401 (token expired); second call: 200 + JPEG (after refresh)
        mocked.get(url, status=401, payload={"error": "Unauthorized"}, repeat=False)
        mocked.get(url, status=200, body=JPEG_BYTES, headers={"content-type": "image/jpeg"}, repeat=False)
        mocked.post(
            SAMSUNG_IOT_REFRESH_URL,
            payload={
                "access_token": "new-iot-access-token",
                "refresh_token": "new-iot-refresh-token",
            },
        )

        result = await hub.download_images()

        assert result is True, "download_images() must return True on successful retry"
        assert hub.downloaded_images[0] == JPEG_BYTES
        assert hub._samsung_iot_refresh_token == "new-iot-refresh-token", (
            "In-memory refresh token must be updated after successful refresh"
        )
        assert hub._samsung_iot_headers["Authorization"] == "Bearer new-iot-access-token"
        # The rotated refresh token is handed to the owner to persist
        assert persisted == ["new-iot-refresh-token"]

    async def test_download_retries_after_400_no_samsung_id(self, session, mocked):
        """400 with 'No samsung id' triggers refresh and retry."""
        hub = _hub(session)
        hub.set_samsung_iot_token(
            "old-iot-token",
            refresh_token="old-refresh",
            auth_server="https://us-auth2.samsungosp.com",
        )

        url = _file_link_url()
        mocked.get(
            url,
            status=400,
            payload={"error": "BadRequestError", "message": "No samsung id available"},
            repeat=False,
        )
        mocked.get(url, status=200, body=JPEG_BYTES, headers={"content-type": "image/jpeg"}, repeat=False)
        mocked.post(
            SAMSUNG_IOT_REFRESH_URL,
            payload={"access_token": "new-iot-access", "refresh_token": "new-refresh"},
        )

        assert await hub.download_images() is True
        assert hub._samsung_iot_refresh_token == "new-refresh"

    async def test_failed_refresh_raises_authentication_error(self, session, mocked):
        hub = _hub(session)
        hub.set_samsung_iot_token("old", refresh_token="expired", auth_server="https://us-auth2.samsungosp.com")
        mocked.get(_file_link_url(), status=401)
        mocked.post(SAMSUNG_IOT_REFRESH_URL, status=401)

        with pytest.raises(AuthenticationError):
            await hub.download_images()


# ---------------------------------------------------------------------------
# Test 2: hub has no _samsung_iot_refresh_token — 401 must raise immediately
# ---------------------------------------------------------------------------


class TestIoTAuthFailureWithoutRefreshToken:
    """No refresh token: 401 raises AuthenticationError immediately, no retry."""

    async def test_401_raises_auth_error_without_retry(self, session, mocked):
        """Without a refresh token, 401 raises AuthenticationError after one request."""
        hub = _hub(session)
        hub.set_samsung_iot_token("iot-access-only")  # IoT token but NO refresh_token

        mocked.get(_file_link_url(), status=401)

        with pytest.raises(AuthenticationError):
            await hub.download_images()

        # Exactly one HTTP GET made — no retry attempted
        assert len(mocked.calls) == 1, "Must not retry when no refresh token is available"
