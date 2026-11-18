"""Rozdělení hodinové spotřeby na VT/NT podle plánu spínání HDO (issue #49).

Hodinová data z pnd/data tarif nerozlišují. Plán spínání HDO (NT
intervaly pro každý den) ale známe z portálu, takže lze každou hodinu
spotřeby rozpočítat podle toho, kolik minut z ní připadá na NT. Výsledek
je ODHAD:

- uvnitř hodiny se předpokládá rovnoměrná spotřeba (hodina 22:00-23:00
  s NT od 22:35 jde z 25/60 do NT),
- skutečné sepnutí relé se může od plánu o pár minut lišit,
- portál vrací plán jen pro dnešek a několik dní dopředu, pro starší dny
  se použije plán zapamatovaný z dřívějších aktualizací, případně plán
  stejného dne v týdnu (HDO plány bývají týdně periodické).

Modul je záměrně bez závislosti na Home Assistant, aby šel testovat.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterable

MINUTES_PER_DAY = 24 * 60

# Kolik dní historie plánů HDO držet v úložišti. Odečty bývají měsíční,
# občas i delší (fakturační období) - rok s rezervou stačí.
HDO_HISTORY_MAX_DAYS = 400

SOURCE_KNOWN = "known"
SOURCE_WEEKDAY = "weekday"
SOURCE_NEAREST = "nearest"


def parse_hhmm(value: str) -> int | None:
    """'22:35' -> 1355, '24:00' -> 1440. Nevalidní -> None."""
    parts = str(value).strip().split(":")
    if len(parts) < 2:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hour <= 24 and 0 <= minute < 60):
        return None
    total = hour * 60 + minute
    return total if total <= MINUTES_PER_DAY else None


def parse_casy_segments(casy: str) -> list[tuple[int, int]]:
    """Řetězec 'casy' z portálu -> NT úseky v minutách dne [start, end).

    Interval přes půlnoc ('22:35-06:35') se rozdělí na dva úseky v rámci
    téhož kalendářního dne (portál posílá plán pro každý den zvlášť,
    takže ranní část 00:00-06:35 je i v plánu následujícího dne). Překryvy
    a návaznosti se sloučí.
    """
    segments: list[tuple[int, int]] = []
    for part in str(casy or "").split(";"):
        part = part.strip()
        if "-" not in part:
            continue
        raw_start, _, raw_end = part.partition("-")
        start = parse_hhmm(raw_start)
        end = parse_hhmm(raw_end)
        if start is None or end is None or start == end:
            continue
        if end > start:
            segments.append((start, end))
        else:
            if start < MINUTES_PER_DAY:
                segments.append((start, MINUTES_PER_DAY))
            if end > 0:
                segments.append((0, end))

    segments.sort()
    merged: list[tuple[int, int]] = []
    for start, end in segments:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def nt_minutes_in_range(segments: list[tuple[int, int]], start: int, end: int) -> int:
    """Počet NT minut v intervalu [start, end) (minuty dne)."""
    total = 0
    for seg_start, seg_end in segments:
        overlap = min(end, seg_end) - max(start, seg_start)
        if overlap > 0:
            total += overlap
    return total


def extract_signal_schedules(signals_data: Any, hdo_signal: str) -> dict[str, str]:
    """Odpověď get_signals -> {'YYYY-MM-DD': casy} pro zvolený signál.

    Bez nastaveného signálu se bere první záznam pro daný den - stejně
    jako to dělají senzory HDO v sensor.py.
    """
    result: dict[str, str] = {}
    if not isinstance(signals_data, dict):
        return result
    for entry in signals_data.get("signals") or []:
        if not isinstance(entry, dict):
            continue
        if hdo_signal and entry.get("signal") != hdo_signal:
            continue
        try:
            day = datetime.strptime(str(entry.get("datum")), "%d.%m.%Y").date()
        except (TypeError, ValueError):
            continue
        key = day.isoformat()
        if key in result:
            continue
        casy = entry.get("casy")
        if isinstance(casy, str):
            result[key] = casy
    return result


def merge_schedule_history(
    history: dict[str, str], fresh: dict[str, str], today: date
) -> bool:
    """Doplní/aktualizuje historii plánů, ořízne staré dny. Vrací True při změně."""
    changed = False
    for key, casy in fresh.items():
        if history.get(key) != casy:
            history[key] = casy
            changed = True
    cutoff = (today - timedelta(days=HDO_HISTORY_MAX_DAYS)).isoformat()
    for key in [k for k in history if k < cutoff]:
        del history[key]
        changed = True
    return changed


@dataclass
class ScheduleLookup:
    """Hledání plánu HDO pro libovolný den s fallbackem."""

    history: dict[str, str]
    _parsed: dict[date, list[tuple[int, int]]] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        for key, casy in self.history.items():
            try:
                day = date.fromisoformat(key)
            except ValueError:
                continue
            self._parsed[day] = parse_casy_segments(casy)

    def __bool__(self) -> bool:
        return bool(self._parsed)

    def segments_for(self, day: date) -> tuple[list[tuple[int, int]], str] | None:
        """NT úseky pro den + zdroj (known / weekday / nearest)."""
        if not self._parsed:
            return None
        if day in self._parsed:
            return self._parsed[day], SOURCE_KNOWN
        same_weekday = [d for d in self._parsed if d.weekday() == day.weekday()]
        if same_weekday:
            best = min(same_weekday, key=lambda d: abs((d - day).days))
            return self._parsed[best], SOURCE_WEEKDAY
        best = min(self._parsed, key=lambda d: abs((d - day).days))
        return self._parsed[best], SOURCE_NEAREST


@dataclass
class TariffSplit:
    vt_kwh: float = 0.0
    nt_kwh: float = 0.0
    hours: int = 0
    days_by_source: dict[str, set[date]] = field(default_factory=dict)
    # Spotřeba po lokálních dnech: {date: [vt, nt]}
    daily: dict[date, list[float]] = field(default_factory=dict)

    def days_count(self, source: str) -> int:
        return len(self.days_by_source.get(source, ()))


def split_hourly_consumption(
    hours: Iterable[tuple[datetime, float]],
    lookup: ScheduleLookup,
    to_local: Any,
) -> TariffSplit | None:
    """Rozdělí hodinovou spotřebu na VT/NT.

    hours: (začátek hodiny jako tz-aware datetime, kWh za tu hodinu).
    to_local: funkce převádějící tz-aware datetime do lokální zóny HA
    (dt_util.as_local). Vrací None, když není žádný plán HDO.
    """
    if not lookup:
        return None
    split = TariffSplit()
    for start, kwh in hours:
        local = to_local(start)
        found = lookup.segments_for(local.date())
        if found is None:
            return None
        segments, source = found
        minute = local.hour * 60 + local.minute
        end = min(minute + 60, MINUTES_PER_DAY)
        nt_ratio = nt_minutes_in_range(segments, minute, end) / max(end - minute, 1)
        nt = kwh * nt_ratio
        vt = kwh - nt
        split.vt_kwh += vt
        split.nt_kwh += nt
        split.hours += 1
        split.days_by_source.setdefault(source, set()).add(local.date())
        day = split.daily.setdefault(local.date(), [0.0, 0.0])
        day[0] += vt
        day[1] += nt
    return split
