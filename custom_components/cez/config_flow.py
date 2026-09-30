"""Config flow pro ČEZ."""
from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import CezAuthError, CezDistribuceApiClient
from .const import (
    CONF_ANLAGE,
    CONF_EAN,
    CONF_HDO_SIGNAL,
    CONF_OM_TYPE,
    CONF_PARTNER,
    CONF_PASSWORD,
    CONF_PRICE_NT,
    CONF_PRICE_VT,
    CONF_USERNAME,
    DEFAULT_PRICE_NT,
    DEFAULT_PRICE_VT,
    DOMAIN,
    OM_TYPE_CONSUMPTION,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


async def _login_and_get_supply_points(username: str, password: str) -> list[dict]:
    """Přihlásí se a vrátí seznam odběrných míst."""
    _LOGGER.debug("Config flow: přihlašuji se pro načtení odběrných míst.")
    async with aiohttp.ClientSession() as session:
        client = CezDistribuceApiClient(username=username, password=password, session=session)
        await client.login()
        _LOGGER.debug("Config flow: načítám odběrná místa.")
        data = await client.get_supply_points()

    vstelles = []
    if data and isinstance(data, dict):
        blocks = data.get("vstelleBlocks", {}).get("blocks", [])
        for block in blocks:
            vstelles.extend(block.get("vstelles", []))
    _LOGGER.debug("Config flow: načteno odběrných míst=%d.", len(vstelles))
    return vstelles


async def _fetch_hdo_signals(username: str, password: str, ean: str) -> list[str]:
    """Vrátí seznam unikátních HDO signálů pro daný EAN (např. ['a3b7dp01', 'a3b7dp06'])."""
    _LOGGER.debug("Config flow: přihlašuji se pro načtení HDO signálů.")
    async with aiohttp.ClientSession() as session:
        client = CezDistribuceApiClient(username=username, password=password, session=session)
        await client.login()
        signals_data = await client.get_signals(ean)

    signal_list = (
        signals_data.get("signals", []) if isinstance(signals_data, dict) else []
    )
    seen: set[str] = set()
    unique: list[str] = []
    for entry in signal_list:
        code = entry.get("signal", "")
        if code and code not in seen:
            seen.add(code)
            unique.append(code)
    _LOGGER.debug("Config flow: načteno HDO signálů=%d.", len(unique))
    return unique


async def _fetch_anlage(username: str, password: str, uid: str) -> str:
    """Vrátí 'anlage' (SAP technické číslo) z detailu odběrného místa -
    potřebné pro hodinovou/15min spotřebu (/pnd/data). Volitelné - pokud
    selže, integrace se přidá i bez hodinové spotřeby (viz __init__.py,
    který se to pak pokusí dohledat znovu při dalším startu)."""
    _LOGGER.debug("Config flow: přihlašuji se pro načtení detailu odběrného místa.")
    async with aiohttp.ClientSession() as session:
        client = CezDistribuceApiClient(username=username, password=password, session=session)
        await client.login()
        detail = await client.get_supply_point_detail(uid)

    if isinstance(detail, dict):
        anlage = (detail.get("anlage_Dist") or {}).get("cislo") or detail.get("anlage", "")
    else:
        anlage = ""
    _LOGGER.debug("Config flow: technické číslo odběrného místa nalezeno=%s.", bool(anlage))
    return anlage


class CezDistribuceConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Průvodce nastavením integrace ČEZ."""

    VERSION = 1

    def __init__(self) -> None:
        self._username: str = ""
        self._password: str = ""
        self._supply_points: list[dict] = []
        self._selected_ean: str = ""
        self._selected_uid: str = ""
        self._selected_partner: str = ""
        self._selected_anlage: str = ""
        self._selected_title: str = ""
        self._selected_om_type: str = ""
        self._hdo_signals: list[str] = []
        self._client: CezDistribuceApiClient | None = None

    async def _async_get_client(self) -> CezDistribuceApiClient:
        """Přihlásí se nejvýše jednou a vrací klienta sdíleného pro celý flow."""
        if self._client is None:
            _LOGGER.debug("Config flow: vytvářím klienta a přihlašuji se k ČEZ.")
            client = CezDistribuceApiClient(
                username=self._username,
                password=self._password,
                session=async_get_clientsession(self.hass),
            )
            await client.login()
            self._client = client
        return self._client

    async def _async_fetch_anlage(self) -> None:
        """Volitelně načte technické číslo, aniž by blokovalo výběr HDO."""
        if self._selected_anlage:
            return

        try:
            client = await self._async_get_client()
            detail = await client.get_supply_point_detail(self._selected_uid)
            if isinstance(detail, dict):
                self._selected_anlage = (
                    (detail.get("anlage_Dist") or {}).get("cislo")
                    or detail.get("anlage", "")
                )
            _LOGGER.debug(
                "Config flow: technické číslo odběrného místa nalezeno=%s.",
                bool(self._selected_anlage),
            )
        except Exception:
            _LOGGER.exception(
                "Nepodařilo se dohledat technické číslo odběrného místa; "
                "hodinová spotřeba se zkusí nastavit později."
            )

    # ------------------------------------------------------------------
    # Krok 1 – přihlašovací údaje
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Zadání přihlašovacích údajů."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._username = user_input[CONF_USERNAME]
            self._password = user_input[CONF_PASSWORD]

            try:
                client = await self._async_get_client()
                _LOGGER.debug("Config flow: načítám odběrná místa.")
                data = await client.get_supply_points()
                blocks = (
                    data.get("vstelleBlocks", {}).get("blocks", [])
                    if isinstance(data, dict)
                    else []
                )
                self._supply_points = [
                    point
                    for block in blocks
                    for point in block.get("vstelles", [])
                ]
                _LOGGER.debug(
                    "Config flow: načteno odběrných míst=%d.",
                    len(self._supply_points),
                )
            except CezAuthError:
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("Neočekávaná chyba při přihlašování")
                errors["base"] = "cannot_connect"

            if not errors:
                if not self._supply_points:
                    errors["base"] = "no_supply_points"
                elif len(self._supply_points) == 1:
                    self._select_point(self._supply_points[0])
                    return await self.async_step_select_hdo_signal()
                else:
                    return await self.async_step_select_supply_point()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Krok 2 – výběr odběrného místa (pokud je jich více)
    # ------------------------------------------------------------------

    async def async_step_select_supply_point(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Výběr odběrného místa."""
        if user_input is not None:
            selected_ean = user_input[CONF_EAN]
            point = next(
                (p for p in self._supply_points if p.get("ean") == selected_ean), None
            )
            if point:
                self._select_point(point)
                return await self.async_step_select_hdo_signal()

        options = {
            p["ean"]: (
                p.get("adresa", {}).get("adresaComplete")
                or p["ean"]
            ) + f" ({p['ean']}, {p.get('typText') or p.get('typ') or '?'})"
            for p in self._supply_points
            if "ean" in p
        }

        return self.async_show_form(
            step_id="select_supply_point",
            data_schema=vol.Schema({vol.Required(CONF_EAN): vol.In(options)}),
        )

    # ------------------------------------------------------------------
    # Krok 3 – výběr HDO signálu
    # ------------------------------------------------------------------

    async def async_step_select_hdo_signal(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Výběr kódu HDO signálu (např. a3b7dp01)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            await self._async_fetch_anlage()
            return await self._async_create_entry(
                user_input[CONF_HDO_SIGNAL],
                user_input[CONF_PRICE_VT],
                user_input[CONF_PRICE_NT],
            )

        # HDO/tarify se týkají jen běžné spotřeby ("S") - odběrné místo
        # výroby/mikrozdroje (FVE, typ "V"/"M") žádný spínací signál nemá,
        # zbytečně by se jen logovala chyba/prázdný výsledek. Rovnou
        # přeskočíme na vytvoření entry bez HDO signálu (viz issue #24 -
        # dodávka do sítě).
        if self._selected_om_type and self._selected_om_type != OM_TYPE_CONSUMPTION:
            self._hdo_signals = []
        elif not self._hdo_signals:
            try:
                client = await self._async_get_client()
                signals_data = await client.get_signals(self._selected_ean)
                signal_list = (
                    signals_data.get("signals", [])
                    if isinstance(signals_data, dict)
                    else []
                )
                seen: set[str] = set()
                for item in signal_list:
                    code = item.get("signal", "")
                    if code and code not in seen:
                        seen.add(code)
                        self._hdo_signals.append(code)
                _LOGGER.debug(
                    "Config flow: načteno HDO signálů=%d.",
                    len(self._hdo_signals),
                )
            except Exception:
                _LOGGER.exception("Nepodařilo se načíst HDO signály.")

        # Pokud nejsou žádné signály, přeskočíme krok
        if not self._hdo_signals:
            await self._async_fetch_anlage()
            return await self._async_create_entry("", DEFAULT_PRICE_VT, DEFAULT_PRICE_NT)

        options = {s: s for s in self._hdo_signals}

        _LOGGER.debug(
            "Config flow: zobrazuji výběr HDO signálu, možností=%d.",
            len(options),
        )
        return self.async_show_form(
            step_id="select_hdo_signal",
            data_schema=vol.Schema({
                vol.Required(CONF_HDO_SIGNAL, default=self._hdo_signals[0]): vol.In(options),
                vol.Required(CONF_PRICE_VT, default=DEFAULT_PRICE_VT): vol.Coerce(float),
                vol.Required(CONF_PRICE_NT, default=DEFAULT_PRICE_NT): vol.Coerce(float),
            }),
            description_placeholders={"signal_count": str(len(self._hdo_signals))},
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Pomocné metody
    # ------------------------------------------------------------------

    def _select_point(self, point: dict) -> None:
        """Uloží vybrané odběrné místo."""
        self._selected_ean = point.get("ean", "")
        self._selected_uid = point.get("uid", "")
        self._selected_partner = point.get("partner", "")
        self._selected_om_type = point.get("typ") or OM_TYPE_CONSUMPTION
        adresa = point.get("adresa") or {}
        base_title = (
            adresa.get("adresaComplete")
            if isinstance(adresa, dict)
            else None
        ) or f"ČEZ {self._selected_ean}"
        type_text = point.get("typText")
        self._selected_title = (
            f"{base_title} ({type_text})"
            if type_text and type_text != "Spotřeba"
            else base_title
        ).strip() or f"ČEZ {self._selected_ean}".strip() or "ČEZ"

    async def _async_create_entry(
        self,
        hdo_signal: str,
        price_vt: float,
        price_nt: float,
    ) -> FlowResult:
        """Vytvoří config entry."""
        await self.async_set_unique_id(self._selected_ean)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=self._selected_title or f"ČEZ {self._selected_ean}".strip() or "ČEZ",
            data={
                CONF_USERNAME: self._username,
                CONF_PASSWORD: self._password,
                CONF_EAN: self._selected_ean,
                "uid": self._selected_uid,
                CONF_PARTNER: self._selected_partner,
                CONF_ANLAGE: self._selected_anlage,
                CONF_OM_TYPE: self._selected_om_type or OM_TYPE_CONSUMPTION,
                CONF_HDO_SIGNAL: hdo_signal,
                CONF_PRICE_VT: price_vt,
                CONF_PRICE_NT: price_nt,
            },
        )
