#!/usr/bin/env python3
"""Ruční test přihlašovacího toku ČEZ MEPAS (CAS OAuth/OIDC).

Používá jen standardní knihovnu, takže není potřeba nic instalovat.

Spuštění:
    CEZ_USER='tvuj-login' CEZ_PASS='tvoje-heslo' python3 test_mepas_login.py

Skript projde stejné kroky jako custom_components/cez/api.py a u každého
vypíše, co se stalo — ideální pro ověření, že host mepas.cez.cz a nový
client_id fungují.
"""
from __future__ import annotations

import http.cookiejar
import os
import re
import sys
import urllib.parse
import urllib.request

# --- Konfigurace (musí odpovídat api.py) ------------------------------------
CAS_BASE_URL = "https://mepas.cez.cz/cas"
CLIENT_NAME = "CasOAuthClient"
RESPONSE_TYPE = "code"
SCOPE = "openid"
CLIENT_ID = "emiCuDBbivwYxraX.dip.dip.ext.zak.prod.v1"
BASE_URL = "https://dip.cezdistribuce.cz/irj/portal"
REDIRECT_URL = BASE_URL

SERVICE_URL = (
    f"{CAS_BASE_URL}/oauth2.0/callbackAuthorize"
    f"?client_id={CLIENT_ID}"
    f"&redirect_uri={urllib.parse.quote(REDIRECT_URL)}"
    f"&response_type={RESPONSE_TYPE}"
    f"&client_name={CLIENT_NAME}"
    f"&scope={SCOPE}"
)
LOGIN_URL = f"{CAS_BASE_URL}/login?service={urllib.parse.quote(SERVICE_URL)}"
AUTHORIZE_URL = (
    # issue #24: druhé kolo OAuth toku - CAS vydá kód a přesměruje na SAP
    # portál, který si dokončí session. 404 na konci řetězu vrací portál
    # (kontrola prohlížeče v iView), ne CAS - detaily v api.py. Krok tu
    # musí zůstat, jinak /token/get vrátí portálové HTML místo JSON.
    f"{CAS_BASE_URL}/oidc/authorize"
    f"?scope={SCOPE}"
    f"&response_type={RESPONSE_TYPE}"
    f"&redirect_uri={urllib.parse.quote(REDIRECT_URL)}"
    f"&client_id={CLIENT_ID}"
)

# Přes CEZ_UA jde podstrčit jiný User-Agent (např. reálný Chrome) a ověřit,
# jestli na něm závisí chování SAP portálu (kontrola prohlížeče v iView).
UA = os.environ.get("CEZ_UA", "Mozilla/5.0 (test-mepas-login)")


def _banner(text: str) -> None:
    print(f"\n{'=' * 70}\n{text}\n{'=' * 70}")


_LAST_HEADERS: dict[str, str] = {}


def _request(opener, url: str, data: bytes | None = None) -> tuple[int, str, str]:
    """Vrátí (status, finalni_url, telo). data=None -> GET, jinak POST.

    Hlavičky poslední odpovědi (i chybové) ukládá do _LAST_HEADERS, aby
    šlo u neočekávaných stavů vypsat, kdo vlastně odpověděl.
    """
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA})
    try:
        with opener.open(req, timeout=30) as resp:
            _LAST_HEADERS.clear()
            _LAST_HEADERS.update(resp.headers.items())
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, resp.geturl(), body
    except urllib.error.HTTPError as err:
        _LAST_HEADERS.clear()
        _LAST_HEADERS.update(err.headers.items())
        body = err.read().decode("utf-8", errors="replace")
        # err.url je URL, na kterém chyba skutečně vznikla (po redirectech).
        # Dřív se tu vracelo původní `url`, takže 404 z konce řetězu
        # přesměrování vypadalo, jako by ho vrátil první server (issue #24).
        return err.code, err.url or url, body


def main() -> int:
    username = os.environ.get("CEZ_USER")
    password = os.environ.get("CEZ_PASS")
    if not username or not password:
        print("Nastav proměnné CEZ_USER a CEZ_PASS.", file=sys.stderr)
        print("Např.: CEZ_USER='login' CEZ_PASS='heslo' python3 test_mepas_login.py",
              file=sys.stderr)
        return 2

    cookie_jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cookie_jar)
    )

    # --- Krok 1: GET login stránky, vytáhnout execution token ----------------
    _banner("KROK 1 – GET login stránky")
    print("URL:", LOGIN_URL)
    status, final_url, html = _request(opener, LOGIN_URL)
    print("HTTP status:", status)
    print("Konečná URL:", final_url)

    if status == 403 or "není autorizovaná" in html:
        print("\n❌ 403 / aplikace není autorizovaná — špatný client_id nebo host.")
        print("Náhled odpovědi:\n", html[:400])
        return 1

    match = re.search(r'name=["\']execution["\']\s+value=["\']([^"\']+)["\']', html)
    if not match:
        # zkusit i opačné pořadí atributů (value před name)
        match = re.search(r'value=["\']([^"\']+)["\']\s+name=["\']execution["\']', html)
    if not match:
        print("\n❌ Nenašel jsem 'execution' token na stránce.")
        print("Náhled HTML:\n", html[:600])
        return 1

    execution = match.group(1)
    print("✅ execution token nalezen:", execution[:40], "...")

    # --- Krok 2: POST přihlašovacích údajů -----------------------------------
    _banner("KROK 2 – POST přihlašovacích údajů")
    form = urllib.parse.urlencode({
        "username": username,
        "password": password,
        "execution": execution,
        "_eventId": "submit",
        "geolocation": "",
    }).encode("utf-8")
    status, final_url, html = _request(opener, LOGIN_URL, data=form)
    print("HTTP status:", status)
    print("Konečná URL:", final_url)

    if "Nesprávné" in html or "incorrect" in html.lower() or "invalid" in html.lower():
        print("\n❌ Server hlásí nesprávné přihlašovací údaje.")
        print("Náhled odpovědi:\n", html[:400])
        return 1
    if status not in (200, 302):
        print(f"\n❌ Neočekávaný status {status}.")
        print("Náhled odpovědi:\n", html[:400])
        return 1
    print("✅ Přihlášení prošlo (server nevrátil chybu o špatných údajích).")
    print("Cookies po loginu:", [c.name for c in cookie_jar])

    # --- Krok 3: GET authorize (OIDC) ----------------------------------------
    _banner("KROK 3 – GET authorize (OIDC) – 404 je tu očekávané")
    print("URL:", AUTHORIZE_URL)
    status, final_url, html = _request(opener, AUTHORIZE_URL)
    print("HTTP status:", status)
    print("Konečná URL:", final_url)
    # issue #24: u ne-2xx/3xx vypisujeme hlavičky a tělo - právě tak se
    # ukázalo, že 404 vrací SAP portál (sap-isc-etag: J2EE/irj), ne CAS.
    if status >= 400:
        print("Hlavičky odpovědi:")
        for name, value in _LAST_HEADERS.items():
            if name.lower() == "set-cookie":
                value = value.split("=", 1)[0] + "=<skryto>"
            print(f"   {name}: {value[:160]}")
        print("Tělo odpovědi (náhled):")
        print(html[:1200] if html.strip() else "   <prázdné>")

    # --- Krok 4: GET API token -----------------------------------------------
    _banner("KROK 4 – GET API token")
    token_url = f"{BASE_URL}/rest-auth-api?path=/token/get"
    print("URL:", token_url)
    status, final_url, body = _request(opener, token_url)
    print("HTTP status:", status)
    print("Konečná URL:", final_url)
    print("Odpověď (náhled):", body[:300])

    if status == 200 and body.strip() and not body.lstrip().startswith("<"):
        print("\n✅ HOTOVO – vypadá to, že token endpoint vrátil data. Přihlášení funguje.")
        return 0

    print("\n⚠️  Token endpoint nevrátil čistá data (možná HTML portál nebo prázdná odpověď).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
