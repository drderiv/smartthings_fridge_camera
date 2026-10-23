"""SmartThings OAuth 2.0 authentication with token refresh.

SmartThings personal access tokens (PATs) expire after 24 hours.
This module provides two authentication approaches:

1. **Browser-based OAuth** (SmartThingsOAuth):
   Standard OAuth 2.0 Authorization Code + PKCE flow via the SmartThings API.
   Requires one-time browser login; tokens refresh indefinitely.

2. **Headless email/password login** (SamsungAccountAuth):
   Direct Samsung Account authentication using the mobile app API endpoints.
   No browser needed — just email and password. The resulting token works with
   the SmartThings API. Does NOT support 2FA-enabled accounts.

Prerequisites for browser-based flow:
    $ smartthings apps:create   # choose "OAuth-In App"
    Save the resulting client_id and client_secret.

Prerequisites for headless flow:
    Samsung Account email/password and Samsung OAuth client credentials
    (signin_client_id and signin_client_secret from the SmartThings APK).
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
from dataclasses import dataclass, field
from urllib.parse import urlencode, urlparse, parse_qs

import aiohttp

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)
FORM_HEADERS = {"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"}

# --- SmartThings API OAuth endpoints ---
AUTHORIZE_URL = "https://api.smartthings.com/oauth/authorize"
TOKEN_URL = "https://api.smartthings.com/oauth/token"

# --- Samsung Account endpoints (mobile app API) ---
SAMSUNG_AUTH_URL = "https://us-auth2.samsungosp.com/auth/oauth2/requestAuthentication"
SAMSUNG_TOKEN_URL = "https://us-auth2.samsungosp.com/auth/oauth2/authWithTncMandatory"
SAMSUNG_IOT_AUTHORIZE_URL = "https://us-auth2.samsungosp.com/auth/oauth2/v2/authorize"
SAMSUNG_IOT_TOKEN_URL = "https://us-auth2.samsungosp.com/auth/oauth2/token"

# Default scopes needed by the fridge camera integration.
DEFAULT_SCOPES = "r:devices:* w:devices:* x:devices:*"
SAMSUNG_SCOPES = "iot.client+mcs.client+galaxystore.openapi"

# Samsung Account client IDs (from decompiled SmartThings app)
SAMSUNG_LOGIN_CLIENT_ID = "yfrtglt53o"
SAMSUNG_IOT_CLIENT_ID = "6iado3s6jc"

# Default redirect URI — standard Home Assistant OAuth redirect.
DEFAULT_REDIRECT_URI = "https://my.home-assistant.io/redirect/oauth"


def _generate_pkce_pair() -> tuple[str, str]:
    """Generate a PKCE code_verifier and its S256 code_challenge."""
    verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


@dataclass
class OAuthCredentials:
    """Bundle returned by token exchange and refresh."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = 0
    scope: str = ""
    installed_app_id: str = ""


@dataclass
class SmartThingsOAuth:
    """Handles the SmartThings OAuth 2.0 Authorization Code flow."""

    client_id: str
    client_secret: str
    redirect_uri: str = DEFAULT_REDIRECT_URI
    scopes: str = DEFAULT_SCOPES

    # Internal PKCE state (populated by get_authorization_url)
    _code_verifier: str = field(default="", init=False, repr=False)

    # ---- Step 1: Authorization URL ----------------------------------------

    def get_authorization_url(self) -> str:
        """Build the URL the user should open in a browser to authorize."""
        self._code_verifier, code_challenge = _generate_pkce_pair()
        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "scope": self.scopes,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        return f"{AUTHORIZE_URL}?{urlencode(params)}"

    # ---- Step 2: Exchange code for tokens ---------------------------------

    async def exchange_code(
        self, session: aiohttp.ClientSession, authorization_code: str
    ) -> OAuthCredentials:
        """Exchange an authorization code for access + refresh tokens."""
        data = {
            "grant_type": "authorization_code",
            "client_id": self.client_id,
            "code": authorization_code,
            "redirect_uri": self.redirect_uri,
        }
        if self._code_verifier:
            data["code_verifier"] = self._code_verifier

        return await self._token_request(session, data)

    # ---- Step 3: Refresh --------------------------------------------------

    async def refresh(self, session: aiohttp.ClientSession, refresh_token: str) -> OAuthCredentials:
        """Use a refresh token to obtain a new access + refresh token pair."""
        data = {
            "grant_type": "refresh_token",
            "client_id": self.client_id,
            "refresh_token": refresh_token,
        }
        return await self._token_request(session, data)

    def _basic_auth_header(self) -> str:
        credentials = f"{self.client_id}:{self.client_secret}".encode()
        return "Basic " + base64.b64encode(credentials).decode()

    async def _token_request(
        self, session: aiohttp.ClientSession, data: dict[str, str]
    ) -> OAuthCredentials:
        """POST to the token endpoint; HTTP errors raise aiohttp.ClientResponseError."""
        async with session.post(
            TOKEN_URL,
            data=data,
            headers={"Authorization": self._basic_auth_header()},
            timeout=REQUEST_TIMEOUT,
        ) as resp:
            if resp.status == 401:
                _LOGGER.error("SmartThings rejected the credentials (HTTP 401). Re-authorization required.")
            resp.raise_for_status()
            return _parse_token_response(await resp.json(content_type=None))

    # ---- Helpers ----------------------------------------------------------

    @staticmethod
    def extract_code_from_redirect(redirect_url: str) -> str:
        """Pull the ``code`` query parameter from a redirect URL."""
        parsed = urlparse(redirect_url)
        codes = parse_qs(parsed.query).get("code", [])
        if not codes:
            raise ValueError(
                f"No 'code' parameter found in redirect URL: {redirect_url}"
            )
        return codes[0]


def _parse_token_response(data: dict) -> OAuthCredentials:
    return OAuthCredentials(
        access_token=data["access_token"],
        refresh_token=data["refresh_token"],
        token_type=data.get("token_type", "bearer"),
        expires_in=data.get("expires_in", 0),
        scope=data.get("scope", ""),
        installed_app_id=data.get("installed_app_id", ""),
    )


# ======================================================================
# Headless Samsung Account login (email + password)
# ======================================================================


@dataclass
class LoginCredentials:
    """Result of a successful headless Samsung Account login."""

    access_token: str
    userauth_token: str = ""


class AuthError(Exception):
    """Raised when Samsung Account authentication fails."""


@dataclass
class SamsungIoTCredentials:
    """Samsung Account IoT-scoped token pair; works with client.smartthings.com."""

    access_token: str
    refresh_token: str
    auth_server_url: str = "https://us-auth2.samsungosp.com"


@dataclass
class SamsungAccountAuth:
    """Headless Samsung Account authentication using the mobile app API.

    This mimics the SmartThings Android app login flow to obtain a bearer
    token directly from email + password, without any browser interaction.

    NOTE: This does NOT work with accounts that have 2FA/MFA enabled.
    """

    email: str
    password: str
    signin_client_id: str
    signin_client_secret: str
    client_id: str = ""
    auth_url: str = SAMSUNG_AUTH_URL
    token_url: str = SAMSUNG_TOKEN_URL

    # Simulated device fingerprint: a static value identifying the "device" making the request.
    _device_id: str = field(default="", init=False)

    def __post_init__(self) -> None:
        if not self.client_id:
            self.client_id = self.signin_client_id
        self._device_id = base64.urlsafe_b64encode(
            hashlib.sha256(self.email.encode()).digest()[:8]
        ).rstrip(b"=").decode()

    async def login(self, session: aiohttp.ClientSession) -> LoginCredentials:
        """Authenticate with email + password and return a bearer token.

        Two steps: requestAuthentication -> userauth_token, then
        authWithTncMandatory -> access_token (Bearer token).
        """
        userauth_token = await self._request_authentication(session)
        access_token = await self._get_bearer_token(session, userauth_token)
        return LoginCredentials(access_token=access_token, userauth_token=userauth_token)

    async def login_iot(self, session: aiohttp.ClientSession) -> SamsungIoTCredentials:
        """Full login -> IoT-scoped token (works with client.smartthings.com)."""
        userauth_token = await self._request_authentication(session)
        return await get_samsung_iot_token(
            session,
            userauth_token=userauth_token,
            login_id=self.email,
            auth_server_url="https://us-auth2.samsungosp.com",
        )

    async def _request_authentication(self, session: aiohttp.ClientSession) -> str:
        """Step 1: submit email + password to get a userauth_token."""
        physical_addr = f"IMEI%3A{self._device_id}"
        data = {
            "signin_client_id": self.signin_client_id,
            "signin_client_secret": self.signin_client_secret,
            "check_2factor_authentication": "Y",
            "originalAppID": self.client_id,
            "devicePhysicalAddressText": physical_addr,
            "customerCode": "NEE",
            "deviceMultiUserID": "0",
            "phoneNumberText": "",
            "deviceName": "HomeAssistant",
            "client_id": self.client_id,
            "deviceTypeCode": "PHONE DEVICE",
            "password": self.password,
            "deviceUniqueID": physical_addr,
            "scope": SAMSUNG_SCOPES,
            "serviceRequired": "N",
            "physical_address_text": physical_addr,
            "login_id_type": "email_id",
            "mobileCountryCode": "310",
            "mobileNetworkCode": "00",
            "deviceNetworkAddressText": "02%3A00%3A00%3A00%3A00%3A00",
            "service_type": "M",
            "isRegisterDevice": "Y",
            "deviceModelID": "SM-G991B",
            "deviceSerialNumberText": self._device_id,
            "softwareVersion": "RP1A.200720.012",
            "username": self.email,
            "login_id": self.email,
        }
        async with session.post(
            self.auth_url, data=data, headers=FORM_HEADERS, timeout=REQUEST_TIMEOUT
        ) as resp:
            if resp.status == 401:
                _LOGGER.error("Samsung login failed: invalid email or password")
                raise AuthError("Invalid Samsung Account email or password")
            if resp.status == 403:
                _LOGGER.error("Samsung login blocked; the account may require 2FA or CAPTCHA")
                raise AuthError(
                    "Samsung Account login blocked. If 2FA is enabled, "
                    "use the browser-based OAuth flow instead."
                )
            resp.raise_for_status()
            body = await resp.json(content_type=None)

        token = body.get("userauth_token")
        if not token:
            error_msg = body.get("error_description", body.get("error", "no details"))
            raise AuthError(f"Samsung login did not return userauth_token: {error_msg}")
        _LOGGER.debug("Samsung Account authentication successful")
        return token

    async def _get_bearer_token(self, session: aiohttp.ClientSession, userauth_token: str) -> str:
        """Step 2: exchange the userauth_token for a SmartThings bearer token."""
        data = {
            "check_email_validation": "Y",
            "authenticate": "Y",
            "data_collection_accepted": "N",
            "client_id": self.client_id,
            "lang_code": "EN",
            "appId": self.client_id,
            "scope": SAMSUNG_SCOPES,
            "login_id": self.email,
            "package": "com.samsung.android.oneconnect",
            "login_id_type": "email_id",
            "physical_address_text": f"IMEI%3A{self._device_id}",
            "userauth_token": userauth_token,
        }
        async with session.post(
            self.token_url, data=data, headers=FORM_HEADERS, timeout=REQUEST_TIMEOUT
        ) as resp:
            resp.raise_for_status()
            body = await resp.json(content_type=None)
        try:
            access_token = body["token"]["access_token"]
        except (KeyError, TypeError) as err:
            error_msg = body.get("error_description", body.get("error", "no details"))
            raise AuthError(f"Token exchange failed: {error_msg}") from err
        _LOGGER.debug("Samsung bearer token obtained successfully")
        return access_token


async def get_samsung_iot_token(
    session: aiohttp.ClientSession,
    userauth_token: str,
    login_id: str = "",
    auth_server_url: str = "https://us-auth2.samsungosp.com",
) -> SamsungIoTCredentials:
    """Exchange a Samsung Account ``userauth_token`` for an IoT-scoped token.

    The token carries Samsung Account identity and works with ``client.smartthings.com``
    (SmartThings API OAuth tokens get "No samsung id available" there).

    Flow, from the SmartThings Android app:
      1. GET  /auth/oauth2/v2/authorize  -> authorization code
      2. POST /auth/oauth2/token         -> access_token + refresh_token
    """
    verifier, challenge = _generate_pkce_pair()
    device_id = base64.urlsafe_b64encode(os.urandom(8)).rstrip(b"=").decode()

    params = {
        "response_type": "code",
        "client_id": SAMSUNG_IOT_CLIENT_ID,
        "scope": "iot.client",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "userauth_token": userauth_token,
        "serviceType": "M",
        "childAccountSupported": "Y",
        "physical_address_text": device_id,
    }
    if login_id:
        params["login_id"] = login_id

    async with session.get(
        f"{auth_server_url}/auth/oauth2/v2/authorize",
        params=params,
        allow_redirects=False,
        timeout=REQUEST_TIMEOUT,
    ) as resp:
        if resp.status not in (200, 302):
            raise AuthError(f"IoT authorize failed: HTTP {resp.status}")
        auth_code = None
        if resp.status == 302:
            codes = parse_qs(urlparse(resp.headers.get("Location", "")).query).get("code", [])
            auth_code = codes[0] if codes else None
        if not auth_code and resp.content_type == "application/json":
            body = await resp.json(content_type=None)
            auth_code = body.get("code") or body.get("auth_code")
    if not auth_code:
        raise AuthError("IoT authorize did not return a code")
    _LOGGER.debug("Got IoT authorization code")

    async with session.post(
        f"{auth_server_url}/auth/oauth2/token",
        data={
            "grant_type": "authorization_code",
            "client_id": SAMSUNG_IOT_CLIENT_ID,
            "code": auth_code,
            "code_verifier": verifier,
            "physical_address_text": device_id,
        },
        headers=FORM_HEADERS,
        timeout=REQUEST_TIMEOUT,
    ) as resp:
        if resp.status != 200:
            raise AuthError(f"IoT token exchange failed: HTTP {resp.status}")
        body = await resp.json(content_type=None)
    access, refresh = body.get("access_token"), body.get("refresh_token")
    if not access or not refresh:
        missing = [k for k, v in (("access_token", access), ("refresh_token", refresh)) if not v]
        raise AuthError(f"IoT token response missing: {', '.join(missing)}")
    _LOGGER.info("Samsung IoT token obtained successfully")
    return SamsungIoTCredentials(access_token=access, refresh_token=refresh, auth_server_url=auth_server_url)


async def refresh_samsung_iot_token(
    session: aiohttp.ClientSession,
    refresh_token: str,
    auth_server_url: str = "https://us-auth2.samsungosp.com",
) -> SamsungIoTCredentials:
    """Refresh a Samsung IoT token. Returns new access + refresh tokens."""
    async with session.post(
        f"{auth_server_url}/auth/oauth2/token",
        data={
            "grant_type": "refresh_token",
            "client_id": SAMSUNG_IOT_CLIENT_ID,
            "refresh_token": refresh_token,
        },
        headers=FORM_HEADERS,
        timeout=REQUEST_TIMEOUT,
    ) as resp:
        if resp.status == 401:
            raise AuthError("Samsung IoT refresh token expired; re-login required")
        resp.raise_for_status()
        body = await resp.json(content_type=None)
    return SamsungIoTCredentials(
        access_token=body["access_token"],
        refresh_token=body.get("refresh_token", refresh_token),
        auth_server_url=auth_server_url,
    )
