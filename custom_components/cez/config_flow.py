"""Config flow pro ČEZ."""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    CezAuthError,
    CezDistribuceApiClient,
    CezInvalidCredentialsError,
    CezUnexpectedLoginPageError,
    extract_supply_points,
)
from .const import (
    CONF_ANLAGE,
    CONF_EAN,
    CONF_HDO_SIGNAL,
    CONF_OM_TYPE,
    CONF_PARTNER,
    CONF_PASSWORD,
    CONF_PORTAL_ENVIRONMENT,
    CONF_PORTAL_PARTNER,
    CONF_PRICE_NT,
    CONF_PRICE_VT,
    CONF_USERNAME,
    DEFAULT_PRICE_NT,
    DEFAULT_PRICE_VT,
    DOMAIN,
    OM_TYPE_CONSUMPTION,
    PORTAL_ENVIRONMENT_NAMES,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


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
        self._selected_environment: tuple[str, str] | None = None
        self._hdo_signals: list[str] = []
        self._client: CezDistribuceApiClient | None = None

    @staticmethod
    def _login_error_key(err: Exception) -> str:
        """Převede výjimku z přihlášení na klíč chyby formuláře."""
        if isinstance(err, CezInvalidCredentialsError):
            return "invalid_auth"
        if isinstance(err, CezUnexpectedLoginPageError):
            return "portal_action_required"
        if not isinstance(err, CezAuthError):
            _LOGGER.exception("Neočekávaná chyba při přihlašování", exc_info=err)
        return "cannot_connect"

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
            # Nové odeslání formuláře = možná jiné údaje, přihlásit znovu.
            self._client = None

            try:
                client = await self._async_get_client()
                _LOGGER.debug("Config flow: načítám odběrná místa.")
                self._supply_points = await self._async_load_supply_points(client)
                _LOGGER.debug(
                    "Config flow: načteno odběrných míst=%d.",
                    len(self._supply_points),
                )
            except Exception as err:  # noqa: BLE001
                errors["base"] = self._login_error_key(err)

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

        show_environment = (
            len({p.get("_portal_environment") for p in self._supply_points}) > 1
        )
        options = {
            p["ean"]: self._supply_point_label(p, show_environment)
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
                if self._selected_environment:
                    await client.select_environment(*self._selected_environment)
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
    # Reauth (změněné heslo) a reconfigure (údaje, prostředí portálu)
    # ------------------------------------------------------------------

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> FlowResult:
        """Spustí HA, když portál odmítne uložené heslo."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Zadání nového hesla ke stávajícímu účtu."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            client = CezDistribuceApiClient(
                username=entry.data[CONF_USERNAME],
                password=user_input[CONF_PASSWORD],
                session=async_get_clientsession(self.hass),
            )
            try:
                await client.login()
            except Exception as err:  # noqa: BLE001
                errors["base"] = self._login_error_key(err)
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]}
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            description_placeholders={"username": entry.data[CONF_USERNAME]},
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Změna přihlašovacích údajů a znovu dohledání prostředí portálu.

        Odběrné místo (EAN) se nemění - je na něm unique_id entry, entity
        i dlouhodobé statistiky. Jiné odběrné místo = nová integrace.
        """
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            client = CezDistribuceApiClient(
                username=user_input[CONF_USERNAME],
                password=user_input[CONF_PASSWORD],
                session=async_get_clientsession(self.hass),
            )
            data_updates: dict[str, Any] = {
                CONF_USERNAME: user_input[CONF_USERNAME],
                CONF_PASSWORD: user_input[CONF_PASSWORD],
            }
            try:
                await client.login()
                environment = None
                if client.environment_options:
                    environment = await client.find_environment_for_supply_point(
                        entry.data.get("uid", ""), entry.data[CONF_EAN]
                    )
                    if environment is None:
                        errors["base"] = "supply_point_not_found"
                elif not any(
                    p.get("ean") == entry.data[CONF_EAN]
                    for p in extract_supply_points(await client.get_supply_points())
                ):
                    errors["base"] = "supply_point_not_found"
            except Exception as err:  # noqa: BLE001
                errors["base"] = self._login_error_key(err)
            if not errors:
                if environment:
                    data_updates[CONF_PORTAL_ENVIRONMENT] = environment[0]
                    data_updates[CONF_PORTAL_PARTNER] = environment[1]
                return self.async_update_reload_and_abort(entry, data_updates=data_updates)

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_USERNAME, default=entry.data.get(CONF_USERNAME, "")
                    ): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            description_placeholders={"ean": entry.data.get(CONF_EAN, "")},
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Pomocné metody
    # ------------------------------------------------------------------

    async def _async_load_supply_points(
        self, client: CezDistribuceApiClient
    ) -> list[dict]:
        """Načte odběrná místa ze všech prostředí portálu (issue #37).

        Účet bez výběru prostředí (běžný případ) má environment_options
        prázdné a načte se jen jednou. Jinak se postupně přepne do každé
        kombinace prostředí/partner a místa se označí, odkud pocházejí.
        """
        if len(client.environment_options) <= 1:
            points = extract_supply_points(await client.get_supply_points())
            for point in points:
                point["_portal_environment"] = client.portal_environment
            return points

        points: list[dict] = []
        seen_eans: set[str] = set()
        for option in client.environment_options:
            environment = (option["environment_id"], option["partner_id"])
            try:
                await client.select_environment(*environment)
                found = extract_supply_points(await client.get_supply_points())
            except Exception:  # noqa: BLE001
                _LOGGER.warning(
                    "Config flow: nepodařilo se načíst odběrná místa v prostředí %s.",
                    environment[0],
                    exc_info=True,
                )
                continue
            _LOGGER.debug(
                "Config flow: prostředí %s, odběrných míst=%d.", environment[0], len(found)
            )
            for point in found:
                ean = point.get("ean")
                if ean and ean in seen_eans:
                    continue
                seen_eans.add(ean)
                point["_portal_environment"] = environment
                points.append(point)
        return points

    def _supply_point_label(self, point: dict, show_environment: bool) -> str:
        details = [point["ean"], point.get("typText") or point.get("typ") or "?"]
        environment = point.get("_portal_environment")
        if show_environment and environment:
            language = "cs" if (self.hass.config.language or "").startswith("cs") else "en"
            names = PORTAL_ENVIRONMENT_NAMES[language]
            details.append(names.get(environment[0], environment[0]))
        address = point.get("adresa", {}).get("adresaComplete") or point["ean"]
        return f"{address} ({', '.join(details)})"

    def _select_point(self, point: dict) -> None:
        """Uloží vybrané odběrné místo."""
        self._selected_ean = point.get("ean", "")
        self._selected_uid = point.get("uid", "")
        self._selected_partner = point.get("partner", "")
        self._selected_environment = point.get("_portal_environment")
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
        title = self._selected_title.strip() or f"ČEZ {self._selected_ean}".strip() or "ČEZ"
        _LOGGER.debug(
            "Config flow: vytvářím config entry (název vyplněn=%s, EAN uložen=%s).",
            bool(title),
            bool(self._selected_ean),
        )
        await self.async_set_unique_id(self._selected_ean)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=title,
            data={
                CONF_USERNAME: self._username,
                CONF_PASSWORD: self._password,
                CONF_EAN: self._selected_ean,
                "uid": self._selected_uid,
                CONF_PARTNER: self._selected_partner,
                CONF_ANLAGE: self._selected_anlage,
                **(
                    {
                        CONF_PORTAL_ENVIRONMENT: self._selected_environment[0],
                        CONF_PORTAL_PARTNER: self._selected_environment[1],
                    }
                    if self._selected_environment
                    else {}
                ),
                CONF_OM_TYPE: self._selected_om_type or OM_TYPE_CONSUMPTION,
                CONF_HDO_SIGNAL: hdo_signal,
                CONF_PRICE_VT: price_vt,
                CONF_PRICE_NT: price_nt,
            },
        )
