"""Async REST klient pro ČEZ Distribuce."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
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
CLIENT_NAME = "CasOAuthClient"
RESPONSE_TYPE = "code"
SCOPE = "openid"

# client_id převzat z tlačítka přihlášení na https://dip.cezdistribuce.cz/irj/portal
CEZ_DISTRIBUCE_CLIENT_ID = "emiCuDBbivwYxraX.dip.dip.ext.zak.prod.v1"
CEZ_DISTRIBUCE_BASE_URL = "https://dip.cezdistribuce.cz/irj/portal"

LOGIN_RETRIES = 2


class CezAuthError(Exception):
    """Chyba přihlášení."""


class CezApiError(Exception):
    """Obecná chyba API."""


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
    ) -> None:
        self._username = username
        self._password = password
        self._base_url = base_url
        self._client_id = client_id
        self._session = session

        redirect_url = base_url
        self._service_url = (
            f"{CAS_BASE_URL}/oauth2.0/callbackAuthorize"
            f"?client_id={client_id}"
            f"&redirect_uri={urllib.parse.quote(redirect_url)}"
            f"&response_type={RESPONSE_TYPE}"
            f"&client_name={CLIENT_NAME}"
            f"&scope={SCOPE}"
        )
        self._login_url = f"{CAS_BASE_URL}/login?service={urllib.parse.quote(self._service_url)}"
        # POZOR (issue #24): tenhle request vypadá zbytečně - trvale vrací
        # HTTP 404 a odpověď se nikde nečte - ale ODSTRANIT SE NESMÍ, viz
        # komentář u kroku 3 níž.
        # K tomu 404: není to změna na straně ČEZ ani špatná cesta. CAS
        # discovery na mepas.cez.cz uvádí authorization_endpoint =
        # /cas/oidc/oidcAuthorize a nepřihlášeně oba tvary (i tenhle
        # /oidc/authorize) vracejí shodně 302 na /cas/login. 404 přijde až
        # se založenou SSO session, protože portálový client_id je u CAS
        # registrovaný jako OAuth2.0 klient (client_name=CasOAuthClient,
        # service míří na /oauth2.0/callbackAuthorize), ne jako OIDC
        # relying party - OIDC authorize ho tedy nenajde. Funkční
        # login_mepas() níž na to nenarazí, používá OIDC/PKCE klienta
        # MEPAS_CLIENT_ID.
        self._authorize_url = (
            f"{CAS_BASE_URL}/oidc/authorize"
            f"?scope={SCOPE}"
            f"&response_type={RESPONSE_TYPE}"
            f"&redirect_uri={urllib.parse.quote(redirect_url)}"
            f"&client_id={client_id}"
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
        """Přihlásí se přes CAS OAuth a načte tokeny."""
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
        ) as auth_session:
            auth_session._cookie_jar = self._auth_cookie_jar  # noqa: SLF001

            # Krok 1 – GET login stránky, vytáhnout execution token
            async with auth_session.get(self._login_url) as resp:
                html = await resp.text()

            soup = BeautifulSoup(html, "html.parser")
            execution_input = soup.find("input", {"name": "execution"})
            if not execution_input:
                raise CezAuthError("Nepodařilo se najít execution token na přihlašovací stránce.")
            execution = execution_input.get("value", "")

            # Krok 2 – POST přihlašovacích údajů
            async with auth_session.post(
                self._login_url,
                data={
                    "username": self._username,
                    "password": self._password,
                    "execution": execution,
                    "_eventId": "submit",
                    "geolocation": "",
                },
            ) as resp:
                if resp.status not in (200, 302):
                    raise CezAuthError(f"Přihlášení selhalo, HTTP {resp.status}")
                html = await resp.text()
                if "Nesprávné" in html or "incorrect" in html.lower():
                    raise CezAuthError("Nesprávné přihlašovací údaje.")

            # Krok 3 – GET authorize URL. Vrací 404 a odpověď se nikde
            # nevyužívá (viz komentář u self._authorize_url), ale krok je
            # NUTNÝ: když se vypustil, /token/get níž začal vracet SAP
            # portálové HTML místo JSON (ověřeno živě 10.9.2026, oba
            # běhy diagnostického skriptu se lišily jen tímhle krokem).
            # Vypadá to, že SAP portál potřebuje po CAS callbacku chvilku
            # na dokončení session a tenhle request slouží jako
            # (nezáměrná) prodleva - proto ho neodstraňuj bez toho, že
            # místo něj přijde explicitní retry na /token/get.
            async with auth_session.get(self._authorize_url) as resp:
                _LOGGER.debug("Authorize response: %s", resp.status)

            # Krok 4 – načíst API token (autentizovaný)
            token_url = f"{self._base_url}/rest-auth-api?path=/token/get"
            async with auth_session.get(token_url) as resp:
                data = await self._read_json_response(resp, token_url)
                self._api_token = data if isinstance(data, str) else data.get("data") or data.get("token")

            # Uložit cookies pro pozdější použití
            self._auth_cookies = auth_session.cookie_jar

        # Krok 5 – anonymní token (nový session bez přihlášení)
        async with aiohttp.ClientSession() as anon_session:
            token_url = f"{self._base_url}/anonymous/rest-auth-api?path=/token/get"
            async with anon_session.get(token_url) as resp:
                data = await self._read_json_response(resp, token_url)
                self._anon_api_token = data if isinstance(data, str) else data.get("data") or data.get("token")
            self._anon_cookies = anon_session.cookie_jar

        _LOGGER.debug("Přihlášení OK, tokeny načteny.")

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

        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar()) as s:
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
            async with aiohttp.ClientSession() as s:
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
        if resp.status >= 400:
            raise CezApiError(f"HTTP {resp.status} pro {url}: {text[:200] or 'prázdná odpověď'}")
        content_type = resp.headers.get("Content-Type", "neznámý")
        if not text.strip():
            raise _InvalidJsonResponse(url, resp.status, content_type, "prázdná odpověď")
        try:
            return json.loads(text)
        except json.JSONDecodeError as err:
            preview = text[:200].replace("\n", " ")
            raise _InvalidJsonResponse(url, resp.status, content_type, preview) from err

    async def _request_with_retry(
        self,
        authenticated: bool,
        method: str,
        path: str,
        json: dict | None = None,
    ) -> Any:
        url = f"{self._base_url}/{path}"
        for attempt in range(LOGIN_RETRIES):
            headers = {}
            if authenticated and self._api_token:
                headers["X-Request-Token"] = self._api_token
            elif not authenticated and self._anon_api_token:
                headers["X-Request-Token"] = self._anon_api_token

            cookies = self._auth_cookies if authenticated else self._anon_cookies

            try:
                async with aiohttp.ClientSession(cookie_jar=cookies) as s:
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
