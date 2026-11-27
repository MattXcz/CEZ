"""Config flow pro ČEZ."""
from __future__ import annotations

import logging
import re
import secrets
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    CezApiError,
    CezAuthCodeError,
    CezAuthError,
    CezDistribuceApiClient,
    CezInvalidCredentialsError,
    CezPortalNoDataAccessError,
    CezPortalUnavailableError,
    CezUnexpectedLoginPageError,
    build_mepas_authorize_url,
    extract_business_partners,
    extract_supply_points,
    find_eans,
    make_pkce_pair,
    parse_mepas_redirect,
)
from .const import (
    AUTH_METHOD_BROWSER,
    CONF_ANLAGE,
    CONF_AUTH_METHOD,
    CONF_EAN,
    CONF_HDO_SIGNAL,
    CONF_OM_TYPE,
    CONF_PARTNER,
    CONF_PASSWORD,
    CONF_PORTAL_ENVIRONMENT,
    CONF_PORTAL_PARTNER,
    CONF_PRICE_NT,
    CONF_PRICE_VT,
    CONF_REFRESH_TOKEN,
    CONF_USERNAME,
    DEFAULT_PRICE_NT,
    DEFAULT_PRICE_VT,
    DOMAIN,
    MEPAS_BROWSER_SCOPE,
    MEPAS_LOGIN_REDIRECT_URI,
    MEPAS_SCOPE,
    OM_TYPE_CONSUMPTION,
    PORTAL_ENVIRONMENT_NAMES,
    pnd_assembly_code_for,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)

CONF_REDIRECT_URL = "redirect_url"
# Přihlášení v prohlížeči nezná typ odběrného místa z portálu - uživatel
# volí jen spotřebu/výrobu; "V" zastupuje celou skupinu V/M/B (chovají se
# stejně, viz OM_TYPES_PRODUCTION).
OM_TYPE_PRODUCTION_CHOICE = "V"
_EAN_FORMAT = re.compile(r"^\d{18}$")


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
        # Přihlášení v prohlížeči (Google/Apple...) - PKCE a state platí
        # pro jednu authorize URL; po neúspěchu se vygeneruje nová.
        self._browser_scope: str = MEPAS_BROWSER_SCOPE
        self._browser_state: str = ""
        self._browser_verifier: str = ""
        self._browser_url: str = ""
        self._browser_client: CezDistribuceApiClient | None = None
        self._browser_partners: list[str] = []
        self._browser_eans: list[str] = []

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Volba „Konfigurovat“ - ceny VT/NT (issue #44)."""
        return CezOptionsFlow()

    @classmethod
    @callback
    def async_supports_options_flow(cls, config_entry: config_entries.ConfigEntry) -> bool:
        """Ceny VT/NT dávají smysl jen u běžné spotřeby - výroba/mikrozdroj
        (FVE) cenový senzor nemá (issue #24)."""
        om_type = config_entry.data.get(CONF_OM_TYPE) or OM_TYPE_CONSUMPTION
        # Přihlášení v prohlížeči nemá HDO, takže ani cenový senzor.
        browser = config_entry.data.get(CONF_AUTH_METHOD) == AUTH_METHOD_BROWSER
        return om_type == OM_TYPE_CONSUMPTION and not browser

    @staticmethod
    def _login_error_key(err: Exception) -> str:
        """Převede výjimku z přihlášení na klíč chyby formuláře."""
        if isinstance(err, CezInvalidCredentialsError):
            return "invalid_auth"
        if isinstance(err, CezPortalUnavailableError):
            _LOGGER.warning("%s", err)
            return "portal_unavailable"
        if isinstance(err, CezUnexpectedLoginPageError):
            return "portal_action_required"
        if isinstance(err, CezPortalNoDataAccessError):
            _LOGGER.warning("%s", err)
            return "portal_no_data_access"
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
        """Volba způsobu přihlášení."""
        return self.async_show_menu(
            step_id="user", menu_options=["password", "browser_login"]
        )

    async def async_step_password(
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
            step_id="password",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Přihlášení v prohlížeči (Google, Apple i heslo) - jen data appky Proud
    # ------------------------------------------------------------------

    def _new_browser_login(self) -> None:
        """Nová authorize URL s čerstvým PKCE a state (kód je jednorázový)."""
        self._browser_verifier, challenge = make_pkce_pair()
        self._browser_state = secrets.token_urlsafe(24)
        self._browser_url = build_mepas_authorize_url(
            self._browser_state, challenge, self._browser_scope
        )

    async def async_step_browser_login(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Uživatel se přihlásí v prohlížeči a vloží adresu cda://validation/?code=...

        Přesměrování na Home Assistant ČEZ nepovoluje (redirect_uri klienta
        appky Proud je pevně cda://validation/), proto se adresa kopíruje
        ručně. Integrace si uloží jen refresh token, ne údaje k Google/Apple.
        """
        errors: dict[str, str] = {}
        if not self._browser_url:
            self._new_browser_login()

        if user_input is not None:
            parsed = parse_mepas_redirect(user_input.get(CONF_REDIRECT_URL, ""))
            error = parsed.get("error")
            if error == "invalid_scope" and self._browser_scope != MEPAS_SCOPE:
                # Klient appky Proud nemá povolený offline_access - zkusíme
                # bez něj (refresh token pak může, ale nemusí přijít).
                _LOGGER.info("CAS odmítl scope offline_access, zkouším jen openid.")
                self._browser_scope = MEPAS_SCOPE
                self._new_browser_login()
                errors["base"] = "scope_retry"
            elif error:
                _LOGGER.warning("Přihlášení v prohlížeči vrátilo chybu: %s", error)
                self._new_browser_login()
                errors["base"] = "browser_login_failed"
            elif not parsed.get("code"):
                errors[CONF_REDIRECT_URL] = "invalid_redirect_url"
            elif parsed.get("state") and parsed["state"] != self._browser_state:
                # Adresa z jiného (staršího) pokusu - její kód k našemu
                # PKCE verifieru nepatří.
                self._new_browser_login()
                errors["base"] = "state_mismatch"
            else:
                errors = await self._async_finish_browser_login(parsed["code"])
                if not errors and self.source in (
                    config_entries.SOURCE_REAUTH,
                    config_entries.SOURCE_RECONFIGURE,
                ):
                    errors = await self._async_check_existing_entry_access()
                if not errors:
                    return await self._async_after_browser_login()

        return self.async_show_form(
            step_id="browser_login",
            data_schema=vol.Schema({vol.Required(CONF_REDIRECT_URL): str}),
            description_placeholders={
                "authorize_url": self._browser_url,
                "redirect_uri": MEPAS_LOGIN_REDIRECT_URI,
            },
            errors=errors,
        )

    async def _async_finish_browser_login(self, code: str) -> dict[str, str]:
        """Vymění kód za tokeny. Vrací chyby formuláře (prázdné = OK)."""
        client = CezDistribuceApiClient(
            username="", password="", session=async_get_clientsession(self.hass)
        )
        try:
            await client.exchange_mepas_code(code, self._browser_verifier, self._browser_scope)
        except CezAuthCodeError as err:
            _LOGGER.warning("%s", err)
            self._new_browser_login()
            return {"base": "code_expired"}
        except Exception as err:  # noqa: BLE001
            self._new_browser_login()
            if not isinstance(err, (CezAuthError, CezApiError)):
                _LOGGER.exception("Neočekávaná chyba při výměně kódu", exc_info=err)
            return {"base": "cannot_connect"}
        if not client.mepas_refresh_token:
            # Bez refresh tokenu by integrace přestala fungovat, jakmile
            # vyprší access token (desítky minut) - nemá smysl ji zakládat.
            _LOGGER.warning(
                "ČEZ nevydal refresh token (scope=%s) - přihlášení v prohlížeči "
                "nejde trvale použít.",
                self._browser_scope,
            )
            self._new_browser_login()
            return {"base": "no_refresh_token"}
        self._browser_client = client
        return {}

    async def _async_check_existing_entry_access(self) -> dict[str, str]:
        """Při reauth/reconfigure: nový účet musí vidět odběrné místo entry."""
        entry = (
            self._get_reauth_entry()
            if self.source == config_entries.SOURCE_REAUTH
            else self._get_reconfigure_entry()
        )
        errors = await self._async_check_pnd_access(
            entry.data.get(CONF_PARTNER, ""),
            entry.data[CONF_EAN],
            entry.data.get(CONF_OM_TYPE) or OM_TYPE_CONSUMPTION,
        )
        if errors.get("base") == "pnd_check_failed":
            self._new_browser_login()
            return {"base": "supply_point_not_found"}
        return errors

    async def _async_after_browser_login(self) -> FlowResult:
        """Po úspěšném přihlášení: reauth/reconfigure uloží token, jinak výběr OM."""
        client = self._browser_client
        assert client is not None
        token = client.mepas_refresh_token
        if self.source in (config_entries.SOURCE_REAUTH, config_entries.SOURCE_RECONFIGURE):
            entry = (
                self._get_reauth_entry()
                if self.source == config_entries.SOURCE_REAUTH
                else self._get_reconfigure_entry()
            )
            return self.async_update_reload_and_abort(
                entry,
                data_updates={
                    CONF_AUTH_METHOD: AUTH_METHOD_BROWSER,
                    CONF_REFRESH_TOKEN: token,
                },
            )
        return await self.async_step_browser_supply_point()

    async def _async_discover_browser_supply_points(self) -> None:
        """Partner z tokenu/profilu a kandidáti EAN z pnd/status (best-effort)."""
        client = self._browser_client
        assert client is not None
        partners = extract_business_partners(client.mepas_claims)
        if not partners:
            try:
                partners = extract_business_partners(await client.get_mepas_userinfo())
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Profil uživatele (userinfo) se nepodařilo načíst: %s", err)
        self._browser_partners = partners
        eans: list[str] = []
        for partner in partners:
            try:
                status = await client.get_pnd_status(partner)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("pnd/status pro partnera se nepodařil: %s", err)
                continue
            eans.extend(e for e in find_eans(status) if e not in eans)
        self._browser_eans = eans
        _LOGGER.debug(
            "Přihlášení v prohlížeči: partnerů=%d, nalezených EAN=%d.",
            len(partners),
            len(eans),
        )

    async def async_step_browser_supply_point(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Výběr odběrného místa bez portálu (EAN, partner, spotřeba/výroba)."""
        errors: dict[str, str] = {}
        if user_input is None and not self._browser_partners and not self._browser_eans:
            await self._async_discover_browser_supply_points()

        if user_input is not None:
            ean = re.sub(r"\s", "", str(user_input[CONF_EAN]))
            partner = re.sub(r"\s", "", str(user_input[CONF_PARTNER]))
            om_type = user_input[CONF_OM_TYPE]
            if not _EAN_FORMAT.match(ean):
                errors[CONF_EAN] = "invalid_ean"
            elif not partner.isdigit():
                errors[CONF_PARTNER] = "invalid_partner"
            else:
                await self.async_set_unique_id(ean)
                self._abort_if_unique_id_configured()
                errors = await self._async_check_pnd_access(partner, ean, om_type)
                if not errors:
                    return self._async_create_browser_entry(partner, ean, om_type)

        ean_field: Any = vol.In(self._browser_eans) if len(self._browser_eans) > 1 else str
        ean_default = self._browser_eans[0] if self._browser_eans else vol.UNDEFINED
        partner_default = self._browser_partners[0] if self._browser_partners else vol.UNDEFINED
        language = "cs" if (self.hass.config.language or "").startswith("cs") else "en"
        om_labels = (
            {OM_TYPE_CONSUMPTION: "Spotřeba", OM_TYPE_PRODUCTION_CHOICE: "Výroba / mikrozdroj"}
            if language == "cs"
            else {OM_TYPE_CONSUMPTION: "Consumption", OM_TYPE_PRODUCTION_CHOICE: "Production / microsource"}
        )
        return self.async_show_form(
            step_id="browser_supply_point",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_EAN, default=ean_default): ean_field,
                    vol.Required(CONF_PARTNER, default=partner_default): str,
                    vol.Required(CONF_OM_TYPE, default=OM_TYPE_CONSUMPTION): vol.In(om_labels),
                }
            ),
            description_placeholders={
                "ean_count": str(len(self._browser_eans)),
            },
            errors=errors,
        )

    async def _async_check_pnd_access(
        self, partner: str, ean: str, om_type: str
    ) -> dict[str, str]:
        """Ověří, že pnd/data pro zadané údaje odpovídá (poslední 3 dny)."""
        client = self._browser_client
        assert client is not None
        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        fmt = "%Y-%m-%dT%H:%M:%S.000Z"
        try:
            await client.get_pnd_data(
                partner,
                ean,
                (now - timedelta(days=3)).strftime(fmt),
                now.strftime(fmt),
                pnd_assembly_code_for(om_type),
            )
        except CezInvalidCredentialsError:
            return {"base": "invalid_auth"}
        except CezApiError as err:
            _LOGGER.warning("Ověření hodinových dat selhalo: %s", err)
            return {"base": "pnd_check_failed"}
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Ověření hodinových dat selhalo: %s", err)
            return {"base": "cannot_connect"}
        return {}

    @callback
    def _async_create_browser_entry(self, partner: str, ean: str, om_type: str) -> FlowResult:
        client = self._browser_client
        assert client is not None
        title = f"ČEZ {ean}" + (" (výroba)" if om_type != OM_TYPE_CONSUMPTION else "")
        return self.async_create_entry(
            title=title,
            data={
                CONF_AUTH_METHOD: AUTH_METHOD_BROWSER,
                CONF_REFRESH_TOKEN: client.mepas_refresh_token,
                CONF_EAN: ean,
                "uid": "",
                CONF_PARTNER: partner,
                CONF_ANLAGE: "",
                CONF_OM_TYPE: om_type,
                CONF_HDO_SIGNAL: "",
                CONF_PRICE_VT: DEFAULT_PRICE_VT,
                CONF_PRICE_NT: DEFAULT_PRICE_NT,
            },
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
        # výroby/mikrozdroje (FVE, typ "V"/"M"/"B") žádný spínací signál nemá,
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
        """Spustí HA, když portál odmítne uložené heslo (nebo refresh token)."""
        if entry_data.get(CONF_AUTH_METHOD) == AUTH_METHOD_BROWSER:
            return await self.async_step_browser_login()
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
        if entry.data.get(CONF_AUTH_METHOD) == AUTH_METHOD_BROWSER:
            # Jediný údaj je refresh token - překonfigurování = nové
            # přihlášení v prohlížeči.
            return await self.async_step_browser_login()
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
                if not errors:
                    # Portál může pro stejný EAN časem vrátit jiné UID
                    # (issue #43) - překonfigurování ho rovnou obnoví.
                    new_uid = await client.resolve_supply_point_uid(entry.data[CONF_EAN])
                    if new_uid and new_uid != entry.data.get("uid"):
                        data_updates["uid"] = new_uid
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


class CezOptionsFlow(config_entries.OptionsFlowWithReload):
    """Změna cen VT/NT bez nového přidání odběrného místa (issue #44).

    Ceny se ukládají do entry.options; senzor „Aktuální cena“ je čte
    přednostně odtud, se zálohou na entry.data (ceny zadané při přidání).
    OptionsFlowWithReload po uložení integraci sám znovu načte."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(
                data={
                    CONF_PRICE_VT: user_input[CONF_PRICE_VT],
                    CONF_PRICE_NT: user_input[CONF_PRICE_NT],
                }
            )

        entry = self.config_entry
        current_vt = entry.options.get(
            CONF_PRICE_VT, entry.data.get(CONF_PRICE_VT, DEFAULT_PRICE_VT)
        )
        current_nt = entry.options.get(
            CONF_PRICE_NT, entry.data.get(CONF_PRICE_NT, DEFAULT_PRICE_NT)
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PRICE_VT, default=float(current_vt)): vol.All(
                        vol.Coerce(float), vol.Range(min=0)
                    ),
                    vol.Required(CONF_PRICE_NT, default=float(current_nt)): vol.All(
                        vol.Coerce(float), vol.Range(min=0)
                    ),
                }
            ),
        )
