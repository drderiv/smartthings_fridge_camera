"""Samsung Account sign-in on a Playwright page (email, password, consent, 2FA)."""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from pathlib import Path

from playwright.sync_api import Page

from .browser import click_first_visible, first_visible, scopes, wait_for
from .errors import InteractiveAuthRequired, RotatorError
from .files import write_secret

_LOGGER = logging.getLogger("rotator.login")

# Element selectors observed on Samsung's sign-in pages.
EMAIL_FIELDS = (
    "input#account", "input#iptLgnPlnID", "input#signInId",
    "input[name='iptLgnPlnID']", "input[type='email']", "input[name='email']",
)
PASSWORD_FIELDS = (
    "input#password", "input#iptLgnPD", "input[name='iptLgnPD']", "input[type='password']",
)
NEXT_BUTTONS = ("button#signInButton", "button:has-text('Next')", "button:has-text('Sign in')")
SUBMIT_BUTTONS = (
    "button#signInButton", "button[type='submit']",
    "button:has-text('Sign in')", "button:has-text('Log in')",
)
CODE_FIELDS = (
    "input[name*='code' i]", "input[placeholder*='code' i]", "input#code",
    "input#verificationCode", "input[type='tel']", "input[type='number']",
)
VERIFY_BUTTONS = (
    "button:has-text('Verify')", "button:has-text('Submit')",
    "button:has-text('Next')", "button[type='submit']",
)
TRUST_DEVICE = (
    "input#trustDevice", "input[name*='trust' i]", "input[name*='remember' i]",
    "label:has-text('Trust this device')", "label:has-text('Don\\'t ask again')",
    "label:has-text('Don’t ask again')",
)
SMS_OPTIONS = (
    "button:has-text('Verify with text message')", "a:has-text('Verify with text message')",
    "button:has-text('Send text message')", "a:has-text('Send text message')",
    "button:has-text('Use text message')", "a:has-text('Use text message')",
    "button:has-text('Text message')", "a:has-text('Text message')",
)
ANOTHER_WAY = (
    "button:has-text('Verify another way')", "a:has-text('Verify another way')",
    "button:has-text('Try another way')", "a:has-text('Try another way')",
    "button:has-text('Didn\\'t get a notification?')", "a:has-text('Didn\\'t get a notification?')",
    "button:has-text('Didn’t get a notification?')", "a:has-text('Didn’t get a notification?')",
)
SMS_CHOICES = (
    "label:has-text('Text message')", "button:has-text('Text message')",
    "input[type='radio'][value*='sms' i]", "input[type='radio'][value*='text' i]",
    "button:has-text('Send code')", "button:has-text('Send text')",
)
CONFIRM_BUTTONS = (
    "button:has-text('Send')", "button:has-text('Send code')", "button:has-text('Next')",
    "button:has-text('Continue')", "button:has-text('Confirm')",
)
# Consent / onboarding screens that must be accepted to continue.
CONSENT_BUTTONS = (
    "button:has-text('Continue')", "a:has-text('Continue')",
    "button:has-text('Agree and continue')", "button:has-text('Agree to all and continue')",
    "button:has-text('I Agree')", "button:has-text('Agree')", "button:has-text('Accept')",
    "button#agreeBtn", "button#accept-btn",
)
AGREE_ALL = (
    "input#agreeAll", "input[name='agreeAll']",
    "label:has-text('Agree to all')", "label:has-text('I agree to all')",
)
# Optional screens (password reminders, promos) that are safe to postpone.
POSTPONE_BUTTONS = (
    "button[data-log-id='not-now']", "button:has-text('Not now')", "a:has-text('Not now')",
    "button:has-text('Later')", "button:has-text('Remind me later')", "button:has-text('Cancel')",
)
VERIFICATION_HINTS = (
    "two-step", "two-factor", "2-step", "verification code", "security code",
    "galaxy device notification", "check your galaxy", "check your phone to verify",
)
CAPTCHA_HINTS = ("recaptcha", "g-recaptcha", "challenge-running", "cf-turnstile", "captcha")

DEVICE_APPROVAL_TIMEOUT_S = 60
PASSWORD_STEP_TIMEOUT_S = 30

Prompt = Callable[[str], str]


def is_interactive() -> bool:
    return sys.stdin.isatty()


def submit(page: Page, buttons: tuple[str, ...]) -> None:
    """Click the first visible button, else press Enter."""
    if not click_first_visible(page, buttons):
        page.keyboard.press("Enter")


def accept_screens(page: Page, buttons: tuple[str, ...], rounds: int = 3) -> None:
    """Click through consent-style screens until none is visible."""
    for _ in range(rounds):
        for target in scopes(page):
            box = first_visible(target, AGREE_ALL)
            if box is not None:
                box.click()
        if not click_first_visible(scopes(page), buttons):
            return
        page.wait_for_timeout(3000)


def fill_credentials(page: Page, email: str, password: str) -> None:
    """Fill whichever of the email / password steps is currently showing."""
    field = first_visible(scopes(page), EMAIL_FIELDS)
    if field is not None and email:
        field.fill(email)
        submit(page, NEXT_BUTTONS)
        # The password step loads as a separate page; wait for it rather than guessing.
        wait_for(page, lambda: first_visible(scopes(page), PASSWORD_FIELDS) is not None, PASSWORD_STEP_TIMEOUT_S)
    field = first_visible(scopes(page), PASSWORD_FIELDS)
    if field is not None and password:
        field.fill(password)
        submit(page, SUBMIT_BUTTONS)
        page.wait_for_timeout(3000)


def _page_mentions(page: Page, hints: tuple[str, ...]) -> bool:
    text = page.content().lower()
    return any(hint in text for hint in hints)


def needs_verification(page: Page) -> bool:
    return _page_mentions(page, VERIFICATION_HINTS) or first_visible(scopes(page), CODE_FIELDS) is not None


def sign_in(
    page: Page,
    *,
    email: str,
    password: str,
    signed_in: Callable[[], bool],
    method: str = "device",
    interactive: bool | None = None,
    prompt: Prompt = input,
    headless: bool = True,
) -> None:
    """Drive the Samsung sign-in form until ``signed_in()`` is true.

    Raises InteractiveAuthRequired when a human is needed but none is available.
    """
    interactive = is_interactive() if interactive is None else interactive
    if not email or not password:
        raise RotatorError("Samsung email and password are required when no saved session exists")

    form = first_visible(scopes(page), EMAIL_FIELDS)
    if form is None:
        raise RotatorError("Could not find the Samsung Account email field")
    fill_credentials(page, email, password)
    accept_screens(page, CONSENT_BUTTONS)

    if not signed_in():
        if _page_mentions(page, CAPTCHA_HINTS) and headless:
            raise InteractiveAuthRequired("Samsung presented a CAPTCHA")
        if needs_verification(page):
            if not interactive:
                raise InteractiveAuthRequired("two-factor verification required")
            _verify(page, signed_in, method, prompt)
        accept_screens(page, CONSENT_BUTTONS + POSTPONE_BUTTONS, rounds=4)


def _verify(page: Page, signed_in: Callable[[], bool], method: str, prompt: Prompt) -> None:
    """Handle the two-factor step: push approval on a Galaxy device, or a typed code."""
    method = method.lower()
    use_sms = method in ("sms", "text")
    if use_sms:
        _LOGGER.info("Requesting the 2FA code by text message")
        if not click_first_visible(scopes(page), SMS_OPTIONS) and click_first_visible(scopes(page), ANOTHER_WAY):
            page.wait_for_timeout(2000)
            click_first_visible(scopes(page), SMS_CHOICES)
            click_first_visible(scopes(page), CONFIRM_BUTTONS)
            page.wait_for_timeout(3000)

    click_first_visible(scopes(page), TRUST_DEVICE)

    if not use_sms:
        print("\nApprove the sign-in on your Galaxy phone/tablet (tap 'Yes').")
        if wait_for(page, signed_in, DEVICE_APPROVAL_TIMEOUT_S):
            return
        _save_prompt_screenshot(page)
        code = prompt("Enter the 2FA verification code (or press Enter if you approved on the device): ")
    else:
        _save_prompt_screenshot(page)
        code = prompt("Enter the 6-digit 2FA code sent by text message: ")

    code = code.strip()
    if not code:
        return
    field = None
    if wait_for(page, lambda: first_visible(scopes(page), CODE_FIELDS) is not None, 10):
        field = first_visible(scopes(page), CODE_FIELDS)
    if field is None:
        raise RotatorError("Could not find the 2FA code field")
    field.fill(code)
    submit(page, VERIFY_BUTTONS)
    page.wait_for_timeout(3000)


def _save_prompt_screenshot(page: Page) -> None:
    """Handy when the prompt went to a tablet; stored next to the scripts, owner-only."""
    path = Path(__file__).resolve().parent.parent / "samsung_2fa_prompt.png"
    try:
        write_secret(path, "")  # create with 0600 first; Playwright then overwrites the contents
        page.screenshot(path=str(path))
        print(f"(Screenshot of the prompt saved to {path})")
    except Exception as err:  # noqa: BLE001 - diagnostics only
        _LOGGER.debug("Could not save 2FA screenshot: %s", err)
