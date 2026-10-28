"""Samsung sign-in: wait for the outcome and react only to what is visible (not to page text)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tools.pat_rotator.rotator import samsung_login as login
from tools.pat_rotator.rotator.errors import InteractiveAuthRequired, RotatorError


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now


class FakePage:
    """Just enough page for sign_in(); its HTML mentions reCAPTCHA, as Samsung's real page does."""

    url = "https://account.smartthings.com/tokens"

    def __init__(self, clock):
        self.clock = clock
        self.polls = 0

    def wait_for_timeout(self, ms):
        self.polls += 1
        self.clock.now += ms / 1000

    def content(self):
        return "<script>No valid parameters found, using default reCAPTCHA URL</script>"


@pytest.fixture
def env(monkeypatch):
    clock = Clock()
    page = FakePage(clock)
    monkeypatch.setattr(login, "time", SimpleNamespace(monotonic=clock.monotonic))
    monkeypatch.setattr(login, "scopes", lambda p: [])
    monkeypatch.setattr(login, "fill_credentials", lambda *a, **k: None)
    monkeypatch.setattr(login, "click_first_visible", lambda *a, **k: False)
    monkeypatch.setattr(login, "first_visible", lambda targets, sels: object() if sels is login.EMAIL_FIELDS else None)
    monkeypatch.setattr(login, "captcha_visible", lambda p: False)
    monkeypatch.setattr(login, "needs_verification", lambda p: False)
    return page, clock, monkeypatch


def run(page, signed_in, **kw):
    login.sign_in(page, email="e@x.y", password="pw", signed_in=signed_in, interactive=kw.pop("interactive", False), **kw)


def test_slow_tokens_page_is_waited_for_not_mistaken_for_a_captcha(env):
    """Regression: the page text mentions reCAPTCHA on every successful login."""
    page, clock, _ = env
    run(page, lambda: page.polls >= 6)                 # the tokens page appears after ~6 s
    assert page.polls == 6


def test_visible_captcha_stops_unattended_logins(env):
    page, _, monkeypatch = env
    monkeypatch.setattr(login, "captcha_visible", lambda p: True)
    with pytest.raises(InteractiveAuthRequired, match="CAPTCHA"):
        run(page, lambda: False)


def test_visible_verification_stops_unattended_logins(env):
    page, _, monkeypatch = env
    monkeypatch.setattr(login, "needs_verification", lambda p: True)
    with pytest.raises(InteractiveAuthRequired, match="two-factor"):
        run(page, lambda: False)


def test_terms_update_page_stops_headless_logins_with_instructions(env):
    page, _, _ = env
    page.url = "https://account.samsung.com/iam/sign-in/status/terms"
    with pytest.raises(InteractiveAuthRequired, match="--no-headless"):
        run(page, lambda: False)


def test_terms_update_page_is_accepted_when_the_config_allows_it(env):
    page, _, monkeypatch = env
    page.url = "https://account.samsung.com/iam/sign-in/status/terms"
    clicked = []

    def click(targets, selectors):
        clicked.append(selectors)
        return True

    monkeypatch.setattr(login, "click_first_visible", click)
    run(page, lambda: page.polls >= 2, accept_terms=True)
    assert login.REQUIRED_TERMS in clicked
    assert all("optional" not in sel.lower() for sel in login.REQUIRED_TERMS)


def test_terms_update_page_is_left_to_a_visible_browser(env):
    page, _, _ = env
    page.url = "https://account.samsung.com/iam/sign-in/status/terms"
    run(page, lambda: page.polls >= 3, headless=False)   # the person accepts it in the window
    assert page.polls == 3


def test_interactive_verification_is_handed_to_the_user(env):
    page, _, monkeypatch = env
    seen = {}
    monkeypatch.setattr(login, "needs_verification", lambda p: not seen)
    monkeypatch.setattr(login, "_verify", lambda p, s, m, pr: seen.setdefault("done", True))
    run(page, lambda: bool(seen), interactive=True)
    assert seen == {"done": True}


def test_signing_in_that_never_finishes_reports_the_page(env):
    page, clock, _ = env
    with pytest.raises(RotatorError, match="did not finish within 40s"):
        run(page, lambda: False)
    assert clock.now >= login.SIGN_IN_TIMEOUT_S


def test_navigation_errors_while_polling_are_ignored(env):
    page, _, _ = env
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise login.PlaywrightError("Execution context was destroyed")
        return True

    run(page, flaky)
    assert calls["n"] == 3


def test_missing_credentials_or_email_field(env):
    page, _, monkeypatch = env
    with pytest.raises(RotatorError, match="required"):
        login.sign_in(page, email="", password="", signed_in=lambda: False, interactive=False)
    monkeypatch.setattr(login, "first_visible", lambda targets, sels: None)
    with pytest.raises(RotatorError, match="email field"):
        run(page, lambda: False)
