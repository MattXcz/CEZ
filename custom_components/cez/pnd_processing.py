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


def summarize_pnd_points(raw_points: list[dict[str, Any]]) -> dict[str, Any]:
    """Shrnutí syrových bodů z pnd/data pro DEBUG log (bez hodnot spotřeby).

    Ukáže, kam až API data skutečně vrací: poslední časovou značku vůbec,
    poslední NENULOVOU značku (za ní jsou placeholdery, které se ořežou)
    a jaká další pole body nesou (např. příznak předběžných dat).
    Pomáhá rozlišit "API dnešek ještě nemá" od chyby importu (issue #37).
    """
    times: list[str] = []
    nonzero_times: list[str] = []
    extra_fields: Counter[str] = Counter()
    status_values: Counter[str] = Counter()
    for entry in raw_points:
        if not isinstance(entry, dict):
            continue
        t = entry.get("time")
        if isinstance(t, str):
            times.append(t)
            try:
                if float(entry.get("value")) != 0:
                    nonzero_times.append(t)
            except (TypeError, ValueError):
                pass
        for key in entry:
            if key not in ("time", "value"):
                extra_fields[key] += 1
        for key in ("status", "state", "quality", "flag"):
            if key in entry:
                status_values[f"{key}={entry[key]}"] += 1
    return {
        "points": len(raw_points),
        "first_time": min(times) if times else None,
        "last_time": max(times) if times else None,
        "last_nonzero_time": max(nonzero_times) if nonzero_times else None,
        "extra_fields": dict(extra_fields),
        "status_values": dict(status_values.most_common(10)),
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


ENERGY_PERIODS = ("today", "yesterday", "this_month", "last_month")


def energy_periods_start(now_local: datetime) -> datetime:
    """Nejstarší okamžik (UTC), který summarize_energy_periods potřebuje -
    lokální půlnoc prvního dne minulého měsíce."""
    month_start = now_local.date().replace(day=1)
    last_month_start = (month_start - timedelta(days=1)).replace(day=1)
    return _local_midnight_utc(last_month_start, now_local.tzinfo)


def _local_midnight_utc(day: Any, tz: Any) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=tz).astimezone(timezone.utc)


def summarize_energy_periods(
    hours: dict[datetime, float], now_local: datetime
) -> dict[str, Any]:
    """Součty hodinové energie za kalendářní období v LOKÁLNÍM čase HA.

    'hours' = {začátek hodiny (aware): kWh}, 'now_local' = aware čas v časové
    zóně HA (dt_util.now()). Vrací pro každé období z ENERGY_PERIODS slovník
    {kwh, hours, expected_hours} - kwh je None, když za období nejsou žádná
    data (ČEZ dodává data se zpožděním, typicky den; 0 by klamala).
    'expected_hours' počítá s přechodem letní/zimní čas (23/25 h) a u
    běžícího období jen do aktuální hodiny. Navíc 'last_hour' = {kwh, start}
    pro nejnovější hodinu s daty.
    """
    tz = now_local.tzinfo
    today = now_local.date()
    yesterday = today - timedelta(days=1)
    month_start = today.replace(day=1)
    last_month_start = (month_start - timedelta(days=1)).replace(day=1)
    now_utc = now_local.astimezone(timezone.utc)

    bounds = {
        "today": (today, today + timedelta(days=1)),
        "yesterday": (yesterday, today),
        "this_month": (month_start, (month_start + timedelta(days=32)).replace(day=1)),
        "last_month": (last_month_start, month_start),
    }
    result: dict[str, Any] = {}
    for key, (first_day, end_day) in bounds.items():
        start_utc = _local_midnight_utc(first_day, tz)
        end_utc = min(_local_midnight_utc(end_day, tz), now_utc)
        result[key] = {
            "kwh": 0.0,
            "hours": 0,
            "expected_hours": max(0, int((end_utc - start_utc).total_seconds() // 3600)),
            "_range": (start_utc, _local_midnight_utc(end_day, tz)),
        }

    newest: datetime | None = None
    for start, kwh in hours.items():
        start_utc = start.astimezone(timezone.utc)
        for period in result.values():
            lo, hi = period["_range"]
            if lo <= start_utc < hi:
                period["kwh"] += kwh
                period["hours"] += 1
        if newest is None or start_utc > newest:
            newest = start_utc

    for period in result.values():
        del period["_range"]
        period["kwh"] = round(period["kwh"], 3) if period["hours"] else None
    result["last_hour"] = {
        "kwh": round(hours_utc_value(hours, newest), 3) if newest else None,
        "start": newest,
    }
    return result


def hours_utc_value(hours: dict[datetime, float], start_utc: datetime) -> float:
    """Hodnota hodiny podle UTC začátku (klíče mohou být v libovolné zóně)."""
    for start, kwh in hours.items():
        if start.astimezone(timezone.utc) == start_utc:
            return kwh
    return 0.0


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


def find_statistic_gaps(
    hours: list[tuple[datetime, float | None]],
    zero_run_hours: int | None,
) -> tuple[datetime | None, int, int]:
    """Najde díry v hodinové statistice (issue #40).

    hours: (začátek hodiny, kWh) seřazené podle času. Díra je:
    - chybějící hodina mezi dvěma uloženými řádky,
    - souvislý běh alespoň `zero_run_hours` hodin s přesně 0 kWh
      (None = nuly nekontrolovat, např. u výroby).
    Hodiny před prvním řádkem se neřeší (elektroměr mohl začít měřit
    později). Vrací (začátek nejstarší díry nebo None, počet chybějících
    hodin, počet hodin v podezřelých nulových bězích).
    """
    earliest: datetime | None = None
    missing = 0
    zero_hours = 0
    previous: datetime | None = None
    run_start: datetime | None = None
    run_len = 0

    def _close_run() -> None:
        nonlocal earliest, zero_hours, run_start, run_len
        if zero_run_hours and run_start is not None and run_len >= zero_run_hours:
            zero_hours += run_len
            if earliest is None or run_start < earliest:
                earliest = run_start
        run_start, run_len = None, 0

    for start, kwh in hours:
        if previous is not None:
            gap = int((start - previous).total_seconds() // 3600) - 1
            if gap > 0:
                missing += gap
                gap_start = previous + timedelta(hours=1)
                if earliest is None or gap_start < earliest:
                    earliest = gap_start
                _close_run()
        if kwh is not None and kwh == 0:
            if run_start is None:
                run_start = start
            run_len += 1
        else:
            _close_run()
        previous = start
    _close_run()
    return earliest, missing, zero_hours


def reading_registers(reading: dict[str, Any]) -> tuple[float | None, float | None]:
    """Stav registrů VT a NT (kWh) z odečtu - pro odhad po tarifech (issue #49)."""
    return _to_float(reading.get("stavVt")), _to_float(reading.get("stavNt"))


def _to_float(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        return float(str(raw).strip().replace(",", "."))
    except (ValueError, TypeError):
        return None
