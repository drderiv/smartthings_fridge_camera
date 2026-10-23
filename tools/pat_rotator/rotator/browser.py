"""Playwright helpers shared by the Samsung/SmartThings login flows."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Union

from playwright.sync_api import BrowserContext, Frame, Page
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from .files import write_secret

Scope = Union[Page, Frame]

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
# Samsung's login pages reject obviously automated browsers, so hide the usual tells.
_LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-blink-features=AutomationControlled",
]
_HIDE_WEBDRIVER = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"

KEEP_DEBUG = os.environ.get("PAT_ROTATOR_KEEP_DEBUG") == "1"


@contextmanager
def browser_context(headless: bool, session_path: Path) -> Iterator[BrowserContext]:
    """Chromium context seeded with the saved session cookies (if any)."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=headless, args=_LAUNCH_ARGS, ignore_default_args=["--enable-automation"]
        )
        try:
            options: dict = {
                "user_agent": USER_AGENT,
                "viewport": {"width": 1920, "height": 1080},
                "locale": "en-US",
                "timezone_id": "America/New_York",  # matches the sessions created by earlier versions
            }
            if session_path.exists():
                options["storage_state"] = str(session_path)
            context = browser.new_context(**options)
            context.add_init_script(_HIDE_WEBDRIVER)
            yield context
        finally:
            browser.close()


def save_session(context: BrowserContext, session_path: Path) -> None:
    """Persist cookies/localStorage (owner-only, atomic)."""
    write_secret(session_path, json.dumps(context.storage_state()))


def scopes(page: Page) -> list[Scope]:
    """The page plus all its frames (Samsung login often lives in an iframe)."""
    return [page, *page.frames]


def first_visible(targets: Scope | Iterable[Scope], selectors: Iterable[str]):
    """First visible locator for any selector on any target, else None."""
    if isinstance(targets, (Page, Frame)):
        targets = [targets]
    selectors = list(selectors)
    for target in targets:
        for selector in selectors:
            try:
                locator = target.locator(selector).first
                if locator.is_visible():
                    return locator
            except PlaywrightError:
                continue
    return None


def click_first_visible(targets: Scope | Iterable[Scope], selectors: Iterable[str]) -> bool:
    locator = first_visible(targets, selectors)
    if locator is None:
        return False
    try:
        locator.click()
    except PlaywrightError:
        return False
    return True


def wait_for(page: Page, predicate, timeout_s: float, poll_ms: int = 500) -> bool:
    """Poll ``predicate()`` until true or timeout; uses Playwright's own wait."""
    waited = 0
    while waited <= timeout_s * 1000:
        if predicate():
            return True
        page.wait_for_timeout(poll_ms)
        waited += poll_ms
    return False
