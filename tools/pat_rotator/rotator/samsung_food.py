"""Capture the Samsung Food (Whisk) API token from app.samsungfood.com."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import requests
from playwright.sync_api import Error as PlaywrightError

from .browser import browser_context, click_first_visible, first_visible, save_session
from .errors import RotatorError
from .files import write_secret
from .samsung_login import CONSENT_BUTTONS, fill_credentials, submit

_LOGGER = logging.getLogger("rotator.food")

APP_URL = "https://app.samsungfood.com"
FOODLIST_URL = "https://api.whisk.com/foodlist/v2"
API_HOSTS = ("api.whisk.com", "api.samsungfood.com", "whisk.com", "samsungfood.com")
LOGIN_BUTTON = ("button:has-text('Log in')", "a:has-text('Log in')")
SAMSUNG_BUTTON = "button:has-text('Samsung')"
LOCAL_STORAGE_HINTS = ("token", "auth", "whisk", "jwt", "user")
COOKIE_HINTS = ("auth", "token", "whisk", "jwt")


def split_bearer(value: str) -> str:
    """The raw token without a 'Bearer ' / 'Token ' prefix."""
    return value.strip().removeprefix("Bearer ").removeprefix("Token ").strip()


def is_valid_token(raw_token: str) -> bool:
    """True if Whisk accepts the token for the food list."""
    try:
        response = requests.get(
            FOODLIST_URL,
            headers={
                "Authorization": f"Bearer {raw_token}",
                "x-whisk-token": raw_token,
                "Accept": "application/json",
            },
            params={"page_size": 10},
            timeout=10,
        )
    except requests.RequestException as err:
        _LOGGER.warning("Could not validate Samsung Food token: %s", err)
        return False
    return response.ok


def generate_food_token(
    email: str,
    password: str,
    output_file: Path,
    session_file: Path,
    *,
    headless: bool = True,
) -> str:
    """Sign in to Samsung Food, harvest candidate tokens, keep the first one Whisk accepts."""
    candidates: set[str] = set()

    with browser_context(headless, session_file) as context:
        page = context.new_page()

        def on_request(request) -> None:
            if any(host in request.url for host in API_HOSTS):
                header = request.headers.get("authorization") or request.headers.get("x-whisk-token")
                if header and "anon_" not in header[:12]:
                    candidates.add(split_bearer(header))

        page.on("request", on_request)

        try:
            page.goto(APP_URL, timeout=60_000)
            page.wait_for_timeout(5000)
            click_first_visible(page, LOGIN_BUTTON)
            page.wait_for_timeout(3000)

            auth_page = _open_samsung_login(page)
            page.wait_for_timeout(1000)
            fill_credentials(auth_page, email, password)
            for _ in range(8):  # consent screens
                if auth_page.is_closed() or click_first_visible(auth_page, CONSENT_BUTTONS + ("button:has-text('Allow')",)):
                    break
                page.wait_for_timeout(1000)
            if auth_page is not page and not auth_page.is_closed():
                try:
                    auth_page.wait_for_event("close", timeout=15_000)
                except PlaywrightError:
                    pass
            page.wait_for_timeout(5000)

            for path in ("/pantry", "/food-list"):  # visiting these makes the app call the API
                try:
                    page.goto(f"{APP_URL}{path}", timeout=20_000)
                    page.wait_for_timeout(3000)
                except PlaywrightError:
                    continue

            candidates |= _tokens_from_storage(page)
            for cookie in context.cookies():
                if any(hint in cookie.get("name", "").lower() for hint in COOKIE_HINTS):
                    candidates.add(split_bearer(cookie.get("value", "")))
            save_session(context, session_file)
        except PlaywrightError as err:
            _LOGGER.warning("Browser problem while fetching the Samsung Food token: %s", err)

    for candidate in sorted(candidates, key=len, reverse=True):
        if candidate and is_valid_token(candidate):
            write_secret(output_file, candidate)
            _LOGGER.info("Wrote validated Samsung Food token to %s", output_file.name)
            return candidate
    raise RotatorError(
        f"No valid Samsung Food token found ({len(candidates)} candidates checked). "
        "Try again with --no-headless to see the login."
    )


def _open_samsung_login(page):
    """Click 'Continue with Samsung'; returns the popup if one opens, else the page."""
    try:
        with page.expect_popup(timeout=12_000) as popup:
            page.get_by_text("Continue with Samsung", exact=False).first.click()
        return popup.value
    except PlaywrightError:
        locator = first_visible(page, (SAMSUNG_BUTTON,))
        if locator is not None:
            try:
                locator.click(force=True)
            except PlaywrightError:
                pass
        page.wait_for_timeout(3000)
        return page


def _tokens_from_storage(page) -> set[str]:
    try:
        stored = json.loads(page.evaluate("() => JSON.stringify(window.localStorage)"))
    except (PlaywrightError, ValueError):
        return set()
    found = set()
    for key, value in stored.items():
        text = str(value)
        if any(hint in key.lower() for hint in LOCAL_STORAGE_HINTS) and (
            "bearer " in text.lower() or len(text) > 30
        ):
            found.add(split_bearer(text))
    return found
