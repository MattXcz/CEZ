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


async def _async_enrich_partner_and_anlage(
    hass: HomeAssistant,
    entry: ConfigEntry,
    client: CezDistribuceApiClient,
    coordinator: CezDistribuceCoordinator,
    uid: str,
) -> None:
    """Na pozadí doplní metadata pro volitelná hodinová data."""
    partner = entry.data.get(CONF_PARTNER, "")
    anlage = entry.data.get(CONF_ANLAGE, "")

    if partner and anlage:
        return

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
            _LOGGER.debug("Dohledávám technické číslo odběrného místa na pozadí.")
            detail = await client.get_supply_point_detail(uid)
            if isinstance(detail, dict):
                anlage = (
                    (detail.get("anlage_Dist") or {}).get("cislo")
                    or detail.get("anlage", "")
                )

        updated_data = dict(entry.data)
        if partner:
            updated_data[CONF_PARTNER] = partner
        if anlage:
            updated_data[CONF_ANLAGE] = anlage
        if updated_data != entry.data:
            hass.config_entries.async_update_entry(entry, data=updated_data)
        if partner and anlage:
            coordinator.set_partner_and_anlage(partner, anlage)
            _LOGGER.info(
                "Doplnil jsem metadata pro hodinová data; aktivuji jejich načítání."
            )
            await coordinator.async_refresh()
        else:
            _LOGGER.debug(
                "Metadata pro hodinová data zatím nejsou kompletní "
                "(partner=%s, anlage=%s).",
                bool(partner),
                bool(anlage),
            )
    except Exception:  # noqa: BLE001
        _LOGGER.exception(
            "Nepodařilo se doplnit metadata pro hodinová data; "
            "ostatní senzory zůstávají funkční."
        )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Nastaví integraci z config entry."""
    username = entry.data[CONF_USERNAME]
    password = entry.data[CONF_PASSWORD]
    ean = entry.data[CONF_EAN]
    uid = entry.data.get("uid", "")
    if not (entry.title or "").strip():
        hass.config_entries.async_update_entry(
            entry, title=f"ČEZ {ean}".strip() or "ČEZ"
        )
        _LOGGER.warning(
            "Config entry neměl název; nastavil jsem bezpečný náhradní název."
        )
    # Chybí u config entries založených před přidáním rozlišení OM typu -
    # bere se jako běžná spotřeba, aby se chování neměnilo (viz const.py).
    om_type = entry.data.get(CONF_OM_TYPE) or OM_TYPE_CONSUMPTION

    _LOGGER.debug(
        "Nastavuji ČEZ config entry (EAN uložen=%s, UID uložen=%s).",
        bool(ean),
        bool(uid),
    )
    session = aiohttp.ClientSession()
    client = CezDistribuceApiClient(username=username, password=password, session=session)

    try:
        await client.login()
    except Exception as err:
        await session.close()
        _LOGGER.error("Přihlášení do ČEZ selhalo: %s", err)
        return False

    entry.async_on_unload(session.close)
    _LOGGER.debug("Přihlášení config entry dokončeno; pokračuji načtením dat.")
    partner = entry.data.get(CONF_PARTNER, "")
    anlage = entry.data.get(CONF_ANLAGE, "")

    coordinator = CezDistribuceCoordinator(
        hass, client, ean=ean, uid=uid, partner=partner, anlage=anlage, om_type=om_type
    )
    await coordinator.async_config_entry_first_refresh()
    _LOGGER.debug("První načtení dat config entry dokončeno.")

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _LOGGER.debug("Platformy ČEZ byly načteny.")
    if not partner or not anlage:
        task = hass.async_create_task(
            _async_enrich_partner_and_anlage(hass, entry, client, coordinator, uid),
            name=f"{DOMAIN} enrich supply point metadata",
        )
        entry.async_on_unload(task.cancel)
        _LOGGER.debug("Dohledání metadat hodinových dat běží na pozadí.")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Odstraní integraci."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok
