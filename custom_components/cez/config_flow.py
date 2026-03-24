"""Config flow pro ČEZ."""
from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult

from .api import CezAuthError, CezDistribuceApiClient
from .const import (
    CONF_BROWSER_AUTH,
    CONF_EAN,
    CONF_HDO_SIGNAL,
    CONF_PASSWORD,
    CONF_PRICE_NT,
    CONF_PRICE_VT,
    CONF_SERVICE_TICKET,
    CONF_USERNAME,
    DEFAULT_PRICE_NT,
    DEFAULT_PRICE_VT,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Optional(CONF_PASSWORD): str,
        vol.Optional(CONF_BROWSER_AUTH): str,
    }
)


async def _login_and_get_supply_points(
    username: str,
    password: str,
    browser_auth: str,
) -> list[dict]:
    """Přihlásí se a vrátí seznam odběrných míst."""
    async with aiohttp.ClientSession() as session:
        client = CezDistribuceApiClient(
            username=username,
            password=password,
            browser_auth=browser_auth or None,
            session=session,
        )
        await client.login()
        data = await client.get_supply_points()

    vstelles = []
    if data and isinstance(data, dict):
        blocks = data.get("vstelleBlocks", {}).get("blocks", [])
        for block in blocks:
            vstelles.extend(block.get("vstelles", []))
    return vstelles


async def _fetch_hdo_signals(
    username: str,
    password: str,
    browser_auth: str,
    ean: str,
) -> list[str]:
    """Vrátí seznam unikátních HDO signálů pro daný EAN (např. ['a3b7dp01', 'a3b7dp06'])."""
    async with aiohttp.ClientSession() as session:
        client = CezDistribuceApiClient(
            username=username,
            password=password,
            browser_auth=browser_auth or None,
            session=session,
        )
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
    return unique


class CezDistribuceConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Průvodce nastavením integrace ČEZ."""

    VERSION = 1

    def __init__(self) -> None:
        self._username: str = ""
        self._password: str = ""
        self._browser_auth: str = ""
        self._supply_points: list[dict] = []
        self._selected_ean: str = ""
        self._selected_uid: str = ""
        self._selected_title: str = ""
        self._hdo_signals: list[str] = []

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
            self._password = user_input.get(CONF_PASSWORD, "")
            self._browser_auth = user_input.get(CONF_BROWSER_AUTH, "")

            if not self._password and not self._browser_auth:
                errors["base"] = "missing_auth_data"
            elif self._password and self._browser_auth:
                errors["base"] = "too_many_auth_methods"

            if not errors:
                try:
                    self._supply_points = await _login_and_get_supply_points(
                        self._username,
                        self._password,
                        self._browser_auth,
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
            ) + f" ({p['ean']})"
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
            return await self._async_create_entry(
                user_input[CONF_HDO_SIGNAL],
                user_input[CONF_PRICE_VT],
                user_input[CONF_PRICE_NT],
            )

        # Načíst dostupné signály
        if not self._hdo_signals:
            try:
                self._hdo_signals = await _fetch_hdo_signals(
                    self._username,
                    self._password,
                    self._browser_auth,
                    self._selected_ean,
                )
            except Exception:
                _LOGGER.exception("Nepodařilo se načíst HDO signály pro EAN %s", self._selected_ean)

        # Pokud nejsou žádné signály, přeskočíme krok
        if not self._hdo_signals:
            return await self._async_create_entry("", DEFAULT_PRICE_VT, DEFAULT_PRICE_NT)

        options = {s: s for s in self._hdo_signals}

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
        adresa = point.get("adresa", {})
        self._selected_title = (
            adresa.get("adresaComplete")
            or f"ČEZ {self._selected_ean}"
        )

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
            title=self._selected_title,
            data={
                CONF_USERNAME: self._username,
                CONF_PASSWORD: self._password,
                CONF_BROWSER_AUTH: self._browser_auth,
                CONF_SERVICE_TICKET: self._browser_auth,
                CONF_EAN: self._selected_ean,
                "uid": self._selected_uid,
                CONF_HDO_SIGNAL: hdo_signal,
                CONF_PRICE_VT: price_vt,
                CONF_PRICE_NT: price_nt,
            },
        )
