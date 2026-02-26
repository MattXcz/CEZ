"""ČEZ Distribuce integrace pro Home Assistant."""
from __future__ import annotations

import logging

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .api import CezDistribuceApiClient
from .const import CONF_EAN, CONF_PASSWORD, CONF_USERNAME, DOMAIN
from .coordinator import CezDistribuceCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Nastaví integraci z config entry."""
    username = entry.data[CONF_USERNAME]
    password = entry.data[CONF_PASSWORD]
    ean = entry.data[CONF_EAN]
    uid = entry.data.get("uid", "")

    session = aiohttp.ClientSession()
    client = CezDistribuceApiClient(username=username, password=password, session=session)

    try:
        await client.login()
    except Exception as err:
        await session.close()
        _LOGGER.error("Přihlášení do ČEZ selhalo: %s", err)
        return False

    coordinator = CezDistribuceCoordinator(hass, client, ean=ean, uid=uid)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    # Uložit session pro cleanup
    entry.async_on_unload(session.close)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Odstraní integraci."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok
