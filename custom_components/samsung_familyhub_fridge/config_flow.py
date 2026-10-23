"""Config flow for Samsung FamilyHub Fridge integration."""
from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import AuthenticationError, FamilyHub, RateLimitError
from .auth import (
    DEFAULT_REDIRECT_URI,
    SAMSUNG_LOGIN_CLIENT_ID,
    AuthError,
    OAuthCredentials,
    SamsungAccountAuth,
    SmartThingsOAuth,
)
from .const import (
    AUTH_MODE_PAT,
    AUTH_MODE_STANDALONE_OAUTH,
    CONF_AUTH_MODE,
    CONF_DEVICE_ID,
    CONF_ENABLE_BOTTOM_CAMERA,
    CONF_FOOD_UPDATE_INTERVAL,
    CONF_LOCATION_ID,
    CONF_OAUTH_CLIENT_ID,
    CONF_OAUTH_CLIENT_SECRET,
    CONF_OAUTH_REFRESH_TOKEN,
    CONF_POLL_INTERVAL,
    CONF_ROTATOR_API_KEY,
    CONF_ROTATOR_URL,
    CONF_SAMSUNG_IOT_AUTH_SERVER,
    CONF_SAMSUNG_IOT_REFRESH_TOKEN,
    CONF_SAMSUNG_SIGNIN_CLIENT_SECRET,
    CONF_TOKEN,
    DEFAULT_ENABLE_BOTTOM_CAMERA,
    DEFAULT_FOOD_UPDATE_INTERVAL,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_ROTATOR_URL,
    DOMAIN,
    MAX_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
)
from .rotator import RotatorClient, entry_value

_LOGGER = logging.getLogger(__name__)

STEP_PAT_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_TOKEN): str,
        vol.Optional(CONF_DEVICE_ID): str,
    }
)
CREDENTIALS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_OAUTH_CLIENT_ID): str,
        vol.Required(CONF_OAUTH_CLIENT_SECRET): str,
    }
)
CODE_SCHEMA = vol.Schema({"redirect_url_or_code": str})
FIELD_REDIRECT = "redirect_url_or_code"


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""


async def _validate_pat(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate a Personal Access Token and resolve the device ID."""
    hub = FamilyHub(async_get_clientsession(hass), data[CONF_TOKEN], data.get(CONF_DEVICE_ID))
    try:
        status = await hub.get_all_device_status()
    except AuthenticationError as err:
        raise InvalidAuth from err
    except (RateLimitError, aiohttp.ClientError, TimeoutError, ValueError) as err:
        raise CannotConnect from err
    hub.set_device_status(status)
    if not data.get(CONF_DEVICE_ID):
        data[CONF_DEVICE_ID] = hub.device_id
    return data


def _redirect_uri(hass: HomeAssistant) -> str:
    """Home Assistant's OAuth redirect URI, or the public my.home-assistant.io one."""
    try:
        return config_entry_oauth2_flow.async_get_redirect_uri(hass)
    except HomeAssistantError:
        return DEFAULT_REDIRECT_URI


def _credentials_input(user_input: dict[str, Any]) -> tuple[str, str, dict[str, str]]:
    """(client_id, client_secret, errors) from the credentials form."""
    client_id = user_input.get(CONF_OAUTH_CLIENT_ID, "").strip()
    client_secret = user_input.get(CONF_OAUTH_CLIENT_SECRET, "").strip()
    errors: dict[str, str] = {}
    if not client_id:
        errors[CONF_OAUTH_CLIENT_ID] = "required"
    elif not client_secret:
        errors[CONF_OAUTH_CLIENT_SECRET] = "required"
    return client_id, client_secret, errors


async def _exchange_code(
    hass: HomeAssistant, oauth: SmartThingsOAuth, raw: str
) -> tuple[OAuthCredentials | None, str | None]:
    """Exchange a pasted redirect URL or code for tokens: (credentials, error key)."""
    try:
        code = SmartThingsOAuth.extract_code_from_redirect(raw) if raw.startswith("http") else raw
        credentials = await oauth.exchange_code(async_get_clientsession(hass), code)
    except ValueError:
        return None, "invalid_redirect_url"
    except (aiohttp.ClientError, TimeoutError, KeyError) as err:
        _LOGGER.warning("SmartThings code exchange failed: %s", type(err).__name__)
        return None, "code_exchange_failed"
    return credentials, None


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):  # type: ignore[call-arg]
    """Handle a config flow for Samsung FamilyHub Fridge."""

    VERSION = 2

    def __init__(self) -> None:
        self._standalone_oauth: SmartThingsOAuth | None = None
        self._standalone_auth_url = ""
        self._standalone_client_id = ""
        self._standalone_client_secret = ""
        self._standalone_refresh_token = ""
        self._reauth_standalone_oauth: SmartThingsOAuth | None = None
        self._reauth_standalone_auth_url = ""
        self._reauth_standalone_client_id = ""
        self._reauth_standalone_client_secret = ""

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> config_entries.OptionsFlow:
        """Create the options flow handler."""
        return SamsungFamilyHubOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """First step: choose Standalone OAuth or a Personal Access Token."""
        return self.async_show_menu(step_id="user", menu_options=["standalone_oauth", "pat"])

    # ---------------- Standalone OAuth path ----------------

    async def async_step_standalone_oauth(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Menu option 'standalone_oauth' resolves to this step."""
        return await self.async_step_standalone_oauth_credentials(user_input)

    async def async_step_standalone_oauth_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1 of standalone OAuth: collect client_id and client_secret."""
        errors: dict[str, str] = {}
        if user_input is not None:
            client_id, client_secret, errors = _credentials_input(user_input)
            if not errors:
                self._standalone_oauth = SmartThingsOAuth(
                    client_id=client_id,
                    client_secret=client_secret,
                    redirect_uri=_redirect_uri(self.hass),
                )
                self._standalone_client_id = client_id
                self._standalone_client_secret = client_secret
                self._standalone_auth_url = self._standalone_oauth.get_authorization_url()
                return await self.async_step_standalone_oauth_link()

        return self.async_show_form(
            step_id="standalone_oauth_credentials", data_schema=CREDENTIALS_SCHEMA, errors=errors
        )

    async def async_step_standalone_oauth_link(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2 of standalone OAuth: show the authorization URL, collect the redirect or code."""
        oauth = self._standalone_oauth
        if oauth is None:
            return await self.async_step_standalone_oauth_credentials()
        auth_url = self._standalone_auth_url or oauth.get_authorization_url()

        errors: dict[str, str] = {}
        if user_input is not None:
            raw = user_input.get(FIELD_REDIRECT, "").strip()
            if not raw:
                errors[FIELD_REDIRECT] = "required"
            else:
                credentials, error = await _exchange_code(self.hass, oauth, raw)
                if credentials is not None:
                    self._standalone_refresh_token = credentials.refresh_token
                    return await self.async_step_standalone_oauth_samsung()
                errors[FIELD_REDIRECT] = error or "code_exchange_failed"

        return self.async_show_form(
            step_id="standalone_oauth_link",
            data_schema=CODE_SCHEMA,
            errors=errors,
            description_placeholders={"authorization_url": auth_url},
        )

    async def async_step_standalone_oauth_samsung(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 3 of standalone OAuth: optional Samsung Account login for image downloads."""
        errors: dict[str, str] = {}
        if user_input is not None:
            email = (user_input.get("samsung_email") or "").strip()
            password = (user_input.get("samsung_password") or "").strip()
            signin_client_secret = (user_input.get(CONF_SAMSUNG_SIGNIN_CLIENT_SECRET) or "").strip()
            data: dict[str, Any] = {
                CONF_AUTH_MODE: AUTH_MODE_STANDALONE_OAUTH,
                CONF_OAUTH_CLIENT_ID: self._standalone_client_id,
                CONF_OAUTH_CLIENT_SECRET: self._standalone_client_secret,
                CONF_OAUTH_REFRESH_TOKEN: self._standalone_refresh_token,
                CONF_SAMSUNG_SIGNIN_CLIENT_SECRET: signin_client_secret,
            }
            if email and password:
                samsung_auth = SamsungAccountAuth(
                    email=email,
                    password=password,
                    signin_client_id=SAMSUNG_LOGIN_CLIENT_ID,
                    signin_client_secret=signin_client_secret,
                )
                try:
                    iot_credentials = await samsung_auth.login_iot(async_get_clientsession(self.hass))
                except AuthError as err:
                    _LOGGER.warning("Samsung Account login failed: %s", err)
                    errors["base"] = "samsung_login_failed"
                except (aiohttp.ClientError, TimeoutError, KeyError, ValueError) as err:
                    _LOGGER.warning("Samsung Account login could not complete: %s", type(err).__name__)
                    errors["base"] = "cannot_connect"
                else:
                    data[CONF_SAMSUNG_IOT_REFRESH_TOKEN] = iot_credentials.refresh_token
                    data[CONF_SAMSUNG_IOT_AUTH_SERVER] = iot_credentials.auth_server_url
            else:
                _LOGGER.warning("Samsung Account credentials not provided; IoT features unavailable")

            if not errors:
                return self.async_create_entry(title="Samsung Fridge Camera (Standalone OAuth)", data=data)

        schema = vol.Schema(
            {
                vol.Optional("samsung_email"): str,
                vol.Optional("samsung_password"): str,
                vol.Optional(CONF_SAMSUNG_SIGNIN_CLIENT_SECRET): str,
            }
        )
        return self.async_show_form(
            step_id="standalone_oauth_samsung",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "samsung_signin_client_secret_hint": (
                    "Obtain from SmartThings APK decompilation or open-source mirrors "
                    "(search for 'signin_client_secret' in the SmartThings app source). "
                    "Leave empty to skip Samsung IoT token acquisition."
                )
            },
        )

    # ---------------- PAT path ----------------

    async def async_step_pat(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Enter a SmartThings Personal Access Token (valid 24 hours; the rotator renews it)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                info = await _validate_pat(self.hass, dict(user_input))
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except Exception:  # noqa: BLE001 - last-resort guard in a config flow
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                if info.get(CONF_DEVICE_ID):
                    await self.async_set_unique_id(info[CONF_DEVICE_ID])
                    self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="Samsung Fridge Camera", data={**info, CONF_AUTH_MODE: AUTH_MODE_PAT}
                )

        return self.async_show_form(step_id="pat", data_schema=STEP_PAT_DATA_SCHEMA, errors=errors)

    # ---------------- Reauth ----------------

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Re-authenticate: Standalone OAuth entries re-run the OAuth steps, others enter a new PAT.

        (Entries from the removed "borrowed OAuth" mode land on the PAT step and become PAT entries.)
        """
        if entry_data.get(CONF_AUTH_MODE) == AUTH_MODE_STANDALONE_OAUTH:
            return await self.async_step_reauth_standalone_oauth()
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle PAT re-authentication: try the rotator's token first, then ask for one."""
        errors: dict[str, str] = {}
        if user_input is None:
            reauth_entry = self._get_reauth_entry()
            rotator = RotatorClient.from_entry(async_get_clientsession(self.hass), reauth_entry)
            offered = await rotator.fetch_token("/pat")
            if offered is not None and offered.token != reauth_entry.data.get(CONF_TOKEN):
                new_data = {**reauth_entry.data, CONF_TOKEN: offered.token, CONF_AUTH_MODE: AUTH_MODE_PAT}
                try:
                    await _validate_pat(self.hass, new_data)
                except (CannotConnect, InvalidAuth):
                    pass  # the rotator's token does not work either; ask the user
                else:
                    return self.async_update_reload_and_abort(reauth_entry, data=new_data)
        if user_input is not None:
            reauth_entry = self._get_reauth_entry()
            new_data = {**reauth_entry.data, CONF_TOKEN: user_input[CONF_TOKEN], CONF_AUTH_MODE: AUTH_MODE_PAT}
            try:
                await _validate_pat(self.hass, new_data)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except Exception:  # noqa: BLE001 - last-resort guard in a config flow
                _LOGGER.exception("Unexpected exception during re-auth")
                errors["base"] = "unknown"
            else:
                return self.async_update_reload_and_abort(reauth_entry, data=new_data)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_TOKEN): str}),
            errors=errors,
        )

    async def async_step_reauth_standalone_oauth(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1 of standalone OAuth re-auth: collect client credentials."""
        errors: dict[str, str] = {}
        if user_input is not None:
            client_id, client_secret, errors = _credentials_input(user_input)
            if not errors:
                self._reauth_standalone_oauth = SmartThingsOAuth(
                    client_id=client_id,
                    client_secret=client_secret,
                    redirect_uri=_redirect_uri(self.hass),
                )
                self._reauth_standalone_client_id = client_id
                self._reauth_standalone_client_secret = client_secret
                self._reauth_standalone_auth_url = self._reauth_standalone_oauth.get_authorization_url()
                return await self.async_step_reauth_standalone_oauth_link()

        return self.async_show_form(
            step_id="reauth_standalone_oauth", data_schema=CREDENTIALS_SCHEMA, errors=errors
        )

    async def async_step_reauth_standalone_oauth_link(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2 of standalone OAuth re-auth: show the authorization URL, collect the code."""
        oauth = self._reauth_standalone_oauth
        if oauth is None:
            return await self.async_step_reauth_standalone_oauth()
        auth_url = self._reauth_standalone_auth_url or oauth.get_authorization_url()

        errors: dict[str, str] = {}
        if user_input is not None:
            raw = user_input.get(FIELD_REDIRECT, "").strip()
            if not raw:
                errors[FIELD_REDIRECT] = "required"
            else:
                credentials, error = await _exchange_code(self.hass, oauth, raw)
                if credentials is not None:
                    reauth_entry = self._get_reauth_entry()
                    return self.async_update_reload_and_abort(
                        reauth_entry,
                        data={
                            **reauth_entry.data,
                            CONF_OAUTH_CLIENT_ID: self._reauth_standalone_client_id,
                            CONF_OAUTH_CLIENT_SECRET: self._reauth_standalone_client_secret,
                            CONF_OAUTH_REFRESH_TOKEN: credentials.refresh_token,
                        },
                    )
                errors[FIELD_REDIRECT] = error or "code_exchange_failed"

        return self.async_show_form(
            step_id="reauth_standalone_oauth_link",
            data_schema=CODE_SCHEMA,
            errors=errors,
            description_placeholders={"authorization_url": auth_url},
        )


class SamsungFamilyHubOptionsFlow(config_entries.OptionsFlow):
    """Options: rotator, location, polling and optional cameras."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        entry = self.config_entry
        enable_bottom = bool(
            entry.options.get(CONF_ENABLE_BOTTOM_CAMERA)
            if CONF_ENABLE_BOTTOM_CAMERA in entry.options
            else entry.data.get(CONF_ENABLE_BOTTOM_CAMERA, DEFAULT_ENABLE_BOTTOM_CAMERA)
        )
        schema = vol.Schema(
            {
                vol.Optional(CONF_ROTATOR_URL, default=entry_value(entry, CONF_ROTATOR_URL, DEFAULT_ROTATOR_URL)): str,
                vol.Optional(CONF_ROTATOR_API_KEY, default=entry_value(entry, CONF_ROTATOR_API_KEY, "")): str,
                vol.Optional(CONF_LOCATION_ID, default=entry_value(entry, CONF_LOCATION_ID, "")): str,
                vol.Optional(
                    CONF_FOOD_UPDATE_INTERVAL,
                    default=int(entry_value(entry, CONF_FOOD_UPDATE_INTERVAL, DEFAULT_FOOD_UPDATE_INTERVAL)),
                ): int,
                vol.Optional(
                    CONF_POLL_INTERVAL,
                    default=int(entry_value(entry, CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)),
                ): vol.All(vol.Coerce(int), vol.Range(min=MIN_POLL_INTERVAL, max=MAX_POLL_INTERVAL)),
                vol.Optional(CONF_ENABLE_BOTTOM_CAMERA, default=enable_bottom): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
