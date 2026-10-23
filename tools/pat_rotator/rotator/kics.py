"""KICS Food Circles token: rolling OAuth refresh, plus a one-time browser bootstrap."""

from __future__ import annotations

import logging
import urllib.parse
from pathlib import Path
from typing import Any

import requests

from .config import read_location_id
from .errors import NotReady, RotatorError
from .files import Paths, read_token, write_secret

_LOGGER = logging.getLogger("rotator.kics")

CLIENT_ID = "qumapq783u"
AUTH_BASE = "https://us-auth2.samsungosp.com"
IAM_AUTHORIZE_URL = "https://account.samsung.com/iam/oauth2/authorize"
REDIRECT_URI = "https://kics-epa.samsungepa.com/api/oauth/callback"
FOODLIST_URL = "https://kics-epa.samsungepa.com/api/foodlist"
USER_AGENT = "Mozilla/5.0 (Linux; Android 17; sdk_gphone16k_x86_64) AppleWebKit/537.36"
BOOTSTRAP_TIMEOUT_S = 60


def validate_token(token: str, location_id: str = "") -> bool:
    """True if the KICS API accepts this access token."""
    token = token.strip()
    if not token:
        return False
    headers = {
        "authorization": token if token.startswith("Bearer ") else f"Bearer {token}",
        "kics-sa-auth-url": AUTH_BASE.removeprefix("https://"),
        "kics-sa-app-id": CLIENT_ID,
        "kics-location": location_id,
        "kics-client-platform": "android",
        "kics-client-version": "KS.ST.2.11-20",
        "accept": "application/json, text/plain, */*",
        "user-agent": USER_AGENT,
    }
    try:
        return requests.get(FOODLIST_URL, headers=headers, timeout=10).status_code == 200
    except requests.RequestException as err:
        _LOGGER.warning("Could not validate KICS token: %s", err)
        return False


def _token_request(form: dict[str, str]) -> dict[str, Any]:
    response = requests.post(
        f"{AUTH_BASE}/auth/oauth2/token",
        data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": USER_AGENT},
        timeout=15,
    )
    if not response.ok:
        raise RotatorError(f"Samsung token endpoint returned HTTP {response.status_code}")
    payload = response.json()
    if not payload.get("access_token"):
        raise RotatorError(f"Samsung token response has no access_token (keys: {sorted(payload)})")
    return payload


def refresh(refresh_token: str) -> dict[str, Any]:
    """Exchange the rolling refresh token for a new access + refresh token."""
    if not refresh_token.strip():
        raise ValueError("empty refresh token")
    return _token_request(
        {"grant_type": "refresh_token", "client_id": CLIENT_ID, "refresh_token": refresh_token.strip()}
    )


def _save(payload: dict[str, Any], paths: Paths, output: Path, refresh_file: Path) -> str:
    write_secret(output, payload["access_token"])
    if payload.get("refresh_token"):
        write_secret(refresh_file, payload["refresh_token"])
    return payload["access_token"]


def rotate(
    paths: Paths,
    *,
    seed_refresh_token: str = "",
    output: Path | None = None,
    refresh_file: Path | None = None,
) -> str:
    """Refresh the access token using the stored (or seed) rolling refresh token."""
    output = output or paths.kics
    refresh_file = refresh_file or paths.kics_refresh
    current = read_token(refresh_file) or seed_refresh_token.strip()

    if not current:
        existing = read_token(output) or read_token(paths.kics_legacy)
        if existing and validate_token(existing, read_location_id(paths.config) or ""):
            _LOGGER.info("No refresh token, but the existing KICS access token is still valid")
            return existing
        raise NotReady("no KICS refresh token (kics_refresh_token.txt or config.json)")

    _LOGGER.info("Refreshing the KICS access token")
    return _save(refresh(current), paths, output, refresh_file)


def bootstrap(paths: Paths, *, headless: bool = True, email: str = "", password: str = "") -> str:
    """One-time: obtain the first access/refresh tokens through Samsung web SSO.

    Reuses the saved Samsung session; credentials are only used if a login form appears.
    """
    from .browser import browser_context, click_first_visible, save_session
    from .samsung_login import CONSENT_BUTTONS, fill_credentials

    authorize = (
        f"{IAM_AUTHORIZE_URL}?client_id={CLIENT_ID}&response_type=code"
        f"&redirect_uri={urllib.parse.quote(REDIRECT_URI, safe='')}"
    )
    captured: list[str] = []

    def look_for_code(url: str) -> None:
        if "kics-epa.samsungepa.com" in url and not captured:
            codes = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("code")
            if codes:
                captured.append(codes[0])

    with browser_context(headless, paths.session) as context:
        page = context.new_page()
        page.on("request", lambda request: look_for_code(request.url))
        page.on("response", lambda response: look_for_code(response.url))
        page.goto(authorize, timeout=60_000)
        for _ in range(BOOTSTRAP_TIMEOUT_S // 3):
            look_for_code(page.url)
            if captured:
                break
            click_first_visible(page, CONSENT_BUTTONS + ("button:has-text('Allow')",))
            if email and password:
                fill_credentials(page, email, password)
            page.wait_for_timeout(3000)
        save_session(context, paths.session)

    if not captured:
        raise RotatorError(
            "Could not capture the Samsung authorization code. Run with --no-headless, "
            "or make sure smartthings_session.json holds an active login."
        )
    payload = _token_request({
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "code": captured[0],
        "redirect_uri": REDIRECT_URI,
    })
    return _save(payload, paths, paths.kics, paths.kics_refresh)


def ensure(paths: Paths, *, seed_refresh_token: str = "", headless: bool = True) -> str:
    """Refresh if we hold a refresh token; otherwise bootstrap from the saved session."""
    if read_token(paths.kics_refresh) or seed_refresh_token:
        return rotate(paths, seed_refresh_token=seed_refresh_token)
    if paths.session.exists():
        _LOGGER.info("No KICS refresh token; bootstrapping from the saved Samsung session")
        return bootstrap(paths, headless=headless)
    return rotate(paths)  # returns a still-valid token, or raises NotReady
