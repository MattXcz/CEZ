#!/usr/bin/env python3
"""Diagnostický skript – vypíše syrová data z ČEZ Distribuce API.

Slouží k dohledání přesných názvů polí pro dodávku (přetok) aktivní energie
zpět do sítě (např. u FVE) v odpovědi endpointu meter-reading-history.
Aktuálně integrace zná jen pole `stavVt` / `stavNt` (odběr). Pokud máte
odběrné místo s přetokem (fotovoltaika), spusťte tento skript a přiložte
výstup (JSON) do GitHub issue nebo PR – ideálně anonymizovaný o citlivé
údaje (adresa, jméno apod.), přihlašovací údaje se nikde nevypisují.

Použití:
    pip install aiohttp beautifulsoup4
    CEZ_USER='tvuj-login' CEZ_PASS='tvoje-heslo' python3 scripts/dump_readings.py

Volitelně lze omezit na konkrétní odběrné místo:
    CEZ_EAN='859182400XXXXXXXXX' CEZ_USER=... CEZ_PASS=... python3 scripts/dump_readings.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path

import aiohttp

# Načteme api.py přímo ze souboru, ne přes balíček custom_components.cez –
# ten při importu spouští __init__.py, který vyžaduje nainstalovaný
# Home Assistant. api.py sám na Home Assistantu nezávisí (jen aiohttp/bs4).
#
# api.py od verze s hodinovou spotřebou navíc importuje const.py (MEPAS
# konstanty) - když selže relativní "from .const import" (nejsme balíček),
# spadne na bare "from const import", což vyžaduje mít složku cez/ na
# sys.path.
_CEZ_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "cez"
if str(_CEZ_DIR) not in sys.path:
    sys.path.insert(0, str(_CEZ_DIR))

_API_PATH = _CEZ_DIR / "api.py"
_spec = importlib.util.spec_from_file_location("cez_api", _API_PATH)
_cez_api = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_cez_api)
CezDistribuceApiClient = _cez_api.CezDistribuceApiClient


def _print_section(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def _dump(data: object) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


async def main() -> int:
    username = os.environ.get("CEZ_USER")
    password = os.environ.get("CEZ_PASS")
    wanted_ean = os.environ.get("CEZ_EAN")

    if not username or not password:
        print("Nastav proměnné CEZ_USER a CEZ_PASS.", file=sys.stderr)
        return 2

    async with aiohttp.ClientSession() as session:
        client = CezDistribuceApiClient(username=username, password=password, session=session)

        _print_section("Přihlašuji se…")
        await client.login()
        print("✅ Přihlášení OK")

        _print_section("Odběrná místa (get_supply_points)")
        supply_points = await client.get_supply_points()
        _dump(supply_points)

        points = _extract_points(supply_points)
        if not points:
            print("\n⚠️  Nepodařilo se najít odběrná místa ve struktuře výše "
                  "– zkopíruj prosím ručně UID/EAN a uprav skript.")
            return 1

        if wanted_ean:
            points = [p for p in points if p.get("ean") == wanted_ean] or points

        for point in points:
            ean = point.get("ean")
            uid = point.get("uid")
            if not ean or not uid:
                continue

            _print_section(f"Detail odběrného místa {ean} (get_supply_point_detail)")
            detail = await client.get_supply_point_detail(uid)
            _dump(detail)

            _print_section(f"Historie odečtů {ean} (get_readings) – TOTO je klíčové pro dodávku/přetok")
            readings = await client.get_readings(uid)
            _dump(readings)

            _print_section(f"HDO signály {ean} (get_signals)")
            signals = await client.get_signals(ean)
            _dump(signals)

    print("\n✅ Hotovo. Zkontroluj sekci 'get_readings' výše – hledáme pole "
          "vedle stavVt/stavNt, která reprezentují dodávku/přetok (např. "
          "stavVt2, stavNt2, dodavkaVt, dodavkaNt, smerToku apod.).")
    return 0


def _extract_points(raw: object) -> list[dict]:
    """Zkusí z různých možných tvarů odpovědi vytáhnout seznam {ean, uid}."""
    candidates: list[dict] = []

    def _walk(node: object) -> None:
        if isinstance(node, dict):
            ean = node.get("ean") or node.get("EAN")
            uid = node.get("uid") or node.get("UID") or node.get("id")
            if ean and uid:
                candidates.append({"ean": ean, "uid": uid})
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(raw)
    return candidates


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
