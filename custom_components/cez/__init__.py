"""ČEZ integrace pro Home Assistant."""
from __future__ import annotations

import logging

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .api import CezDistribuceApiClient
from .const import (
    CONF_ANLAGE,
    CONF_EAN,
    CONF_OM_TYPE,
    CONF_PARTNER,
    CONF_PASSWORD,
    CONF_USERNAME,
    DOMAIN,
    OM_TYPE_CONSUMPTION,
)
from .coordinator import CezDistribuceCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR]


async def _async_ensure_partner_and_anlage(
    hass: HomeAssistant,
    entry: ConfigEntry,
    client: CezDistribuceApiClient,
    uid: str,
) -> tuple[str, str]:
    """Zajistí, že config entry má uložené 'partner'/'anlage'.

    Instalace nastavené před touto verzí pluginu je nemají - dohledáme je
    (partner ze seznamu odběrných míst, anlage z detailu) a trvale
    uložíme, ať se nemusí dohledávat při každém startu ani ať uživatel
    nemusí integraci ručně znovu přidávat. Pokud dohledání selže (např.
    odběratel bez chytrého elektroměru), integrace se přesto nastaví -
    jen hodinová spotřeba nebude dostupná."""
    partner = entry.data.get(CONF_PARTNER, "")
    anlage = entry.data.get(CONF_ANLAGE, "")

    if partner and anlage:
        return partner, anlage

    try:
        if not partner:
            supply_points = await client.get_supply_points()
            blocks = (supply_points or {}).get("vstelleBlocks", {}).get("blocks", [])
            for block in blocks:
                for point in block.get("vstelles", []):
                    if point.get("uid") == uid:
                        partner = point.get("partner", "")
                        break

        if not anlage:
            detail = await client.get_supply_point_detail(uid)
            if isinstance(detail, dict):
                anlage = (detail.get("anlage_Dist") or {}).get("cislo") or detail.get("anlage", "")

        if partner and anlage:
            hass.config_entries.async_update_entry(
                entry,
                data={**entry.data, CONF_PARTNER: partner, CONF_ANLAGE: anlage},
            )
            _LOGGER.info("Doplnil jsem 'partner'/'anlage' do konfigurace (hodinová spotřeba).")
    except Exception:  # noqa: BLE001
        _LOGGER.exception(
            "Nepodařilo se dohledat 'partner'/'anlage' - hodinová spotřeba "
            "nebude dostupná, ostatní senzory fungují normálně."
        )

    return partner, anlage


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Nastaví integraci z config entry."""
    username = entry.data[CONF_USERNAME]
    password = entry.data[CONF_PASSWORD]
    ean = entry.data[CONF_EAN]
    uid = entry.data.get("uid", "")
    # Chybí u config entries založených před přidáním rozlišení OM typu -
    # bere se jako běžná spotřeba, aby se chování neměnilo (viz const.py).
    om_type = entry.data.get(CONF_OM_TYPE) or OM_TYPE_CONSUMPTION

    session = aiohttp.ClientSession()
    client = CezDistribuceApiClient(username=username, password=password, session=session)

    try:
        await client.login()
    except Exception as err:
        await session.close()
        _LOGGER.error("Přihlášení do ČEZ selhalo: %s", err)
        return False

    partner, anlage = await _async_ensure_partner_and_anlage(hass, entry, client, uid)

    coordinator = CezDistribuceCoordinator(
        hass, client, ean=ean, uid=uid, partner=partner, anlage=anlage, om_type=om_type
    )
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
