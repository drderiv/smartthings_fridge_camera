"""Exceptions shared across the rotator."""


class RotatorError(RuntimeError):
    """Base class for rotator failures."""


class ConfigError(RotatorError):
    """config.json is missing or invalid."""


class InteractiveAuthRequired(RotatorError):
    """Samsung wants a human (2FA, CAPTCHA); automated retries must stop."""


class NotReady(RotatorError):
    """Nothing to do yet (e.g. no refresh token and no saved session)."""
