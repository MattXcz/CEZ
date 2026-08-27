#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Standalone testovací nástroj pro hodinovou/15min spotřebu (appka Proud,
MEPAS/AWS Gateway).

DŮLEŽITÉ: tenhle skript NEIMPLEMENTUJE vlastní logiku - importuje přímo
api.py, pnd_processing.py a const.py z custom_components/cez/, TEDY
STEJNÉ SOUBORY, JAKÉ BĚŽÍ V HA PLUGINU. Cokoliv tady otestuješ, je
doslova stejný kód jako v pluginu - žádná paralelní, časem rozjetá
implementace.

Co skript NEDĚLÁ (na rozdíl od pluginu):
  - nezapisuje nic do HA databáze / dlouhodobých statistik
  - nepočítá běžící součet (ten vyžaduje přístup do HA recorderu,
    mimo HA nedává smysl) - místo toho jen ukáže hodinové přírůstky

Použití:
  pip install aiohttp beautifulsoup4

  # Základní běh - přihlásí se, ukáže odběrná místa, uloží JSON+CSV
  CEZ_USER='login' CEZ_PASS='heslo' python3 scripts/test_pnd_consumption.py

  # Konkrétní odběrné místo a assemblyCode
  python3 scripts/test_pnd_consumption.py --ean 859182400708532693 --assembly 03

  # Ruční zadání období (ISO8601, UTC)
  python3 scripts/test_pnd_consumption.py --from 2026-08-01T00:00:00 --to 2026-08-10T00:00:00

  # Simulace toho, co by plugin natáhl PŘÍŠTĚ, kdyby poslední uložená
  # hodina v HA byla tahle - přesně nahrazuje "co má coordinator.py
  # v get_last_statistics", bez nutnosti mít běžící HA
  python3 scripts/test_pnd_consumption.py --simulate-last-known 2026-08-19T01:00:00

Přihlašovací údaje: --username/--password, nebo proměnné prostředí
CEZ_USER/CEZ_PASS (shodně s dump_readings.py), nebo interaktivní dotaz.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import getpass
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp

# Stejné soubory jako v HA pluginu - viz docstring výše. api.py interně
# importuje const.py relativně (jako HA balíček) a při selhání spadne na
# bare "from const import", proto přidáváme custom_components/cez na
# sys.path stejně jako dump_readings.py.
_CEZ_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "cez"
if str(_CEZ_DIR) not in sys.path:
    sys.path.insert(0, str(_CEZ_DIR))

from api import CezApiError, CezAuthError, CezDistribuceApiClient  # noqa: E402
from const import (  # noqa: E402
    MAX_PND_INTERVAL_DAYS,
    PND_ASSEMBLY_15MIN,
    PND_ASSEMBLY_HOURLY,
    PND_INTERVAL_MINUTES,
    PND_TRAILING_SAFETY_DAYS,
)
from pnd_processing import fetch_pnd_chunked, process_pnd_response  # noqa: E402


def _parse_iso(value: str) -> datetime:
    """Přijímá ISO8601 s nebo bez 'Z'/offsetu, vrací tz-aware UTC datetime."""
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def dump_raw(label: str, data) -> None:
    print(f"\n===== RAW: {label} =====")
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def show_supply_points(data) -> list[dict]:
    points = []
    if isinstance(data, dict):
        blocks = data.get("vstelleBlocks", {}).get("blocks", [])
        for block in blocks:
            points.extend(block.get("vstelles", []))

    print(f"\nOdběrná místa nalezena: {len(points)}")
    for i, p in enumerate(points, 1):
        print(f"   [{i}] EAN: {p.get('ean', '?')}  UID: {p.get('uid', '?')}  Partner: {p.get('partner', '?')}")
    return points


async def resolve_supply_point(client: CezDistribuceApiClient, ean_filter: str | None) -> dict:
    supply_data = await client.get_supply_points()
    points = show_supply_points(supply_data)
    if not points:
        raise SystemExit("ČEZ nevrátil žádné odběrné místo.")

    if ean_filter:
        selected = next((p for p in points if p.get("ean") == ean_filter), None)
        if not selected:
            raise SystemExit(f"EAN {ean_filter} nebyl v seznamu nalezen.")
    else:
        selected = points[0]

    return selected


async def resolve_anlage(client: CezDistribuceApiClient, uid: str) -> str:
    detail = await client.get_supply_point_detail(uid)
    anlage = ""
    if isinstance(detail, dict):
        anlage = (detail.get("anlage_Dist") or {}).get("cislo") or detail.get("anlage", "")
    if not anlage:
        raise SystemExit("Nepodařilo se zjistit 'anlage' z detailu odběrného místa.")
    print(f"Anlage: {anlage}")
    return anlage


def write_csv(path: Path, hourly_buckets: dict[datetime, float]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["start_utc", "kwh"])
        for hour_start in sorted(hourly_buckets):
            writer.writerow([hour_start.isoformat(), f"{hourly_buckets[hour_start]:.4f}"])


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Test hodinové spotřeby ČEZ (appka Proud) - stejný kód jako HA plugin",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--username", default=os.getenv("CEZ_USER"))
    parser.add_argument(
        "--password",
        default=None,
        help="Heslo přímo na příkazové řádce (jinak CEZ_PASS env, nebo dotaz)",
    )
    parser.add_argument("--ean", help="EAN odběrného místa; jinak se vybere první")
    parser.add_argument(
        "--assembly",
        choices=[PND_ASSEMBLY_HOURLY, PND_ASSEMBLY_15MIN],
        default=None,
        help=f"assemblyCode pro pnd/data (výchozí z const.py: {PND_ASSEMBLY_HOURLY}=hodinová kWh, "
        f"{PND_ASSEMBLY_15MIN}=15min kW)",
    )

    time_group = parser.add_mutually_exclusive_group()
    time_group.add_argument(
        "--from",
        dest="date_from",
        help="Ruční počátek období (ISO8601, UTC), např. 2026-08-01T00:00:00. "
        "Nelze kombinovat s --simulate-last-known.",
    )
    time_group.add_argument(
        "--simulate-last-known",
        dest="simulate_last_known",
        help="ISO8601 čas poslední 'uložené' hodiny - simuluje přesně to, "
        "co by coordinator.py zjistil z get_last_statistics(), a spočítá "
        "od toho fetch_start stejným způsobem jako plugin (včetně "
        "PND_TRAILING_SAFETY_DAYS ohlédnutí zpátky). V tomhle režimu se "
        "--to IGNORUJE - reálný plugin se vždy dotazuje až do 'teď', "
        "stejně jako tady.",
    )

    parser.add_argument(
        "--to",
        dest="date_to",
        help="Ruční konec období (ISO8601, UTC); výchozí = teď. "
        "Ignorováno při použití --simulate-last-known.",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Vypsat kompletní JSON odpovědi na obrazovku (jinak jen do souboru)",
    )
    parser.add_argument(
        "--outdir",
        default=".",
        help="Kam uložit výstupní soubory (JSON, CSV). Výchozí: aktuální složka.",
    )
    args = parser.parse_args()

    username = args.username or input("Uživatelské jméno ČEZ: ").strip()
    password = args.password or os.getenv("CEZ_PASS") or getpass.getpass("Heslo ČEZ: ")

    assembly_code = args.assembly or PND_ASSEMBLY_HOURLY
    interval_minutes = PND_INTERVAL_MINUTES if assembly_code == PND_ASSEMBLY_HOURLY else 15

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    async with aiohttp.ClientSession() as throwaway_session:
        # 'session' parametr konstruktoru se u aktuálních metod api.py
        # fakticky nevyužívá (každá metoda si otvírá vlastní session),
        # ale konstruktor ho vyžaduje - viz api.py.
        client = CezDistribuceApiClient(username=username, password=password, session=throwaway_session)

        try:
            print("1. Přihlašuji se do ČEZ Distribuce (portál)...")
            await client.login()
            print("   OK")

            selected = await resolve_supply_point(client, args.ean)
            ean = selected.get("ean", "")
            uid = selected.get("uid", "")
            partner = selected.get("partner", "")
            print(f"\nVybráno: EAN={ean}  UID={uid}  Partner={partner}")

            await resolve_anlage(client, uid)  # jen ověření dostupnosti, zatím nevyužito u pnd/data

            print("\n2. Přihlašuji se do MEPAS (AWS Gateway)...")
            await client.login_mepas()
            print("   OK")

            now = datetime.now(timezone.utc)

            if args.simulate_last_known:
                last_known = _parse_iso(args.simulate_last_known)
                fetch_start = min(
                    last_known + timedelta(minutes=interval_minutes),
                    now - timedelta(days=PND_TRAILING_SAFETY_DAYS),
                )
                fetch_end = now
                if args.date_to:
                    print(
                        "   [pozn.] --to se v režimu --simulate-last-known ignoruje "
                        "(reálný plugin se vždy dotazuje až do 'teď')."
                    )
                print(
                    f"\n[simulace] last_known={last_known.isoformat()} -> "
                    f"fetch_start={fetch_start.isoformat()} "
                    f"(stejný výpočet jako coordinator.py, PND_TRAILING_SAFETY_DAYS={PND_TRAILING_SAFETY_DAYS})"
                )
            elif args.date_from:
                fetch_start = _parse_iso(args.date_from)
                fetch_end = _parse_iso(args.date_to) if args.date_to else now
            else:
                fetch_start = now - timedelta(days=2)
                fetch_end = _parse_iso(args.date_to) if args.date_to else now

            print(
                f"\n3. Stahuji pnd/data (assemblyCode={assembly_code}, "
                f"interval={interval_minutes}min): {fetch_start.isoformat()} .. {fetch_end.isoformat()}"
            )

            def _on_chunk_error(chunk_start, chunk_end, err):
                print(f"   [chyba] okno {chunk_start} .. {chunk_end} selhalo: {err}", file=sys.stderr)

            raw_points, unit = await fetch_pnd_chunked(
                client, partner, ean, fetch_start, fetch_end, assembly_code,
                MAX_PND_INTERVAL_DAYS, on_chunk_error=_on_chunk_error,
            )
            print(f"   Staženo syrových záznamů: {len(raw_points)}, jednotka: {unit}")

            timestamp_tag = now.strftime("%Y%m%dT%H%M%S")
            json_path = outdir / f"pnd_raw_{timestamp_tag}.json"
            with json_path.open("w", encoding="utf-8") as f:
                json.dump(raw_points, f, ensure_ascii=False, indent=2)
            print(f"   Syrová data uložena do: {json_path}")

            if args.raw:
                dump_raw("pnd_data (syrová)", raw_points)

            hourly_buckets, trimmed = process_pnd_response(
                raw_points, unit, interval_minutes, fetch_start, now
            )
            print("\n4. Zpracováno (stejná logika jako coordinator.py):")
            print(f"   Ořezáno nulových (zatím neúplných) záznamů z konce: {trimmed}")
            print(f"   Hotových hodinových bodů: {len(hourly_buckets)}")
            if hourly_buckets:
                oldest = min(hourly_buckets)
                newest = max(hourly_buckets)
                print(f"   Rozsah: {oldest.isoformat()} .. {newest.isoformat()}")
                print(f"   'last_pnd_timestamp' by byl: {(newest + timedelta(hours=1)).isoformat()}")
                total_kwh = sum(hourly_buckets.values())
                print(f"   Celková spotřeba v okně: {total_kwh:.3f} kWh")

            csv_path = outdir / f"pnd_hourly_{timestamp_tag}.csv"
            write_csv(csv_path, hourly_buckets)
            print(f"   Hodinová data (jak by šla do HA statistik) uložena do: {csv_path}")

        except (CezAuthError, CezApiError) as err:
            print(f"\nCHYBA: {type(err).__name__}: {err}", file=sys.stderr)
            return 1

    print("\n===== HOTOVO =====")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
