"""Coordinator that polls the fridge's SmartThings status and downloads camera images."""
from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import requests
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import AuthenticationError, FamilyHub, RateLimitError
from .const import CONF_TOKEN, DEFAULT_POLL_INTERVAL, DOMAIN
from .rotator import RotatorClient

if TYPE_CHECKING:
    from .food import SamsungFoodCoordinator

_LOGGER = logging.getLogger(__name__)

PAT_FALLBACK_LIFETIME = 22 * 3600   # assumed PAT lifetime if the rotator gives no schedule
PAT_RETRY_DELAY = 300               # re-check the rotator this soon after a failed fetch
DOOR_CLOSE_FOOD_DELAY = 12          # let the fridge's AI vision finish before re-reading inventory
DOWNLOAD_WARNINGS = 3               # warn about failed image downloads this many times, then once more


class FridgeCoordinator(DataUpdateCoordinator[None]):
    """State lives on ``hub`` (door state, images); this coordinator drives the polling."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: Any,
        hub: FamilyHub,
        *,
        rotator: RotatorClient | None = None,
        poll_interval: int = DEFAULT_POLL_INTERVAL,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name="Samsung Family Hub",
            update_interval=timedelta(seconds=poll_interval),
        )
        self.hub = hub
        self.food_coordinator: SamsungFoodCoordinator | None = None
        self.last_file_ids: list[str] = []
        self.last_updated_at: float | None = None
        self._rotator = rotator          # only set in PAT mode; the rotator supplies the PAT
        self._pat_expires_at = 0.0
        self._download_failures = 0
        self._rate_limited = False

    # --- PAT from the rotator ---------------------------------------------------

    def set_pat_expiry(self, expires_at: float | None) -> None:
        """Skip the rotator until ``expires_at`` (from the startup /tokens document)."""
        if expires_at:
            self._pat_expires_at = float(expires_at)

    async def _async_sync_pat(self, *, force: bool = False) -> bool:
        """Pick up a rotated PAT from the rotator. True if the token changed."""
        if self._rotator is None:
            return False
        now = time.time()
        if not force and self.hub.token and self._pat_expires_at and now < self._pat_expires_at:
            return False
        found = await self.hass.async_add_executor_job(self._rotator.fetch_token, "/pat")
        if found is None:
            self._pat_expires_at = now + PAT_RETRY_DELAY
            return False
        self._pat_expires_at = found.next_rotation_at or now + PAT_FALLBACK_LIFETIME
        if found.token == self.hub.token:
            return False
        _LOGGER.info("Picked up a fresh SmartThings PAT from the rotator")
        self.hub.update_token(found.token)
        self._persist_pat(found.token)
        return True

    def _persist_pat(self, token: str) -> None:
        """Store the PAT in the config entry so a restart can use it before the rotator answers."""
        entry = self.config_entry
        if entry.data.get(CONF_TOKEN) != token:
            self.hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_TOKEN: token})

    # --- polling ----------------------------------------------------------------

    async def _async_update_data(self) -> None:
        await self._async_sync_pat()
        try:
            await self.hub.async_ensure_fresh_token()
            await self._async_poll()
        except AuthenticationError as err:
            await self._async_recover_auth(err)
        except RateLimitError:
            if not self._rate_limited:
                self._rate_limited = True
                _LOGGER.warning(
                    "SmartThings is rate limiting requests (HTTP 429); skipping polls until it "
                    "clears. A longer poll interval (Configure) or fewer clients on this token helps."
                )
            return
        except (requests.RequestException, ValueError, KeyError) as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
                translation_placeholders={"error": str(err)},
            ) from err
        if self._rate_limited:
            self._rate_limited = False
            _LOGGER.info("SmartThings is no longer rate limiting requests")

    async def _async_recover_auth(self, err: AuthenticationError) -> None:
        """After a 401/403: retry once with a newer PAT from the rotator, else start reauth."""
        if await self._async_sync_pat(force=True):
            try:
                await self._async_poll()
                return
            except AuthenticationError:
                pass
        raise ConfigEntryAuthFailed(translation_domain=DOMAIN, translation_key="auth_failed") from err

    async def _async_poll(self) -> None:
        hub = self.hub
        run = self.hass.async_add_executor_job

        if hub.device_id is None:
            _LOGGER.debug("No device_id; fetching the device list")
            hub.set_device_status(await run(hub.get_all_device_status))
            if hub.device_id is None:
                return
        hub.set_current_device_status(await run(hub.get_current_device_status))
        hub.extract_device_data()
        file_ids = hub.get_file_ids()
        _LOGGER.debug(
            "polled status: last_closed=%s should_update=%s file_ids=%s",
            hub.last_closed, hub.should_update, file_ids,
        )

        if file_ids and set(file_ids) != set(self.last_file_ids):
            await self._async_download(file_ids)
        if hub.should_update:
            _LOGGER.debug("Door closed; asking the fridge to refresh its camera images")
            await run(hub.update_camera)
            hub.should_update = False

    async def _async_download(self, file_ids: list[str]) -> None:
        _LOGGER.debug("file IDs changed: %s -> %s; downloading images", self.last_file_ids, file_ids)
        if await self.hass.async_add_executor_job(self.hub.download_images):
            self.last_updated_at = time.time()
            self.last_file_ids = file_ids
            self._download_failures = 0
            if self.food_coordinator is not None:  # a new camera snapshot means inventory may have changed
                self.food_coordinator.async_trigger_door_close_sync(DOOR_CLOSE_FOOD_DELAY)
            return
        self._download_failures += 1
        if self._download_failures <= DOWNLOAD_WARNINGS:
            _LOGGER.warning(
                "Camera image download failed (attempt %d); will retry on the next poll",
                self._download_failures,
            )
        elif self._download_failures == DOWNLOAD_WARNINGS + 1:
            _LOGGER.warning("Camera image download keeps failing; suppressing further warnings until it recovers")
