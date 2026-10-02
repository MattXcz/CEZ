"""ČEZ integrace pro Home Assistant."""
from __future__ import annotations

import logging

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryError,
    ConfigEntryNotReady,
)

from .api import (
    CezApiError,
    CezDistribuceApiClient,
    CezInvalidCredentialsError,
    extract_supply_points,
)
from .const import (
    CONF_ANLAGE,
    CONF_EAN,
    CONF_OM_TYPE,
    CONF_PARTNER,
    CONF_PASSWORD,
    CONF_PORTAL_ENVIRONMENT,
    CONF_PORTAL_PARTNER,
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
            for point in extract_supply_points(await client.get_supply_points()):
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


async def _async_ensure_portal_environment(
    hass: HomeAssistant,
    entry: ConfigEntry,
    client: CezDistribuceApiClient,
    uid: str,
    ean: str,
) -> None:
    """Zajistí, že je vybrané prostředí portálu s odběrným místem entry (issue #37).

    Týká se jen účtů, které po přihlášení vybírají prostředí. Entries
    založené před touto verzí prostředí uložené nemají a výchozí volba
    (Domácnost) nemusí obsahovat jejich EAN; stejně tak když uložené
    prostředí z portálu zmizelo.
    """
    if not client.environment_options:
        return
    if entry.data.get(CONF_PORTAL_ENVIRONMENT) and not client.preferred_environment_missing:
        return

    _LOGGER.info("Dohledávám prostředí portálu ČEZ s odběrným místem této integrace.")
    try:
        environment = await client.find_environment_for_supply_point(uid, ean)
    except (CezApiError, aiohttp.ClientError, TimeoutError) as err:
        raise ConfigEntryNotReady(
            f"Nepodařilo se dohledat prostředí portálu ČEZ: {err}"
        ) from err
    if environment is None:
        raise ConfigEntryError(
            "Odběrné místo této integrace není vidět v žádném prostředí portálu "
            "ČEZ Distribuce. Překonfigurujte integraci, nebo ji přidejte znovu."
        )
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            CONF_PORTAL_ENVIRONMENT: environment[0],
            CONF_PORTAL_PARTNER: environment[1],
        },
    )
    _LOGGER.info("Prostředí portálu ČEZ uloženo do konfigurace (%s).", environment[0])


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
    portal_environment = (
        (entry.data[CONF_PORTAL_ENVIRONMENT], entry.data[CONF_PORTAL_PARTNER])
        if entry.data.get(CONF_PORTAL_ENVIRONMENT) and entry.data.get(CONF_PORTAL_PARTNER)
        else None
    )
    session = aiohttp.ClientSession()
    client = CezDistribuceApiClient(
        username=username,
        password=password,
        session=session,
        portal_environment=portal_environment,
    )

    try:
        await client.login()
    except CezInvalidCredentialsError as err:
        await session.close()
        raise ConfigEntryAuthFailed(str(err)) from err
    except Exception as err:
        # Síť, timeout, změna přihlašovacího toku nebo mezikrok portálu
        # (podmínky, smlouva) - HA to zkusí znovu s rostoucím odstupem.
        await session.close()
        raise ConfigEntryNotReady(f"Přihlášení do ČEZ selhalo: {err}") from err

    entry.async_on_unload(session.close)
    await _async_ensure_portal_environment(hass, entry, client, uid, ean)
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
