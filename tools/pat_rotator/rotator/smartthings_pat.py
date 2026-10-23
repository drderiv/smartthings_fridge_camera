"""Create a SmartThings Personal Access Token through account.smartthings.com."""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from playwright.sync_api import Page
from playwright.sync_api import Error as PlaywrightError

from .browser import KEEP_DEBUG, browser_context, first_visible, save_session, wait_for
from .errors import InteractiveAuthRequired, RotatorError
from .files import write_secret
from .samsung_login import CAPTCHA_HINTS, Prompt, VERIFICATION_HINTS, is_interactive, sign_in

_LOGGER = logging.getLogger("rotator.pat")

TOKENS_URL = "https://account.smartthings.com/tokens"
SESSION_SETTLE_TIMEOUT_S = 15
GENERATE_BUTTON = "button:has-text('Generate new token'), a:has-text('Generate new token')"
SIGNIN_OR_TOKENS = (
    "input#account, input#iptLgnPlnID, input#signInId, input[type='email'], " + GENERATE_BUTTON
)
NAME_FIELDS = (
    "input[name='inputTokenName']", "input#token-name", "input[name='tokenName']",
    "input[placeholder*='Token Name']", "input[type='text']",
)
SUBMIT_BUTTONS = (
    "button#submit", "button:has-text('Generate Token')", "button:has-text('Generate token')",
    "button[type='submit']",
)
TOKEN_ELEMENTS = "code, pre, input[readonly], .token-string, .token-value"
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)


def generate_pat(
    email: str,
    password: str,
    output_file: Path,
    session_file: Path,
    *,
    token_name: str = "ha-fridge-camera",
    two_factor_method: str = "device",
    headless: bool = True,
    prompt: Prompt = input,
) -> str:
    """Return a fresh PAT (also written to ``output_file``)."""
    with browser_context(headless, session_file) as context:
        page = context.new_page()
        try:
            page.goto(TOKENS_URL, timeout=60_000)
            page.wait_for_selector(SIGNIN_OR_TOKENS, timeout=45_000)

            def on_tokens_page() -> bool:
                return page.locator(GENERATE_BUTTON).first.is_visible()

            # The page can show a sign-in form for a moment before it recognises a valid session;
            # give it time to settle, because a needless password login risks a CAPTCHA.
            if on_tokens_page() or wait_for(page, on_tokens_page, SESSION_SETTLE_TIMEOUT_S):
                _LOGGER.info("Signed in with the saved session")
            else:
                _LOGGER.info("No active session; signing in to Samsung")
                sign_in(
                    page, email=email, password=password, signed_in=on_tokens_page,
                    method=two_factor_method, prompt=prompt, headless=headless,
                )
                page.wait_for_selector(GENERATE_BUTTON, timeout=45_000)
                save_session(context, session_file)

            token = _create_token(page, token_name)
            write_secret(output_file, token)
            save_session(context, session_file)
            _LOGGER.info("Wrote new PAT to %s", output_file.name)
            return token
        except (PlaywrightError, RotatorError) as err:
            _explain_failure(page, err, headless)
            raise


def check_session(session_file: Path, *, headless: bool = True) -> bool:
    """True if the saved SmartThings session is still signed in (no password, no PAT created).

    A valid session is saved back, which also lets the server's idle timer slide.
    """
    if not session_file.exists():
        return False
    with browser_context(headless, session_file) as context:
        page = context.new_page()
        try:
            page.goto(TOKENS_URL, timeout=60_000)
            page.wait_for_selector(SIGNIN_OR_TOKENS, timeout=45_000)
        except PlaywrightError as err:
            _LOGGER.debug("Session check could not load the tokens page: %s", err)
            return False

        def on_tokens_page() -> bool:
            return page.locator(GENERATE_BUTTON).first.is_visible()

        if on_tokens_page() or wait_for(page, on_tokens_page, SESSION_SETTLE_TIMEOUT_S):
            save_session(context, session_file)
            return True
        return False


def _create_token(page: Page, token_name: str) -> str:
    page.locator(GENERATE_BUTTON).first.click()
    field = _wait_visible(page, NAME_FIELDS, 15)
    field.fill(f"{token_name}-{time.strftime('%Y%m%d-%H%M%S')}")

    # Scope checkboxes render asynchronously; grant them all.
    page.wait_for_selector("input[type='checkbox']", timeout=15_000)
    for box in page.locator("input[type='checkbox']").all():
        try:
            if not box.is_checked():
                box.set_checked(True, force=True)
        except PlaywrightError:
            box.click(force=True)

    submit = _wait_visible(page, SUBMIT_BUTTONS, 10)
    submit.click()

    token: list[str] = []

    def found() -> bool:
        token[:] = _extract_token(page)
        return bool(token)

    if not wait_for(page, found, 15):
        raise RotatorError("Could not find the generated PAT on the confirmation page")
    return token[0]


def _wait_visible(page: Page, selectors, timeout_s: float):
    found = []

    def check() -> bool:
        locator = first_visible(page, selectors)
        if locator is not None:
            found.append(locator)
        return bool(found)

    if not wait_for(page, check, timeout_s):
        raise RotatorError(f"Expected form control not found: {selectors[0]}")
    return found[0]


def _extract_token(page: Page) -> list[str]:
    """UUID shown on the confirmation page (specific elements first, then page text)."""
    for element in page.locator(TOKEN_ELEMENTS).all():
        try:
            text = element.inner_text() or element.get_attribute("value") or ""
        except PlaywrightError:
            continue
        if match := UUID.search(text):
            return [match.group(0)]
    if match := UUID.search(page.inner_text("body")):
        return [match.group(0)]
    return []


def _explain_failure(page: Page, err: Exception, headless: bool) -> None:
    """Log what the page looked like; turn CAPTCHA / 2FA walls into InteractiveAuthRequired."""
    try:
        _LOGGER.error("PAT generation failed at %s ('%s'): %s", page.url, page.title(), err)
        content = page.content().lower()
        if KEEP_DEBUG:  # the tokens page can display a live PAT, so this is opt-in
            base = Path(__file__).resolve().parent.parent
            write_secret(base / "smartthings_pat_error.html", page.content())
            page.screenshot(path=str(base / "smartthings_pat_error.png"))
    except PlaywrightError:
        return
    if headless and any(hint in content for hint in CAPTCHA_HINTS):
        raise InteractiveAuthRequired("Samsung presented a CAPTCHA") from err
    if headless and not is_interactive() and any(hint in content for hint in VERIFICATION_HINTS):
        raise InteractiveAuthRequired("two-factor verification required") from err
