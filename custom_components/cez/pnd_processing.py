"""Čisté zpracování dat z pnd/data - BEZ závislosti na Home Assistantu.

Sdíleno mezi coordinator.py (plugin) a scripts/test_pnd_consumption.py
(standalone test) - existuje kvůli tomu, aby se tahle logika nikdy
neimplementovala na dvou místech zvlášť a časem se nerozjela do
nesouhlasu. Cokoliv, co testuješ přes ten skript, je doslova stejný kód,
jaký běží v HA pluginu.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any


def infer_interval_minutes(
    raw_points: list[dict[str, Any]], fallback: int
) -> int:
    """Odvodí skutečný krok dat (v minutách) z časových značek odpovědi.

    Nutné pro assemblyCode kódy, jejichž granularitu neznáme napevno
    (issue #24 - výrobní/dodávkové EANy nabízejí jiné kódy než spotřeba
    a jejich krok se dřív hádal podle PND_INTERVAL_MINUTES, což u
    assemblyCode=02 vyšlo 4x špatně: skutečný krok byl 15 minut, ne 60).
    Bere modus rozestupů mezi po sobě jdoucími "time" značkami - jeden
    chybějící/duplicitní bod modus nerozhodne. Když je k dispozici méně
    než dva body, vrací 'fallback' (nelze nic odvodit).
    """
    timestamps: list[datetime] = []
    for entry in raw_points:
        try:
            timestamps.append(
                datetime.strptime(entry["time"], "%Y-%m-%dT%H:%M:%S.%fZ")
            )
        except (KeyError, ValueError, TypeError):
            continue
    if len(timestamps) < 2:
        return fallback

    timestamps.sort()
    deltas = [
        round((b - a).total_seconds() / 60)
        for a, b in zip(timestamps, timestamps[1:])
    ]
    deltas = [d for d in deltas if d > 0]
    if not deltas:
        return fallback
    return Counter(deltas).most_common(1)[0][0]


def parse_pnd_entries(
    raw_points: list[dict[str, Any]],
    unit: str | None,
    interval_minutes: int,
    fetch_start: datetime,
) -> list[tuple[datetime, float]]:
    """Naparsuje syrové záznamy z pnd/data na (start_intervalu, kWh).

    'time' v odpovědi ČEZ je KONEC intervalu, ne začátek - odečítáme
    interval_minutes, aby "start" odpovídal skutečnému začátku.
    Přepočet kW -> kWh se dělá jen pokud API vrátilo výkon (unit == "kW"),
    ne když už vrátilo přímo energii (unit == "kWh").
    """
    needs_conversion = (unit or "").strip().lower() == "kw"
    interval_hours = interval_minutes / 60

    points: list[tuple[datetime, float]] = []
    for entry in raw_points:
        try:
            end_time = datetime.strptime(entry["time"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(
                tzinfo=timezone.utc
            )
            raw_value = float(entry["value"])
            kwh = raw_value * interval_hours if needs_conversion else raw_value
        except (KeyError, ValueError, TypeError):
            continue
        start_time = end_time - timedelta(minutes=interval_minutes)
        if start_time < fetch_start:
            continue
        points.append((start_time, kwh))

    points.sort(key=lambda p: p[0])
    return points


def trim_trailing_zero_run(
    points: list[tuple[datetime, float]]
) -> tuple[list[tuple[datetime, float]], int]:
    """Ořízne souvislý 'ocas' nul od NEJNOVĚJŠÍHO konce seznamu.

    ČEZ vrací pro ještě nezpracované intervaly placeholder s hodnotou
    přesně 0 - a může se to týkat víc než jen posledního bodu (klidně
    několika hodin zpátky). Jakmile při procházení od konce narazíme na
    první nenulovou hodnotu, všechno starší (i případné skutečné nuly,
    např. výpadek proudu) se bere jako hotové.

    NEfiltruje nuly uprostřed/na začátku seznamu - jen souvislý tail.
    Vrací (oříznutý seznam, počet odstraněných záznamů).
    """
    points = list(points)  # nemutovat vstup
    trimmed = 0
    while points and points[-1][1] == 0:
        points.pop()
        trimmed += 1
    return points, trimmed


def aggregate_to_hourly(
    points: list[tuple[datetime, float]],
    interval_minutes: int,
    now: datetime,
) -> dict[datetime, float]:
    """Agreguje (případně 15min) body na hodinové součty (kWh).

    Vynechává:
    - poslední (ještě probíhající) hodinu (hour_start + 1h > now) - mohla
      dorazit jen částečně,
    - neúplné hodiny u jemnějšího rozlišení - pokud (u 15min režimu) ČEZ
      doplní jen 3 ze 4 čtvrthodin, hodina se bere jako ještě nehotová,
      ne uměle nízká.
    """
    expected_points_per_hour = max(1, 60 // interval_minutes)

    hourly_sums: dict[datetime, float] = {}
    hourly_counts: dict[datetime, int] = {}
    for start_time, kwh in points:
        hour_start = start_time.replace(minute=0, second=0, microsecond=0)
        hourly_sums[hour_start] = hourly_sums.get(hour_start, 0.0) + kwh
        hourly_counts[hour_start] = hourly_counts.get(hour_start, 0) + 1

    return {
        hour_start: kwh
        for hour_start, kwh in hourly_sums.items()
        if hour_start + timedelta(hours=1) <= now
        and hourly_counts[hour_start] >= expected_points_per_hour
    }


def process_pnd_response(
    raw_points: list[dict[str, Any]],
    unit: str | None,
    interval_minutes: int,
    fetch_start: datetime,
    now: datetime,
) -> tuple[dict[datetime, float], int]:
    """Kompletní zpracování syrové odpovědi pnd/data na hodinové součty,
    připravené k importu do HA statistik (nebo k výpisu v testovacím
    skriptu).

    Vrací (hodinove_soucty, pocet_orezanych_nul)."""
    points = parse_pnd_entries(raw_points, unit, interval_minutes, fetch_start)
    points, trimmed = trim_trailing_zero_run(points)
    hourly_buckets = aggregate_to_hourly(points, interval_minutes, now)
    return hourly_buckets, trimmed


async def fetch_pnd_chunked(
    client: Any,
    partner: str,
    ean: str,
    start: datetime,
    end: datetime,
    assembly_code: str,
    max_interval_days: int,
    on_chunk_error: Any = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Stáhne /pnd/data v částech <= max_interval_days (tvrdý limit API -
    ověřeno: 366 dní projde, 367 ne).

    'client' je instance CezDistribuceApiClient (z api.py) - libovolná
    s metodou async get_pnd_data(partner, ean, date_from, date_to,
    assembly_code). Části se selháním (typicky moc stará data, ze kdy
    elektroměr ještě neexistoval) se přeskočí, volitelně přes
    on_chunk_error(chunk_start, chunk_end, chyba) zalogují/vypíšou.
    Vrací (hodnoty, jednotka)."""
    results: list[dict[str, Any]] = []
    unit: str | None = None
    chunk_start = start
    max_delta = timedelta(days=max_interval_days)

    while chunk_start < end:
        chunk_end = min(chunk_start + max_delta, end)
        try:
            data = await client.get_pnd_data(
                partner=partner,
                ean=ean,
                date_from=_fmt(chunk_start),
                date_to=_fmt(chunk_end),
                assembly_code=assembly_code,
            )
        except Exception as err:  # noqa: BLE001 - typ chyby je na volajícím
            if on_chunk_error:
                on_chunk_error(chunk_start, chunk_end, err)
            chunk_start = chunk_end
            continue

        if isinstance(data, list) and data:
            if unit is None:
                unit = data[0].get("unit")
            values = data[0].get("values", [])
            if isinstance(values, list):
                results.extend(values)

        chunk_start = chunk_end

    return results, unit


def _fmt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def reading_baseline(
    reading: dict[str, Any], local_tz: Any
) -> tuple[datetime, float] | None:
    """Okamžik (UTC, zaokrouhleno dolů na celou hodinu) a součet VT+NT
    (kWh) posledního odečtu z historie odečtů portálu (issue #40).

    Slouží jako základ pro odhad aktuálního stavu elektroměru: základ
    z odečtu + hodinová spotřeba z pnd/data od tohoto okamžiku.

    Předpoklad (neověřeno na portálu, jen podle běžné praxe ČEZ): odečet
    "k datu" bez času (nebo s časem 00:00) je stav na KONCI toho dne,
    tj. o lokální půlnoci následujícího dne. Pokud portál pošle i
    konkrétní čas odečtu (casOdectu, např. odečet technikem), použije
    se ten. Vrací None, když odečet nemá datum nebo ani jednu hodnotu.
    """
    vt = _to_float(reading.get("stavVt"))
    nt = _to_float(reading.get("stavNt"))
    if vt is None and nt is None:
        return None

    raw_date = str(reading.get("datumOdectu") or "").split("T")[0].strip()
    try:
        day = datetime.strptime(raw_date, "%Y-%m-%d")
    except ValueError:
        return None

    hour = minute = 0
    raw_time = str(reading.get("casOdectu") or "").strip()
    parts = raw_time.split(":")
    if len(parts) >= 2:
        try:
            hour, minute = int(parts[0]), int(parts[1])
        except ValueError:
            hour = minute = 0
        if not (0 <= hour < 24 and 0 <= minute < 60):
            hour = minute = 0

    if hour == 0 and minute == 0:
        local_naive = day + timedelta(days=1)
    else:
        local_naive = day.replace(hour=hour)  # minuty zahodíme - statistiky jsou po hodinách

    if hasattr(local_tz, "localize"):  # pytz
        local = local_tz.localize(local_naive)
    else:
        local = local_naive.replace(tzinfo=local_tz)
    baseline = local.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return baseline, (vt or 0.0) + (nt or 0.0)


def _to_float(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        return float(str(raw).strip().replace(",", "."))
    except (ValueError, TypeError):
        return None
