"""Async REST klient pro ČEZ Distribuce."""
from __future__ import annotations

import logging
import urllib.parse
from typing import Any

import aiohttp
from bs4 import BeautifulSoup

_LOGGER = logging.getLogger(__name__)

CAS_BASE_URL = "https://cas.cez.cz/cas"
CLIENT_NAME = "CasOAuthClient"
RESPONSE_TYPE = "code"
SCOPE = "openid"

CEZ_DISTRIBUCE_CLIENT_ID = "fjR3ZL9zrtsNcDQF.onpremise.dip.sap.dipcezdistribucecz.prod"
CEZ_DISTRIBUCE_BASE_URL = "https://dip.cezdistribuce.cz/irj/portal"

LOGIN_RETRIES = 2


class CezAuthError(Exception):
    """Chyba přihlášení."""


class CezApiError(Exception):
    """Obecná chyba API."""


class CezDistribuceApiClient:
    """Async klient pro ČEZ Distribuce REST API."""

    def __init__(
        self,
        username: str,
        password: str,
        session: aiohttp.ClientSession,
        base_url: str = CEZ_DISTRIBUCE_BASE_URL,
        client_id: str = CEZ_DISTRIBUCE_CLIENT_ID,
        browser_auth: str | None = None,
    ) -> None:
        self._username = username
        self._password = password
        self._base_url = base_url
        self._client_id = client_id
        self._session = session
        self._browser_auth = browser_auth

        redirect_url = base_url
        self._service_url = (
            f"{CAS_BASE_URL}/oauth2.0/callbackAuthorize"
            f"?client_id={client_id}"
            f"&redirect_uri={urllib.parse.quote(redirect_url)}"
            f"&response_type={RESPONSE_TYPE}"
            f"&client_name={CLIENT_NAME}"
        )
        self._login_url = f"{CAS_BASE_URL}/login?service={urllib.parse.quote(self._service_url)}"
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

    # ------------------------------------------------------------------
    # Přihlášení
    # ------------------------------------------------------------------

    async def login(self) -> None:
        """Přihlásí se přes CAS OAuth a načte tokeny."""
        _LOGGER.debug("Přihlašuji se do ČEZ Distribuce...")

        if self._browser_auth:
            await self._login_with_browser_auth(self._browser_auth)
            return

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

            # Krok 3 – GET authorize URL
            async with auth_session.get(self._authorize_url) as resp:
                _LOGGER.debug("Authorize response: %s", resp.status)

            # Krok 4 – načíst API token (autentizovaný)
            async with auth_session.get(f"{self._base_url}/rest-auth-api?path=/token/get") as resp:
                data = await resp.json(content_type=None)
                self._api_token = data if isinstance(data, str) else data.get("data") or data.get("token")

            # Uložit cookies pro pozdější použití
            self._auth_cookies = auth_session.cookie_jar

        # Krok 5 – anonymní token (nový session bez přihlášení)
        async with aiohttp.ClientSession() as anon_session:
            async with anon_session.get(f"{self._base_url}/anonymous/rest-auth-api?path=/token/get") as resp:
                data = await resp.json(content_type=None)
                self._anon_api_token = data if isinstance(data, str) else data.get("data") or data.get("token")
            self._anon_cookies = anon_session.cookie_jar

        _LOGGER.debug("Přihlášení OK, tokeny načteny.")

    async def _login_with_browser_auth(self, code_or_url: str) -> None:
        """Přihlásí se pomocí browser callback hodnoty (WSO2/CAS).

        Podporuje vložení:
        - samotného `code`/`ticket`, nebo
        - celé callback URL s query parametrem `code` (WSO2) nebo `ticket` (CAS).
        """
        auth_param, auth_value = self._extract_browser_auth_value(code_or_url)
        if not auth_value:
            raise CezAuthError("V callback URL nebyl nalezen parametr code ani ticket.")

        connector = aiohttp.TCPConnector()
        async with aiohttp.ClientSession(
            connector=connector,
            max_line_size=8190 * 4,
            max_field_size=8190 * 4,
        ) as auth_session:
            auth_session._cookie_jar = self._auth_cookie_jar  # noqa: SLF001

            callback_url = f"{self._service_url}&{auth_param}={urllib.parse.quote(auth_value)}"
            async with auth_session.get(callback_url, allow_redirects=True) as resp:
                _LOGGER.debug("Browser callback response: %s", resp.status)

            async with auth_session.get(f"{self._base_url}/rest-auth-api?path=/token/get") as resp:
                data = await resp.json(content_type=None)
                self._api_token = data if isinstance(data, str) else data.get("data") or data.get("token")

            self._auth_cookies = auth_session.cookie_jar

        async with aiohttp.ClientSession() as anon_session:
            async with anon_session.get(f"{self._base_url}/anonymous/rest-auth-api?path=/token/get") as resp:
                data = await resp.json(content_type=None)
                self._anon_api_token = data if isinstance(data, str) else data.get("data") or data.get("token")
            self._anon_cookies = anon_session.cookie_jar

        if not self._api_token:
            raise CezAuthError("Browser callback code/ticket je neplatný nebo expirovaný.")

        _LOGGER.debug("Přihlášení přes browser callback code/ticket OK, tokeny načteny.")

    @staticmethod
    def _extract_browser_auth_value(code_or_url: str) -> tuple[str, str]:
        """Vrátí auth parametr a hodnotu ze vstupu (code/ticket nebo URL)."""
        raw = code_or_url.strip()
        if not raw:
            return "", ""

        if "code=" not in raw and "ticket=" not in raw:
            return ("code", raw)

        parsed = urllib.parse.urlparse(raw)
        query = urllib.parse.parse_qs(parsed.query)
        code = query.get("code", [""])[0]
        if code:
            return ("code", code)
        ticket = query.get("ticket", [""])[0]
        if ticket:
            return ("ticket", ticket)
        return ("", "")

    # ------------------------------------------------------------------
    # Interní GET / POST s retry a obnovou tokenu
    # ------------------------------------------------------------------

    async def _auth_get(self, path: str) -> Any:
        return await self._request_with_retry(authenticated=True, method="GET", path=path)

    async def _auth_post(self, path: str, json: dict | None = None) -> Any:
        return await self._request_with_retry(authenticated=True, method="POST", path=path, json=json)

    async def _anon_post(self, path: str, json: dict | None = None) -> Any:
        return await self._request_with_retry(authenticated=False, method="POST", path=path, json=json)

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

            async with aiohttp.ClientSession(cookie_jar=cookies) as s:
                if method == "GET":
                    async with s.get(url, headers=headers) as resp:
                        raw = await resp.json(content_type=None)
                else:
                    async with s.post(url, headers=headers, json=json or {}) as resp:
                        raw = await resp.json(content_type=None)

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
