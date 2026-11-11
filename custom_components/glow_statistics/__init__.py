"""Glow Statistics: Hildebrand Glow (DCC) half-hourly readings as long-term statistics."""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import GlowAuthError, GlowClient, GlowError
from .const import ATTR_START_DATE, DOMAIN, SERVICE_REIMPORT
from .coordinator import GlowConfigEntry, GlowCoordinator

_LOGGER = logging.getLogger(__name__)
PLATFORMS: list[Platform] = [Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

REIMPORT_SCHEMA = vol.Schema({vol.Required(ATTR_START_DATE): cv.date})


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the re-import service."""

    async def _reimport(call: ServiceCall) -> None:
        entries = [e for e in hass.config_entries.async_entries(DOMAIN) if e.state is ConfigEntryState.LOADED]
        if not entries:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="not_loaded")
        for entry in entries:
            try:
                await entry.runtime_data.async_reimport(call.data[ATTR_START_DATE])
            except ConfigEntryAuthFailed as err:
                raise HomeAssistantError(str(err)) from err
            except GlowError as err:
                raise HomeAssistantError(f"Glowmarkt API error: {err}") from err

    hass.services.async_register(DOMAIN, SERVICE_REIMPORT, _reimport, schema=REIMPORT_SCHEMA)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: GlowConfigEntry) -> bool:
    """Log in, find the meters and start the hourly import."""
    client = GlowClient(async_get_clientsession(hass), entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD])
    try:
        await client.authenticate()
        resources = await client.get_resources()
    except GlowAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except GlowError as err:
        raise ConfigEntryNotReady(str(err)) from err

    coordinator = GlowCoordinator(hass, entry, client, resources)
    if not coordinator.streams:
        _LOGGER.warning("No electricity or gas consumption resources found in this Glow account")
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # The first run can backfill years of history (one request per 10 days), so it runs
    # in the background instead of holding up Home Assistant's start-up.
    entry.async_create_background_task(hass, coordinator.async_refresh(), f"{DOMAIN}_first_refresh")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: GlowConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
