"""Config flow pro ČEZ Distribuce."""
from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult

from .api import CezAuthError, CezDistribuceApiClient
from .const import CONF_EAN, CONF_PASSWORD, CONF_USERNAME, DOMAIN

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


async def _fetch_supply_points(
    hass: HomeAssistant, username: str, password: str
) -> list[dict]:
    """Přihlásí se a vrátí seznam odběrných míst."""
    async with aiohttp.ClientSession() as session:
        client = CezDistribuceApiClient(username=username, password=password, session=session)
        await client.login()
        data = await client.get_supply_points()

    vstelles = []
    if data and isinstance(data, dict):
        blocks = data.get("vstelleBlocks", {}).get("blocks", [])
        for block in blocks:
            vstelles.extend(block.get("vstelles", []))
    return vstelles


class CezDistribuceConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Průvodce nastavením integrace ČEZ Distribuce."""

    VERSION = 1

    def __init__(self) -> None:
        self._username: str = ""
        self._password: str = ""
        self._supply_points: list[dict] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Krok 1 – přihlašovací údaje."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._username = user_input[CONF_USERNAME]
            self._password = user_input[CONF_PASSWORD]

            try:
                self._supply_points = await _fetch_supply_points(
                    self.hass, self._username, self._password
                )
            except CezAuthError:
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("Neočekávaná chyba při přihlašování")
                errors["base"] = "cannot_connect"

            if not errors:
                if len(self._supply_points) == 1:
                    # Jen jedno odběrné místo – přeskočit výběr
                    return await self._create_entry(self._supply_points[0])
                elif len(self._supply_points) > 1:
                    return await self.async_step_select_supply_point()
                else:
                    errors["base"] = "no_supply_points"

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_select_supply_point(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Krok 2 – výběr odběrného místa (pokud je jich více)."""
        if user_input is not None:
            selected_ean = user_input[CONF_EAN]
            point = next(
                (p for p in self._supply_points if p.get("ean") == selected_ean), None
            )
            if point:
                return await self._create_entry(point)

        options = {
            p["ean"]: f"{p.get('address', p['ean'])} ({p['ean']})"
            for p in self._supply_points
            if "ean" in p
        }

        return self.async_show_form(
            step_id="select_supply_point",
            data_schema=vol.Schema({vol.Required(CONF_EAN): vol.In(options)}),
        )

    async def _create_entry(self, supply_point: dict) -> FlowResult:
        """Vytvoří config entry."""
        ean = supply_point.get("ean", "")
        uid = supply_point.get("uid", "")
        title = supply_point.get("address") or f"ČEZ {ean}"

        await self.async_set_unique_id(ean)
        self._abort_if_unique_id_configured()

        return self.async_create_entry(
            title=title,
            data={
                CONF_USERNAME: self._username,
                CONF_PASSWORD: self._password,
                CONF_EAN: ean,
                "uid": uid,
            },
        )
