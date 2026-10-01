"""Async REST klient pro ČEZ Distribuce."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import secrets
import urllib.parse
import uuid
from typing import Any

import aiohttp
from bs4 import BeautifulSoup

try:
    # Jako součást HA balíčku custom_components.cez
    from .const import (
        APP_BUILD_NUMBER,
        APP_VERSION,
        AWS_API_GATEWAY_STATIC_URL,
        MEPAS_CLIENT_ID,
        MEPAS_LOGIN_REDIRECT_URI,
        MEPAS_SCOPE,
        MEPAS_TOKEN_URL,
    )
except ImportError:
    # Samostatně (diagnostické skripty ve scripts/) - const.py leží ve
    # stejné složce, nebo je natažen přes importlib.
    from const import (
        APP_BUILD_NUMBER,
        APP_VERSION,
        AWS_API_GATEWAY_STATIC_URL,
        MEPAS_CLIENT_ID,
        MEPAS_LOGIN_REDIRECT_URI,
        MEPAS_SCOPE,
        MEPAS_TOKEN_URL,
    )

_LOGGER = logging.getLogger(__name__)

# ČEZ přesunul centrální autentizaci (CAS/MEPAS) z cas.cez.cz na mepas.cez.cz.
# Starý host pro starý client_id vrací 403 "Aplikace není autorizovaná k použití
# přihlašování pomocí CASu", takže přihlašovací formulář ani pole 'execution'
# neexistují a přihlášení selže.
CAS_BASE_URL = "https://mepas.cez.cz/cas"
RESPONSE_TYPE = "code"
SCOPE = "openid"

# client_id převzat z tlačítka přihlášení na https://dip.cezdistribuce.cz/irj/portal
CEZ_DISTRIBUCE_CLIENT_ID = "emiCuDBbivwYxraX.dip.dip.ext.zak.prod.v1"
CEZ_DISTRIBUCE_BASE_URL = "https://dip.cezdistribuce.cz/irj/portal"

LOGIN_RETRIES = 2

# Mezikroky po přihlášení (Angular aplikace /irj/portal/landing, issue #37).
# Účet s více prostředími (např. Domácnost + Podnikatel pod jedním e-mailem)
# musí po přihlášení vybrat prostředí a partnera; do té doby všechna datová
# API vrací HTTP 200 s prázdným text/html tělem.
LANDING_CHECK_PATH = "landing?path=check"
LANDING_ENVIRONMENTS_PATH = "landing?path=select-environment"
LANDING_MAX_STEPS = 3
# Pořadí, ve kterém se prostředí vybírá automaticky, když volající žádné
# nepředepsal (stávající config entries před issue #37).
_ENVIRONMENT_PRIORITY = ("D", "P", "S", "O", "PND")

# Bez explicitního timeoutu se aiohttp spoléhá na svůj default (300 s) - pokud
# portál na request nikdy neodpoví, celý config flow / update se na několik
# minut zasekne, aniž by to bylo v logu vidět jako chyba. Krátký timeout tohle
# převede na rychlé, viditelné selhání (odchytává ho volající přes
# `except Exception`).
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)


class CezAuthError(Exception):
    """Chyba přihlášení."""


class CezInvalidCredentialsError(CezAuthError):
    """Portál odmítl jméno/heslo - jediná chyba, která má spustit reauth."""


class CezApiError(Exception):
    """Obecná chyba API."""


class CezUnexpectedLoginPageError(CezApiError):
    """Po odeslání přihlašovacích údajů nedoběhl řetěz přesměrování na portál.

    Typicky CAS/portál vložil mezikrok, se kterým integrace nepočítá - např.
    výběr účtu, když jeden e-mail má víc účtů (osobní + podnikatelský,
    issue #37). Struktura stránky je zalogovaná přes _describe_page().
    """


def extract_supply_points(data: Any) -> list[dict]:
    """Vytáhne seznam odběrných míst z odpovědi get_supply_points()."""
    blocks = (
        (data.get("vstelleBlocks") or {}).get("blocks", [])
        if isinstance(data, dict)
        else []
    )
    return [point for block in blocks for point in block.get("vstelles", [])]


# Klíčová slova, podle kterých v diagnostice odhadujeme, že mezikrok po
# přihlášení je výběr účtu/profilu (issue #37). Jen pro log - na chování
# to nemá vliv.
_ACCOUNT_SELECTION_HINTS = (
    "účet",
    "účtu",
    "profil",
    "podnikatel",
    "fyzická osoba",
    "právnická osoba",
    "vyberte",
    "zvolte",
    "account",
    "select",
)

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_DIGITS_RE = re.compile(r"\d")


def _mask_text(text: str) -> str:
    """Zamaskuje e-maily a číslice (EAN, IČ, čísla účtů) - log jde do issue."""
    return _DIGITS_RE.sub("#", _EMAIL_RE.sub("<email>", text))


def _describe_page(url: str, status: int, html: str) -> dict[str, Any]:
    """Vrátí strukturu neočekávané stránky pro diagnostiku (bez osobních údajů).

    Hodnoty polí, query parametrů ani popisky voleb se nelogují (mohou
    obsahovat jména, IČ, tokeny); jen jejich názvy, typy a počty.
    """
    parsed = urllib.parse.urlparse(url)
    info: dict[str, Any] = {
        "status": status,
        "host": parsed.hostname,
        "path": parsed.path,
        "query_keys": sorted(urllib.parse.parse_qs(parsed.query).keys()),
    }
    soup = BeautifulSoup(html or "", "html.parser")
    info["title"] = _mask_text(soup.title.get_text(strip=True)) if soup.title else None
    info["headings"] = [
        _mask_text(h.get_text(" ", strip=True))[:120]
        for h in soup.find_all(["h1", "h2", "h3"])[:8]
    ]
    forms = []
    for form in soup.find_all("form")[:5]:
        action = urllib.parse.urlparse(form.get("action") or "")
        fields = []
        for field in form.find_all(["input", "select", "button", "textarea"]):
            entry = {
                "tag": field.name,
                "type": field.get("type"),
                "name": field.get("name"),
            }
            if field.name == "select":
                entry["options"] = len(field.find_all("option"))
            fields.append(entry)
        radios: dict[str, int] = {}
        for f in fields:
            if f["type"] in ("radio", "checkbox") and f["name"]:
                radios[f["name"]] = radios.get(f["name"], 0) + 1
        forms.append(
            {
                "id": form.get("id"),
                "method": (form.get("method") or "get").lower(),
                "action_path": action.path or None,
                "fields": fields[:30],
                "radio_groups": radios,
            }
        )
    info["forms"] = forms
    text = soup.get_text(" ", strip=True).lower()
    info["account_selection_hints"] = [h for h in _ACCOUNT_SELECTION_HINTS if h in text]
    info["_text_preview"] = _mask_text(" ".join(soup.get_text(" ", strip=True).split()))[:800]
    return info


def _is_login_form(html: str) -> bool:
    """True, pokud stránka je (znovu) CAS přihlašovací formulář."""
    soup = BeautifulSoup(html or "", "html.parser")
    return bool(
        soup.find("input", {"name": "execution"})
        and soup.find("input", {"type": "password"})
    )


def _log_unexpected_page(step: str, url: str, status: int, html: str) -> dict[str, Any]:
    """Zaloguje popis neočekávané stránky (WARNING struktura, DEBUG náhled textu)."""
    info = _describe_page(url, status, html)
    preview = info.pop("_text_preview")
    _LOGGER.warning(
        "ČEZ %s: neočekávaná stránka po přihlášení (možná výběr účtu, issue #37). "
        "Pošlete prosím tento řádek do issue: %s",
        step,
        json.dumps(info, ensure_ascii=False),
    )
    _LOGGER.debug("ČEZ %s: náhled textu stránky (maskováno): %s", step, preview)
    return info


class _InvalidJsonResponse(CezApiError):
    """Odpověď API nebyla validní JSON."""

    def __init__(self, url: str, status: int, content_type: str, preview: str) -> None:
        self.url = url
        self.status = status
        self.content_type = content_type
        self.preview = preview
        super().__init__(
            f"Neplatná JSON odpověď z {url} "
            f"(HTTP {status}, Content-Type: {content_type}): {preview}"
        )

    @property
    def looks_like_portal_html(self) -> bool:
        """Vrací True, pokud ČEZ místo API dat vrátil HTML portál."""
        return (
            "text/html" in self.content_type.lower()
            or self.preview.lower().lstrip().startswith("<html")
        )


def _response_diagnostics(
    resp: aiohttp.ClientResponse, body_chars: int
) -> dict[str, Any]:
    """Popíše nevalidní odpověď bez těla a tajných hlaviček."""

    def describe_url(url: str) -> dict[str, Any]:
        parsed = urllib.parse.urlparse(url)
        return {
            "host": parsed.hostname,
            "path": parsed.path,
            "query_keys": sorted(urllib.parse.parse_qs(parsed.query).keys()),
        }

    diagnostics: dict[str, Any] = {
        "status": resp.status,
        "url": describe_url(str(resp.url)),
        "content_type": resp.headers.get("Content-Type"),
        "content_length_header": resp.headers.get("Content-Length"),
        "body_chars": body_chars,
        "redirects": [
            {
                "status": previous.status,
                **describe_url(str(previous.url)),
            }
            for previous in resp.history
        ],
    }
    for header in ("Server", "Transfer-Encoding", "Cache-Control"):
        if value := resp.headers.get(header):
            diagnostics[header.lower().replace("-", "_")] = value
    return diagnostics


def _safe_endpoint(url: str) -> str:
    """Vrátí host, cestu a názvy query parametrů bez jejich hodnot."""
    parsed = urllib.parse.urlparse(url)
    query_keys = sorted(urllib.parse.parse_qs(parsed.query).keys())
    query = f"?keys={','.join(query_keys)}" if query_keys else ""
    return f"{parsed.hostname}{parsed.path}{query}"


def _make_pkce_pair() -> tuple[str, str]:
    """Vygeneruje (code_verifier, code_challenge) dle RFC 7636 (S256)."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode("ascii")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    return verifier, challenge


class CezDistribuceApiClient:
    """Async klient pro ČEZ Distribuce REST API."""

    def __init__(
        self,
        username: str,
        password: str,
        session: aiohttp.ClientSession,
        base_url: str = CEZ_DISTRIBUCE_BASE_URL,
        client_id: str = CEZ_DISTRIBUCE_CLIENT_ID,
        portal_environment: tuple[str, str] | None = None,
    ) -> None:
        self._username = username
        self._password = password
        self._base_url = base_url
        self._client_id = client_id
        self._session = session

        # Výběr prostředí portálu (environmentId, partnerId), issue #37.
        # Předvolba z config entry; při každém (re)loginu se vybere znovu.
        self._portal_environment = portal_environment
        # Všechny dostupné kombinace prostředí/partner z posledního loginu,
        # prázdné, pokud portál výběr nevyžadoval.
        self.environment_options: list[dict[str, str]] = []
        # True, když předvolené prostředí při posledním loginu portál
        # nenabídl a vybralo se výchozí - volající má prostředí dohledat.
        self.preferred_environment_missing = False

        redirect_url = f"{base_url}/common-api?path=/common/header"
        self._portal_host = urllib.parse.urlparse(base_url).hostname
        # Doslova URL tlačítka "Přihlásit" na anonymní stránce portálu
        # (dip.cezdistribuce.cz/irj/portal), včetně cesty /oidc/oidcAuthorize
        # a pořadí parametrů. Návrat vede přes common/header, který portál
        # používá po přihlášení v prohlížeči.
        self._authorize_url = (
            f"{CAS_BASE_URL}/oidc/oidcAuthorize"
            f"?response_type={RESPONSE_TYPE}"
            f"&redirect_uri={urllib.parse.quote(redirect_url, safe='')}"
            f"&client_id={client_id}"
            f"&scope={SCOPE}"
        )

        # Sdílíme jeden aiohttp session, ale potřebujeme oddělit cookie jary
        self._auth_cookie_jar = aiohttp.CookieJar()
        self._anon_cookie_jar = aiohttp.CookieJar()

        # Tokeny pro ČEZ API
        self._api_token: str | None = None
        self._anon_api_token: str | None = None

        # MEPAS/AWS Gateway (appka Proud) - hodinová/15min spotřeba.
        # Token má krátkou platnost (JWT s 'exp'), obnovuje se přes
        # login_mepas() při každém volání get_pnd_* metod, které selžou
        # na neplatný/chybějící token.
        self._mepas_access_token: str | None = None
        self._mepas_installation_id = uuid.uuid4().hex[:16]

    # ------------------------------------------------------------------
    # Přihlášení (portál)
    # ------------------------------------------------------------------

    async def login(self) -> None:
        """Přihlásí se přes CAS OAuth a načte tokeny.

        Pořadí kroků kopíruje prohlížeč (ověřeno diagnostickým skriptem
        scripts/test_mepas_login.py s CEZ_FLOW=browser, issue #24):
          1) GET oidcAuthorize -> CAS přesměruje na login formulář
          2) POST přihlašovacích údajů -> callback -> portál dostane kód
          3) GET /token/get (autentizovaný API token)
          4) anonymní token v čistém sessionu
        """
        _LOGGER.debug("Přihlašuji se do ČEZ Distribuce...")

        # Vyčistit jar z předchozího přihlášení – jinak CAS při druhém a dalším
        # přihlášení v témže procesu rozpozná platnou session, přeskočí
        # přihlašovací formulář a přesměruje rovnou na portál, čímž zmizí
        # pole 'execution' a login selže (viz issue #21).
        self._auth_cookie_jar.clear()

        connector = aiohttp.TCPConnector()
        async with aiohttp.ClientSession(
            connector=connector,
            max_line_size=8190 * 4,
            max_field_size=8190 * 4,
            timeout=REQUEST_TIMEOUT,
        ) as auth_session:
            auth_session._cookie_jar = self._auth_cookie_jar  # noqa: SLF001

            # Krok 1 – GET authorize (jako prohlížeč). Bez SSO session nás
            # CAS přesměruje na /cas/login?service=... s formulářem; resp.url
            # je pak ta login URL, na kterou míří POST v kroku 2.
            _LOGGER.debug("CAS authorize požadavek: endpoint=%s", _safe_endpoint(self._authorize_url))
            async with auth_session.get(self._authorize_url) as resp:
                login_url = str(resp.url)
                html = await resp.text()
                _LOGGER.debug(
                    "CAS authorize odpověď: HTTP=%d endpoint=%s content_type=%s redirects=%d",
                    resp.status,
                    _safe_endpoint(login_url),
                    resp.headers.get("Content-Type", "neznámý"),
                    len(resp.history),
                )
            if "/cas/login" not in login_url:
                raise CezAuthError(
                    f"CAS authorize: neočekávané přesměrování na {login_url!r}"
                )

            soup = BeautifulSoup(html, "html.parser")
            execution_input = soup.find("input", {"name": "execution"})
            if not execution_input:
                raise CezAuthError("Nepodařilo se najít execution token na přihlašovací stránce.")
            execution = execution_input.get("value", "")

            # Krok 2 – POST přihlašovacích údajů. Řetěz přesměrování končí
            # na portálu /irj/portal?code=... - tím je kód doručen a session
            # založená. Landing iView SAP portálu ale ne-browserovému
            # User-Agentu vrací 404 ("Could not open iView. The iView is not
            # compatible with your browser..."); to je kosmetické, cookies
            # jsou nastavené a /token/get níž funguje (issue #24).
            _LOGGER.debug("Odesílám přihlašovací formulář CAS na endpoint=%s", _safe_endpoint(login_url))
            async with auth_session.post(
                login_url,
                data={
                    "username": self._username,
                    "password": self._password,
                    "execution": execution,
                    "_eventId": "submit",
                    "geolocation": "",
                },
            ) as resp:
                landed_on_portal = resp.url.host == self._portal_host
                landed_with_code = landed_on_portal and "code" in resp.url.query
                _LOGGER.debug(
                    "Login POST skončil: HTTP %s, host=%s, path=%s, code=%s, redirectů=%d",
                    resp.status,
                    resp.url.host,
                    resp.url.path,
                    landed_with_code,
                    len(resp.history),
                )
                if resp.status == 404 and landed_with_code:
                    _LOGGER.debug(
                        "Portál vrátil 404 z landing iView, kód doručen - pokračuji"
                    )
                elif resp.status not in (200, 302):
                    html = await resp.text()
                    _log_unexpected_page("login POST", str(resp.url), resp.status, html)
                    raise CezAuthError(f"Přihlášení selhalo, HTTP {resp.status}")
                else:
                    html = await resp.text()
                    if "Nesprávné" in html or "incorrect" in html.lower():
                        raise CezInvalidCredentialsError("Nesprávné přihlašovací údaje.")
                    if not landed_on_portal and _is_login_form(html):
                        # CAS vrátil znovu přihlašovací formulář = údaje
                        # neprošly, jen s jinou hláškou než "Nesprávné".
                        raise CezInvalidCredentialsError("Nesprávné přihlašovací údaje.")
                    if not landed_on_portal:
                        # Řetěz přesměrování zůstal na CAS (mepas.cez.cz) -
                        # CAS chce po uživateli ještě něco (výběr účtu,
                        # souhlas, změna hesla...). Bez toho /token/get
                        # vrátí HTML portálu a chyba by byla matoucí.
                        info = _log_unexpected_page(
                            "login POST", str(resp.url), resp.status, html
                        )
                        raise CezUnexpectedLoginPageError(
                            "Po přihlášení CAS nepřesměroval na portál "
                            f"(zůstal na {info['host']}{info['path']}). "
                            "Pravděpodobně je vyžadován mezikrok, např. výběr účtu."
                        )

            # Krok 3 – načíst API token (autentizovaný)
            token_url = f"{self._base_url}/rest-auth-api?path=/token/get"
            async with auth_session.get(token_url) as resp:
                try:
                    data = await self._read_json_response(resp, token_url)
                except _InvalidJsonResponse as err:
                    if err.looks_like_portal_html:
                        _log_unexpected_page(
                            "token/get", str(resp.url), resp.status, await resp.text()
                        )
                        raise CezUnexpectedLoginPageError(
                            "Portál po přihlášení nevydal API token (vrátil HTML). "
                            "Pravděpodobně je vyžadován mezikrok, např. výběr účtu."
                        ) from err
                    raise
                self._api_token = data if isinstance(data, str) else data.get("data") or data.get("token")

            # Krok 3b – mezikroky landing stránky (výběr prostředí, issue #37)
            await self._complete_landing_checks(auth_session)

            # Uložit cookies pro pozdější použití
            self._auth_cookies = auth_session.cookie_jar

        # Krok 4 – anonymní token (nový session bez přihlášení)
        async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as anon_session:
            token_url = f"{self._base_url}/anonymous/rest-auth-api?path=/token/get"
            async with anon_session.get(token_url) as resp:
                data = await self._read_json_response(resp, token_url)
                self._anon_api_token = data if isinstance(data, str) else data.get("data") or data.get("token")
            self._anon_cookies = anon_session.cookie_jar

        _LOGGER.debug("Přihlášení OK, tokeny načteny.")

    # ------------------------------------------------------------------
    # Landing mezikroky (výběr prostředí, issue #37)
    # ------------------------------------------------------------------

    async def _landing_request(
        self, session: aiohttp.ClientSession, method: str, path: str
    ) -> Any:
        """Zavolá landing API a rozbalí obálku {statusCode, data} jako frontend."""
        url = f"{self._base_url}/{path}"
        headers = {"X-Request-Token": self._api_token} if self._api_token else {}
        async with session.request(method, url, headers=headers) as resp:
            if method == "POST" and resp.status < 400 and not (await resp.text()).strip():
                # Akce bez návratové hodnoty - výsledek ověří následný check.
                return None
            raw = await self._read_json_response(resp, url)
        if isinstance(raw, dict) and "statusCode" in raw:
            if raw["statusCode"] != 200:
                raise CezApiError(
                    f"Landing {_safe_endpoint(url)} vrátil statusCode={raw['statusCode']}"
                )
            return raw.get("data")
        return raw

    async def _complete_landing_checks(self, session: aiohttp.ClientSession) -> None:
        """Projde kontroly po přihlášení stejně jako landing stránka portálu.

        Účtům bez mezikroků vrátí check anyCheckNeeded=false a nic se neděje.
        Pokud samotný check selže, jen to zalogujeme - u účtů, kterým výběr
        nechybí, to přihlášení rozbít nesmí.
        """
        self.environment_options = []
        self.preferred_environment_missing = False
        selected = False
        for _ in range(LANDING_MAX_STEPS):
            try:
                check = await self._landing_request(session, "GET", LANDING_CHECK_PATH)
            except (CezApiError, aiohttp.ClientError, TimeoutError) as err:
                if selected:
                    raise CezUnexpectedLoginPageError(
                        f"Po výběru prostředí portálu selhala kontrola stavu: {err}"
                    ) from err
                _LOGGER.debug("Landing check se nepodařil, pokračuji bez něj: %r", err)
                return
            if not isinstance(check, dict):
                _LOGGER.debug("Landing check vrátil neočekávaný typ %s.", type(check).__name__)
                return
            _LOGGER.debug(
                "Landing check: anyCheckNeeded=%s environmentSelect=%s "
                "accessConditions=%s spopContract=%s",
                check.get("anyCheckNeeded"),
                check.get("environmentSelect"),
                check.get("accessConditions"),
                check.get("spopContract"),
            )
            if check.get("anyCheckNeeded") is False:
                if self._portal_environment and not selected:
                    await self._reapply_preferred_environment(session)
                return
            if check.get("environmentSelect"):
                await self._select_environment_during_login(session)
                selected = True
                continue
            if check.get("accessConditions") or check.get("spopContract"):
                raise CezUnexpectedLoginPageError(
                    "Portál ČEZ Distribuce vyžaduje potvrzení podmínek používání "
                    "nebo smlouvy. Přihlaste se jednou na dip.cezdistribuce.cz "
                    "v prohlížeči a potvrďte je."
                )
            return
        raise CezUnexpectedLoginPageError(
            "Portál ČEZ Distribuce stále vyžaduje výběr prostředí ani po "
            f"{LANDING_MAX_STEPS} pokusech; data by se nenačetla."
        )

    async def _reapply_preferred_environment(self, session: aiohttp.ClientSession) -> None:
        """Prosadí uložené prostředí, i když ho portál nevyžaduje.

        Pokud si portál pamatuje poslední výběr pro celého uživatele (ne jen
        pro session), dvě entries pod jedním účtem by si jinak prostředí
        navzájem přepínaly. Chyba se jen loguje - účtům s jedním prostředím
        nesmí rozbít přihlášení.
        """
        env_id, partner_id = self._portal_environment
        try:
            await self._post_select_environment(session, env_id, partner_id)
        except (CezApiError, aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.debug("Opětovný výběr prostředí %s se nepodařil: %r", env_id, err)
        else:
            _LOGGER.debug("Opětovně vybráno uložené prostředí portálu %s.", env_id)

    async def _post_select_environment(
        self, session: aiohttp.ClientSession, env_id: str, partner_id: str
    ) -> None:
        await self._landing_request(
            session,
            "POST",
            f"{LANDING_ENVIRONMENTS_PATH}/{urllib.parse.quote(env_id)}"
            f"/{urllib.parse.quote(partner_id)}",
        )

    async def _select_environment_during_login(self, session: aiohttp.ClientSession) -> None:
        """Načte nabídku prostředí a vybere předvolené (nebo výchozí)."""
        data = await self._landing_request(session, "GET", LANDING_ENVIRONMENTS_PATH)
        environments = (data or {}).get("environments", []) if isinstance(data, dict) else []
        options: list[dict[str, str]] = []
        selected_by_portal: dict[str, str] = {}
        for env in environments:
            env_id = env.get("environmentId")
            if not env_id:
                continue
            if env.get("selectedPartnerId"):
                selected_by_portal[env_id] = str(env["selectedPartnerId"])
            for partner in env.get("partners") or []:
                partner_id = partner.get("partnerId")
                if partner_id:
                    options.append(
                        {
                            "environment_id": env_id,
                            "partner_id": str(partner_id),
                            "partner_name": partner.get("partnerName") or "",
                        }
                    )
        self.environment_options = options
        _LOGGER.debug(
            "Výběr prostředí: dostupná prostředí=%s, kombinací prostředí/partner=%d.",
            sorted({o["environment_id"] for o in options}),
            len(options),
        )
        if not options:
            raise CezUnexpectedLoginPageError(
                "Portál vyžaduje výběr prostředí, ale nenabídl žádného partnera."
            )

        choice = self._choose_environment(options, selected_by_portal)
        env_id, partner_id = choice["environment_id"], choice["partner_id"]
        _LOGGER.debug("Vybírám prostředí portálu %s.", env_id)
        await self._post_select_environment(session, env_id, partner_id)
        self._portal_environment = (env_id, partner_id)

    def _choose_environment(
        self, options: list[dict[str, str]], selected_by_portal: dict[str, str]
    ) -> dict[str, str]:
        """Předvolba z config entry, jinak prostředí dle _ENVIRONMENT_PRIORITY."""
        if self._portal_environment:
            env_id, partner_id = self._portal_environment
            for option in options:
                if option["environment_id"] == env_id and option["partner_id"] == partner_id:
                    return option
            _LOGGER.warning(
                "Uložené prostředí portálu %s už není dostupné, vybírám výchozí.", env_id
            )
            self.preferred_environment_missing = True

        def rank(option: dict[str, str]) -> tuple[int, int]:
            env_id = option["environment_id"]
            env_rank = (
                _ENVIRONMENT_PRIORITY.index(env_id)
                if env_id in _ENVIRONMENT_PRIORITY
                else len(_ENVIRONMENT_PRIORITY)
            )
            # Partner, kterého má portál u prostředí předvybraného, má přednost.
            partner_rank = 0 if selected_by_portal.get(env_id) == option["partner_id"] else 1
            return env_rank, partner_rank

        return min(options, key=rank)

    @property
    def portal_environment(self) -> tuple[str, str] | None:
        """Aktuálně vybrané (environmentId, partnerId), pokud portál výběr vyžaduje."""
        return self._portal_environment

    async def select_environment(self, environment_id: str, partner_id: str) -> None:
        """Přepne prostředí portálu v rámci existující session (config flow)."""
        if (environment_id, partner_id) == self._portal_environment:
            return
        async with aiohttp.ClientSession(
            cookie_jar=self._auth_cookies, timeout=REQUEST_TIMEOUT
        ) as session:
            await self._post_select_environment(session, environment_id, partner_id)
        self._portal_environment = (environment_id, partner_id)
        self.preferred_environment_missing = False

    async def find_environment_for_supply_point(
        self, uid: str, ean: str
    ) -> tuple[str, str] | None:
        """Najde prostředí portálu, ve kterém je vidět dané odběrné místo.

        Začne aktuálně vybraným prostředím (nejčastější případ, nic se
        nepřepíná). Pokud se nenajde nikde, vrátí None a klient zůstane
        v původně vybraném prostředí.
        """
        original = self._portal_environment
        candidates = [
            (o["environment_id"], o["partner_id"]) for o in self.environment_options
        ]
        if original in candidates:
            candidates.remove(original)
            candidates.insert(0, original)
        for environment in candidates:
            try:
                await self.select_environment(*environment)
                points = extract_supply_points(await self.get_supply_points())
            except CezApiError as err:
                _LOGGER.debug("Prostředí %s nejde prohledat: %s", environment[0], err)
                continue
            if any(
                (uid and p.get("uid") == uid) or (ean and p.get("ean") == ean)
                for p in points
            ):
                _LOGGER.debug("Odběrné místo nalezeno v prostředí %s.", environment[0])
                return environment
        if original and original != self._portal_environment:
            await self.select_environment(*original)
        return None

    # ------------------------------------------------------------------
    # Přihlášení (MEPAS/AWS Gateway - appka Proud, hodinová data)
    # ------------------------------------------------------------------

    async def login_mepas(self) -> None:
        """Získá OIDC accessToken pro AWS Gateway (/pnd/data, /pnd/status).

        Na rozdíl od portálového login() NEJDE o znovupoužití stejné CAS
        session - appka Proud dělá pro MEPAS úplně samostatné, plnohodnotné
        přihlášení vlastním OAuth2/PKCE tokem:

          1) GET /cas/oidc/authorize (bez cookies) -> redirect na /cas/login
          2) GET/POST /cas/login (standardní CAS login formulář)
          3) redirect s 'ticket' -> .../oauth2.0/callbackAuthorize
          4) redirect zpět na /cas/oidc/authorize (teď už s TGC cookie)
          5) redirect na cda://validation/?code=...
          6) výměna code za accessToken na /cas/oidc/accessToken

        Ověřeno zachyceným provozem reálné appky (mitmproxy) - nejde o
        odhad z dekompilace, endpoint i pořadí kroků jsou potvrzené.
        """
        _LOGGER.debug("Přihlašuji se do MEPAS (AWS Gateway)...")

        verifier, challenge = _make_pkce_pair()
        state = secrets.token_hex(32)

        authorize_url = (
            f"{CAS_BASE_URL}/oidc/authorize"
            f"?client_id={MEPAS_CLIENT_ID}"
            f"&response_type=code"
            f"&state={state}"
            f"&code_challenge_method=S256"
            f"&code_challenge={challenge}"
            f"&redirect_uri={urllib.parse.quote(MEPAS_LOGIN_REDIRECT_URI, safe='')}"
            f"&scope={MEPAS_SCOPE}"
        )

        async with aiohttp.ClientSession(
            cookie_jar=aiohttp.CookieJar(), timeout=REQUEST_TIMEOUT
        ) as s:
            async with s.get(authorize_url, allow_redirects=False) as resp:
                if resp.status not in (301, 302, 303, 307, 308):
                    raise CezAuthError(
                        f"MEPAS authorize neočekávaný HTTP {resp.status}"
                    )
                login_url = resp.headers.get("Location", "")

            if "/cas/login" not in login_url:
                raise CezAuthError(f"MEPAS: neočekávaný redirect na {login_url!r}")

            async with s.get(login_url) as resp:
                html = await resp.text()
            soup = BeautifulSoup(html, "html.parser")
            execution_input = soup.find("input", {"name": "execution"})
            if not execution_input:
                raise CezAuthError("MEPAS login: execution token nenalezen.")
            execution = execution_input.get("value", "")

            async with s.post(
                login_url,
                data={
                    "username": self._username,
                    "password": self._password,
                    "execution": execution,
                    "_eventId": "submit",
                    "geolocation": "",
                },
                allow_redirects=False,
            ) as resp:
                if resp.status not in (301, 302, 303, 307, 308):
                    text = await resp.text()
                    if not _is_login_form(text):
                        _log_unexpected_page("MEPAS login POST", str(resp.url), resp.status, text)
                    raise CezAuthError(
                        f"MEPAS přihlášení selhalo, HTTP {resp.status}: "
                        f"{text[:200]!r}"
                    )
                next_url = resp.headers.get("Location", "")

            # Následuj řetěz přesměrování (ticket -> zpět na oidc/authorize),
            # dokud nedostaneme přesměrování na cda://validation/?code=...
            for _ in range(5):
                if next_url.startswith("cda://"):
                    break
                async with s.get(next_url, allow_redirects=False) as resp:
                    if resp.status not in (301, 302, 303, 307, 308):
                        text = await resp.text()
                        raise CezAuthError(
                            f"MEPAS: neočekávaný krok, HTTP {resp.status}: "
                            f"{text[:200]!r}"
                        )
                    next_url = resp.headers.get("Location", "")

            if not next_url.startswith("cda://"):
                raise CezAuthError(
                    f"MEPAS: nedošli jsme k přesměrování s kódem. "
                    f"Poslední URL: {next_url!r}"
                )

            parsed = urllib.parse.urlparse(next_url)
            query = urllib.parse.parse_qs(parsed.query)
            code = query.get("code", [None])[0]
            if not code:
                raise CezAuthError(
                    f"V přesměrování nebyl nalezen 'code'. Location: {next_url!r}"
                )

            body = urllib.parse.urlencode(
                {
                    "scope": MEPAS_SCOPE,
                    "client_id": MEPAS_CLIENT_ID,
                    "grant_type": "authorization_code",
                    "code": code,
                    "code_verifier": verifier,
                    "redirect_uri": MEPAS_LOGIN_REDIRECT_URI,
                }
            )
            async with s.post(
                MEPAS_TOKEN_URL,
                data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ) as resp:
                data = await self._read_json_response(resp, MEPAS_TOKEN_URL)

        self._mepas_access_token = (
            data.get("accessToken")
            or data.get("access_token")
            or (data.get("data") or {}).get("accessToken")
        )
        if not self._mepas_access_token:
            raise CezAuthError(f"MEPAS token exchange nevrátil accessToken: {data}")

        _LOGGER.debug("MEPAS přihlášení OK.")

    def _mepas_headers(self, *, is_post: bool = False) -> dict[str, str]:
        headers = {
            "authorization": self._mepas_access_token or "",
            "appversion": APP_VERSION,
            "buildnumber": APP_BUILD_NUMBER,
            "installationid": self._mepas_installation_id,
        }
        if is_post:
            # Appka posílá u POST/PUT navíc apicalluid (viz dekompilovaný
            # interceptor - generateUUIDwithoutHash(32)); server to podle
            # všeho jen loguje pro trasování, ale posíláme to shodně.
            headers["apicalluid"] = "".join(
                secrets.choice(
                    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
                )
                for _ in range(32)
            )
            headers["Content-Type"] = "application/json"
        return headers

    async def _mepas_request(
        self, method: str, path: str, json_body: dict | None = None
    ) -> Any:
        """GET/POST na AWS Gateway s automatickým obnovením MEPAS tokenu."""
        url = f"{AWS_API_GATEWAY_STATIC_URL}/{path}"

        last_status: int | None = None
        last_body: str = ""

        for attempt in range(LOGIN_RETRIES):
            if not self._mepas_access_token:
                await self.login_mepas()

            headers = self._mepas_headers(is_post=(method == "POST"))
            async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as s:
                if method == "GET":
                    async with s.get(url, headers=headers) as resp:
                        text = await resp.text()
                        status = resp.status
                else:
                    async with s.post(url, headers=headers, json=json_body or {}) as resp:
                        text = await resp.text()
                        status = resp.status

            if status in (401, 403):
                # DŮLEŽITÉ: uchováváme tělo odpovědi, protože 403 nemusí
                # znamenat jen expirovaný token - AWS Gateway/appka Proud ho
                # vrací i pro platný token, když daný partner/EAN vůbec
                # nemá povolený/aktivovaný dálkový odečet (AMM) - to se bez
                # zalogování těla odpovědi nedá odlišit od skutečně
                # expirovaného tokenu (viz issue #24).
                last_status, last_body = status, text
                _LOGGER.debug(
                    "MEPAS token zřejmě expiroval nebo přístup zamítnut (HTTP %s), "
                    "obnovuji... (pokus %d): %s",
                    status,
                    attempt + 1,
                    text[:300],
                )
                self._mepas_access_token = None
                continue

            if status >= 400:
                raise CezApiError(f"HTTP {status} pro {url}: {text[:300]}")

            try:
                return json.loads(text)
            except json.JSONDecodeError as err:
                raise CezApiError(
                    f"Neplatná JSON odpověď z {url}: {text[:200]!r}"
                ) from err

        detail = f" Poslední odpověď HTTP {last_status}: {last_body[:300]!r}" if last_status else ""
        raise CezApiError(
            f"Nepodařilo se získat data z: {url} (MEPAS auth selhává opakovaně).{detail}"
        )

    # ------------------------------------------------------------------
    # Interní GET / POST s retry a obnovou tokenu (portál)
    # ------------------------------------------------------------------

    async def _auth_get(self, path: str) -> Any:
        return await self._request_with_retry(authenticated=True, method="GET", path=path)

    async def _auth_post(self, path: str, json: dict | None = None) -> Any:
        return await self._request_with_retry(authenticated=True, method="POST", path=path, json=json)

    async def _anon_post(self, path: str, json: dict | None = None) -> Any:
        return await self._request_with_retry(authenticated=False, method="POST", path=path, json=json)

    async def _read_json_response(self, resp: aiohttp.ClientResponse, url: str) -> Any:
        """Načte JSON odpověď a převede nevalidní tělo na čitelnou API chybu."""
        text = await resp.text()
        _LOGGER.debug(
            "ČEZ API odpověď: endpoint=%s HTTP=%d content_type=%s body_chars=%d redirects=%d",
            _safe_endpoint(str(resp.url)),
            resp.status,
            resp.headers.get("Content-Type", "neznámý"),
            len(text),
            len(resp.history),
        )
        if resp.status >= 400:
            raise CezApiError(f"HTTP {resp.status} pro {url}: {text[:200] or 'prázdná odpověď'}")
        content_type = resp.headers.get("Content-Type", "neznámý")
        if not text.strip():
            self._log_invalid_json_response(resp, text)
            raise _InvalidJsonResponse(url, resp.status, content_type, "prázdná odpověď")
        try:
            return json.loads(text)
        except json.JSONDecodeError as err:
            preview = text[:200].replace("\n", " ")
            self._log_invalid_json_response(resp, text)
            raise _InvalidJsonResponse(url, resp.status, content_type, preview) from err

    @staticmethod
    def _log_invalid_json_response(resp: aiohttp.ClientResponse, text: str) -> None:
        """Zaloguje bezpečnou strukturu neočekávané odpovědi pouze na DEBUG."""
        diagnostics = _response_diagnostics(resp, len(text))
        if "text/html" in (resp.headers.get("Content-Type") or "").lower():
            page = _describe_page(str(resp.url), resp.status, text)
            for key in ("title", "headings", "_text_preview"):
                page.pop(key, None)
            for form in page["forms"]:
                form.pop("id", None)
                form.pop("action_path", None)
            diagnostics["html_structure"] = page
        _LOGGER.debug(
            "ČEZ API vrátil neplatnou JSON odpověď: %s",
            json.dumps(diagnostics, ensure_ascii=False),
        )

    async def _request_with_retry(
        self,
        authenticated: bool,
        method: str,
        path: str,
        json: dict | None = None,
    ) -> Any:
        url = f"{self._base_url}/{path}"
        for attempt in range(LOGIN_RETRIES):
            _LOGGER.debug(
                "ČEZ API požadavek: %s endpoint=%s pokus=%d/%d",
                method,
                _safe_endpoint(url),
                attempt + 1,
                LOGIN_RETRIES,
            )
            headers = {}
            if authenticated and self._api_token:
                headers["X-Request-Token"] = self._api_token
            elif not authenticated and self._anon_api_token:
                headers["X-Request-Token"] = self._anon_api_token

            cookies = self._auth_cookies if authenticated else self._anon_cookies

            try:
                async with aiohttp.ClientSession(cookie_jar=cookies, timeout=REQUEST_TIMEOUT) as s:
                    if method == "GET":
                        async with s.get(url, headers=headers) as resp:
                            raw = await self._read_json_response(resp, url)
                    else:
                        async with s.post(url, headers=headers, json=json or {}) as resp:
                            raw = await self._read_json_response(resp, url)
            except _InvalidJsonResponse as err:
                if authenticated and err.looks_like_portal_html and attempt < LOGIN_RETRIES - 1:
                    _LOGGER.debug(
                        "ČEZ vrátil HTML portál místo JSONu, obnovuji přihlášení... (pokus %d)",
                        attempt + 1,
                    )
                    await self.login()
                    continue
                raise

            # Zpracování odpovědi
            if isinstance(raw, dict) and "statusCode" in raw:
                status_code = raw["statusCode"]
                if status_code == 401:
                    _LOGGER.debug("Token expiroval, obnova... (pokus %d)", attempt + 1)
                    await self.login()
                    continue
                elif status_code == 200:
                    return raw.get("data", raw)
            else:
                return raw.get("data", raw) if isinstance(raw, dict) and "data" in raw else raw

        raise CezApiError(f"Nepodařilo se získat data z: {url}")

    # ------------------------------------------------------------------
    # Veřejné metody API
    # ------------------------------------------------------------------

    async def get_supply_points(self) -> dict:
        """Vrátí seznam odběrných míst."""
        return await self._auth_post(
            "vyhledani-om?path=/vyhledaniom/zakladniInfo/50/PREHLED_OM_CELEK",
            json={"nekontrolovatPrislusnostOM": False},
        )

    async def get_supply_point_detail(self, uid: str) -> dict:
        """Vrátí detail odběrného místa."""
        return await self._auth_get(f"prehled-om?path=supply-point-detail/{uid}")

    async def get_readings(self, uid: str) -> dict:
        """Vrátí historii odečtů (VT, NT)."""
        return await self._auth_post(
            f"prehled-om?path=supply-point-detail/meter-reading-history/{uid}/false",
            json={},
        )

    async def get_signals(self, ean: str) -> dict:
        """Vrátí HDO signály (stav VT/NT, časy spínání)."""
        return await self._auth_get(f"prehled-om?path=supply-point-detail/signals/{ean}")

    async def get_outages(self, ean: str) -> dict:
        """Vrátí plánované odstávky pro daný EAN."""
        return await self._anon_post(
            "anonymous/vyhledani-odstavek?path=shutdown-search",
            json={"eans": [ean]},
        )

    # ------------------------------------------------------------------
    # Veřejné metody API (MEPAS/AWS Gateway - hodinová/15min spotřeba)
    # ------------------------------------------------------------------

    async def get_pnd_status(self, partner: str) -> Any:
        """Seznam dostupných measurement/assembly kódů pro partnera."""
        return await self._mepas_request("GET", f"pnd/status/{partner}")

    async def get_pnd_data(
        self,
        partner: str,
        ean: str,
        date_from: str,
        date_to: str,
        assembly_code: str,
    ) -> Any:
        """Časová řada spotřeby/výkonu z /pnd/data/{partner}.

        date_from/date_to jako ISO8601 s 'Z' (např. '2026-08-01T00:00:00.000Z').
        Rozsah date_to - date_from nesmí přesáhnout MAX_PND_INTERVAL_DAYS,
        jinak ČEZ vrátí HTTP 400.
        """
        return await self._mepas_request(
            "POST",
            f"pnd/data/{partner}",
            json_body={
                "assemblyCode": assembly_code,
                "ean": ean,
                "from": date_from,
                "to": date_to,
            },
        )
