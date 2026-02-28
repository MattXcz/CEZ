"""Senzory pro ČEZ."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_EAN,
    CONF_HDO_SIGNAL,
    CONF_PRICE_NT,
    CONF_PRICE_VT,
    DATA_READINGS,
    DATA_SIGNALS,
    DEFAULT_PRICE_NT,
    DEFAULT_PRICE_VT,
    DOMAIN,
    HDO_STATE_NT,
    HDO_STATE_UNKNOWN,
    HDO_STATE_VT,
)
from .coordinator import CezDistribuceCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Nastaví senzory z config entry."""
    coordinator: CezDistribuceCoordinator = hass.data[DOMAIN][entry.entry_id]
    ean = entry.data[CONF_EAN]
    hdo_signal = entry.data.get(CONF_HDO_SIGNAL, "")

    async_add_entities(
        [
            CezHdoStateSensor(coordinator, entry, ean, hdo_signal),
            CezHdoScheduleSensor(coordinator, entry, ean, hdo_signal),
            CezTariffBoundarySensor(coordinator, entry, ean, hdo_signal, HDO_STATE_VT, "start"),
            CezTariffBoundarySensor(coordinator, entry, ean, hdo_signal, HDO_STATE_VT, "end"),
            CezTariffBoundarySensor(coordinator, entry, ean, hdo_signal, HDO_STATE_NT, "start"),
            CezTariffBoundarySensor(coordinator, entry, ean, hdo_signal, HDO_STATE_NT, "end"),
            CezTariffCountdownSensor(coordinator, entry, ean, hdo_signal, HDO_STATE_VT),
            CezTariffCountdownSensor(coordinator, entry, ean, hdo_signal, HDO_STATE_NT),
            CezReadingSensor(coordinator, entry, ean, "VT"),
            CezReadingSensor(coordinator, entry, ean, "NT"),
            CezCurrentPriceSensor(coordinator, entry, ean, hdo_signal),
        ]
    )


class CezTimeAwareSensor(CoordinatorEntity[CezDistribuceCoordinator], SensorEntity):
    """Senzor závislý na aktuálním čase; přepočítá stav každou minutu."""

    _unsub_time_listener: CALLBACK_TYPE | None = None

    async def async_added_to_hass(self) -> None:
        """Po přidání do HA spustí minutový přepočet stavu."""
        await super().async_added_to_hass()
        self._unsub_time_listener = async_track_time_interval(
            self.hass, self._handle_time_change, timedelta(minutes=1)
        )

    async def async_will_remove_from_hass(self) -> None:
        """Uklidí listener při odebrání entity."""
        if self._unsub_time_listener is not None:
            self._unsub_time_listener()
            self._unsub_time_listener = None
        await super().async_will_remove_from_hass()

    @callback
    def _handle_time_change(self, _: datetime) -> None:
        """Zapíše nový stav na minutovém tiknutí."""
        self.async_write_ha_state()


def _device_info(entry: ConfigEntry, ean: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, ean)},
        name=entry.title,
        manufacturer="ČEZ",
        model="Elektroměr",
    )


class CezCurrentPriceSensor(CezTimeAwareSensor):
    """Aktuální cena za kWh podle HDO stavu."""

    _attr_native_unit_of_measurement = "Kč/kWh"
    _attr_icon = "mdi:alpha-c-circle"
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: CezDistribuceCoordinator,
        entry: ConfigEntry,
        ean: str,
        hdo_signal: str,
    ) -> None:
        super().__init__(coordinator)
        self._hdo_signal = hdo_signal
        self._price_vt = float(entry.data.get(CONF_PRICE_VT, DEFAULT_PRICE_VT))
        self._price_nt = float(entry.data.get(CONF_PRICE_NT, DEFAULT_PRICE_NT))
        self._attr_unique_id = f"{ean}_current_price"
        self._attr_name = "Aktuální cena"
        self._attr_device_info = _device_info(entry, ean)

    @property
    def native_value(self) -> float | None:
        windows = _get_nt_windows_around_now(self.coordinator.data, self._hdo_signal)
        if windows is None:
            return None

        hdo_state = _current_hdo_state_from_windows(windows)
        if hdo_state == HDO_STATE_NT:
            return self._price_nt
        return self._price_vt

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        windows = _get_nt_windows_around_now(self.coordinator.data, self._hdo_signal)
        hdo_state = _current_hdo_state_from_windows(windows) if windows is not None else HDO_STATE_UNKNOWN

        return {
            "stav_hdo": hdo_state,
            "cena_vt": self._price_vt,
            "cena_nt": self._price_nt,
        }


class CezHdoStateSensor(CezTimeAwareSensor):
    """Aktuální stav HDO – VT nebo NT dle časového plánu spínání."""

    _attr_icon = "mdi:transmission-tower"
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: CezDistribuceCoordinator,
        entry: ConfigEntry,
        ean: str,
        hdo_signal: str,
    ) -> None:
        super().__init__(coordinator)
        self._ean = ean
        self._hdo_signal = hdo_signal
        self._attr_unique_id = f"{ean}_hdo_state"
        self._attr_name = "Stav HDO"
        self._attr_device_info = _device_info(entry, ean)

    @property
    def native_value(self) -> str:
        """Vrátí VT nebo NT podle toho, jestli aktuální čas leží v NT intervalu."""
        windows = _get_nt_windows_around_now(self.coordinator.data, self._hdo_signal)
        if windows is None:
            return HDO_STATE_UNKNOWN
        return _current_hdo_state_from_windows(windows)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        now = datetime.now()
        intervals = _get_todays_intervals(self.coordinator.data, self._hdo_signal) or []
        current_state = _state_for_minute(intervals, now.hour * 60 + now.minute)

        current_window = _current_tariff_window_absolute(intervals, current_state, now)
        next_window = _next_tariff_window_absolute(intervals, HDO_STATE_NT, now)
        return {
            "hdo_signal": self._hdo_signal,
            "nt_intervals_dnes": _format_nt_intervals(intervals),
            "od": current_window[0].isoformat() if current_window else None,
            "do": current_window[1].isoformat() if current_window else None,
            "dalsi_sepnuti": next_window[0].isoformat() if next_window else None,
        }


class CezHdoScheduleSensor(CoordinatorEntity[CezDistribuceCoordinator], SensorEntity):
    """Přehled NT intervalů HDO pro dnešní den."""

    _attr_icon = "mdi:clock-time-eight-outline"
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: CezDistribuceCoordinator,
        entry: ConfigEntry,
        ean: str,
        hdo_signal: str,
    ) -> None:
        super().__init__(coordinator)
        self._ean = ean
        self._hdo_signal = hdo_signal
        self._attr_unique_id = f"{ean}_hdo_schedule"
        self._attr_name = "Spínání HDO dnes"
        self._attr_device_info = _device_info(entry, ean)

    @property
    def native_value(self) -> int | None:
        """Počet NT intervalů dnes."""
        intervals = _get_todays_intervals(self.coordinator.data, self._hdo_signal)
        return len(intervals) if intervals is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Detailní rozpis NT intervalů jako atributy."""
        intervals = _get_todays_intervals(self.coordinator.data, self._hdo_signal) or []
        normalized_intervals = _normalize_nt_intervals(intervals)
        formatted_intervals = _format_nt_intervals(intervals)
        return {
            "hdo_signal": self._hdo_signal,
            "datum": date.today().strftime("%d.%m.%Y"),
            "pocet_intervalu": len(formatted_intervals),
            "intervaly": formatted_intervals,
            "nt_celkem_minut": sum(end - start for start, end in normalized_intervals),
        }


class CezTariffBoundarySensor(CoordinatorEntity[CezDistribuceCoordinator], SensorEntity):
    """Čas začátku / konce aktuálního nebo nejbližšího VT/NT období."""

    _attr_icon = "mdi:clock-outline"
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: CezDistribuceCoordinator,
        entry: ConfigEntry,
        ean: str,
        hdo_signal: str,
        tariff: str,
        boundary: str,
    ) -> None:
        super().__init__(coordinator)
        self._hdo_signal = hdo_signal
        self._tariff = tariff
        self._boundary = boundary

        tariff_name = "Vysoký tarif" if tariff == HDO_STATE_VT else "Nízký tarif"
        suffix = "start" if boundary == "start" else "end"
        boundary_name = "start" if boundary == "start" else "konec"

        self._attr_unique_id = f"{ean}_{tariff.lower()}_{suffix}"
        self._attr_name = f"{tariff_name} {boundary_name}"
        self._attr_device_info = _device_info(entry, ean)

    @property
    def native_value(self) -> str | None:
        intervals = _get_todays_intervals(self.coordinator.data, self._hdo_signal)
        if intervals is None:
            return None
        window = _tariff_window(intervals, self._tariff)
        if window is None:
            return None
        minute = window[0] if self._boundary == "start" else window[1]
        return _minute_to_hhmm(minute)


class CezTariffCountdownSensor(CoordinatorEntity[CezDistribuceCoordinator], SensorEntity):
    """Minutový odpočet do konce VT/NT období."""

    _attr_native_unit_of_measurement = "min"
    _attr_icon = "mdi:timer-outline"
    _attr_has_entity_name = True
    _attr_entity_registry_enabled_default = False

    def __init__(
        self,
        coordinator: CezDistribuceCoordinator,
        entry: ConfigEntry,
        ean: str,
        hdo_signal: str,
        tariff: str,
    ) -> None:
        super().__init__(coordinator)
        self._hdo_signal = hdo_signal
        self._tariff = tariff
        label = "Vysoký tarif" if tariff == HDO_STATE_VT else "Nízký tarif"

        self._attr_unique_id = f"{ean}_{tariff.lower()}_countdown"
        self._attr_name = f"Odpočet do konce {label.lower()}"
        self._attr_device_info = _device_info(entry, ean)

    @property
    def native_value(self) -> int | None:
        intervals = _get_todays_intervals(self.coordinator.data, self._hdo_signal)
        if intervals is None:
            return None
        return _minutes_until_tariff_end(intervals, self._tariff)


class CezReadingSensor(CoordinatorEntity[CezDistribuceCoordinator], SensorEntity):
    """Poslední naměřená hodnota elektroměru (VT nebo NT)."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_icon = "mdi:lightning-bolt"
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: CezDistribuceCoordinator,
        entry: ConfigEntry,
        ean: str,
        tariff: str,
    ) -> None:
        super().__init__(coordinator)
        self._ean = ean
        self._tariff = tariff
        self._attr_unique_id = f"{ean}_reading_{tariff.lower()}"
        self._attr_name = f"Stav elektroměru {tariff}"
        self._attr_device_info = _device_info(entry, ean)

    @property
    def native_value(self) -> float | None:
        """Poslední hodnota odečtu v kWh."""
        readings = self.coordinator.data.get(DATA_READINGS) if self.coordinator.data else None
        if not readings or not isinstance(readings, list):
            return None
        latest = readings[0]
        key = "stavVt" if self._tariff == "VT" else "stavNt"
        raw = latest.get(key)
        if raw is None:
            return None
        try:
            return float(str(raw).strip())
        except (ValueError, TypeError):
            _LOGGER.warning("Nelze převést hodnotu '%s' na float (tarif %s)", raw, self._tariff)
            return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        readings = self.coordinator.data.get(DATA_READINGS) if self.coordinator.data else []
        if not readings or not isinstance(readings, list):
            return {}
        latest = readings[0]
        return {
            "datum_odectu": latest.get("datumOdectu", "").split("T")[0],
            "cas_odectu": latest.get("casOdectu"),
            "duvod_odectu": latest.get("duvodOdectuText"),
            "provedl": latest.get("istablartText"),
            "status": latest.get("statusText"),
            "jednotka": latest.get("vtUnitRead" if self._tariff == "VT" else "ntUnitRead"),
        }


def _get_todays_intervals(
    data: dict | None, hdo_signal: str
) -> list[dict] | None:
    """Najde záznamy pro dnešní datum a daný signál a vrátí list intervalů."""
    if not data:
        return None

    signals_data = data.get(DATA_SIGNALS)
    if not signals_data:
        return None

    signal_list = signals_data.get("signals", [])
    if not signal_list:
        return None

    today_str = date.today().strftime("%d.%m.%Y")

    for entry in signal_list:
        if entry.get("datum") != today_str:
            continue
        if hdo_signal and entry.get("signal") != hdo_signal:
            continue
        casy_raw = entry.get("casy", "")
        return _parse_casy(casy_raw)

    return []


def _parse_casy(casy_str: str) -> list[dict]:
    """Parsuje string s intervaly HDO do listu slovníků s klíči from/to."""
    intervals = []
    for part in casy_str.split(";"):
        part = part.strip().rstrip(";").strip()
        if not part:
            continue
        if "-" in part:
            start, _, end = part.partition("-")
            start = start.strip()
            end = end.strip()
            if start and end:
                intervals.append({"from": start, "to": end})
    return intervals


def _current_hdo_state(intervals: list[dict]) -> str:
    """Vrátí NT pokud aktuální čas leží v NT intervalu, jinak VT."""
    now_minute = _current_minute()
    return _state_for_minute(intervals, now_minute)


def _get_nt_windows_around_now(data: dict | None, hdo_signal: str) -> list[tuple[datetime, datetime]] | None:
    """Vrátí NT okna kolem dneška (včera, dnes, zítra) jako absolutní datetime."""
    if not data:
        return None

    signals_data = data.get(DATA_SIGNALS)
    if not signals_data:
        return None

    signal_list = signals_data.get("signals", [])
    if not signal_list:
        return None

    today = date.today()
    relevant_dates = {today - timedelta(days=1), today, today + timedelta(days=1)}
    windows: list[tuple[datetime, datetime]] = []

    for entry in signal_list:
        if hdo_signal and entry.get("signal") != hdo_signal:
            continue
        datum = _parse_signal_date(entry.get("datum"))
        if datum is None or datum not in relevant_dates:
            continue

        for interval in _parse_casy(entry.get("casy", "")):
            start_minute = _parse_hhmm(interval.get("from", ""))
            end_minute = _parse_hhmm(interval.get("to", ""))
            if start_minute is None or end_minute is None or start_minute == end_minute:
                continue

            start_dt = datetime.combine(datum, datetime.min.time()) + timedelta(minutes=start_minute)
            end_dt = datetime.combine(datum, datetime.min.time()) + timedelta(minutes=end_minute)
            if end_minute <= start_minute:
                end_dt += timedelta(days=1)

            windows.append((start_dt, end_dt))

    return sorted(windows, key=lambda item: item[0])


def _parse_signal_date(raw: Any) -> date | None:
    try:
        return datetime.strptime(str(raw), "%d.%m.%Y").date()
    except (TypeError, ValueError):
        return None


def _current_hdo_state_from_windows(windows: list[tuple[datetime, datetime]]) -> str:
    now = datetime.now()
    return HDO_STATE_NT if _current_nt_window(windows, now) else HDO_STATE_VT


def _current_nt_window(
    windows: list[tuple[datetime, datetime]], now: datetime
) -> tuple[datetime, datetime] | None:
    for start, end in windows:
        if start <= now < end:
            return start, end
    return None


def _next_nt_window(
    windows: list[tuple[datetime, datetime]], now: datetime
) -> tuple[datetime, datetime] | None:
    current = _current_nt_window(windows, now)
    if current:
        _, current_end = current
        for start, end in windows:
            if start >= current_end:
                return start, end

    for start, end in windows:
        if start > now:
            return start, end
    return None


def _current_minute() -> int:
    now = datetime.now()
    return now.hour * 60 + now.minute


def _parse_hhmm(value: str) -> int | None:
    try:
        hour_str, minute_str = value.split(":", 1)
        hour = int(hour_str)
        minute = int(minute_str)
    except (ValueError, AttributeError):
        return None

    if hour == 24 and minute == 0:
        return 24 * 60
    if 0 <= hour < 24 and 0 <= minute < 60:
        return hour * 60 + minute
    return None


def _normalize_nt_intervals(intervals: list[dict]) -> list[tuple[int, int]]:
    """Normalizuje NT intervaly do minut a sloučí překryvy i návaznosti přes půlnoc."""
    segments: list[tuple[int, int]] = []
    for interval in intervals:
        start = _parse_hhmm(interval.get("from", ""))
        end = _parse_hhmm(interval.get("to", ""))
        if start is None or end is None or start == end:
            continue

        if end > start:
            segments.append((start, end))
        else:
            segments.append((start, 24 * 60))
            segments.append((0, end))

    if not segments:
        return []

    segments.sort(key=lambda x: (x[0], x[1]))
    merged: list[list[int]] = []
    for start, end in segments:
        if not merged:
            merged.append([start, end])
            continue

        last = merged[-1]
        if start <= last[1]:
            last[1] = max(last[1], end)
        else:
            merged.append([start, end])

    normalized = [(start, end) for start, end in merged]

    if normalized and normalized[0][0] == 0 and normalized[-1][1] == 24 * 60:
        wrapped = (normalized[-1][0], normalized[0][1] + 24 * 60)
        middle = normalized[1:-1]
        return middle + [wrapped]

    return normalized


def _is_minute_in_nt(intervals: list[dict], minute: int) -> bool:
    minute %= 24 * 60
    segments = _normalize_nt_intervals(intervals)
    for start, end in segments:
        if start <= minute < end:
            return True
        if end > 24 * 60 and start <= minute + 24 * 60 < end:
            return True
    return False


def _state_for_minute(intervals: list[dict], minute: int) -> str:
    return HDO_STATE_NT if _is_minute_in_nt(intervals, minute) else HDO_STATE_VT


def _tariff_window(intervals: list[dict], tariff: str) -> tuple[int, int] | None:
    """Vrátí start/end (v minutách dne) pro aktuální nebo nejbližší období tarifu."""
    now = _current_minute()

    def _is_tariff(minute: int) -> bool:
        return _state_for_minute(intervals, minute) == tariff

    if _is_tariff(now):
        start_offset = 0
        for back in range(1, 24 * 60 + 1):
            if not _is_tariff(now - back):
                break
            start_offset = -back
    else:
        start_offset = None
        for offset in range(1, 24 * 60 + 1):
            if _is_tariff(now + offset):
                start_offset = offset
                break
        if start_offset is None:
            return None

    start_absolute = now + start_offset
    end_absolute = None
    for offset in range(max(0, start_offset), max(0, start_offset) + 24 * 60 + 1):
        if not _is_tariff(now + offset):
            end_absolute = now + offset
            break

    if end_absolute is None:
        return None

    return start_absolute % (24 * 60), end_absolute % (24 * 60)


def _minutes_until_tariff_end(intervals: list[dict], tariff: str) -> int | None:
    """Vrátí počet minut do konce aktuálního nebo nejbližšího období tarifu."""
    now = _current_minute()

    def _is_tariff(minute: int) -> bool:
        return _state_for_minute(intervals, minute) == tariff

    started = False
    for offset in range(0, 2 * 24 * 60 + 1):
        if _is_tariff(now + offset):
            started = True
        elif started:
            return offset

    return None


def _current_tariff_window_absolute(
    intervals: list[dict], tariff: str, now: datetime
) -> tuple[datetime, datetime] | None:
    """Vrátí absolutní začátek/konec aktuálního období tarifu vůči `now`."""
    now_minute = now.hour * 60 + now.minute

    if _state_for_minute(intervals, now_minute) != tariff:
        return None

    start_offset = 0
    for back in range(1, 24 * 60 + 1):
        if _state_for_minute(intervals, now_minute - back) != tariff:
            break
        start_offset = -back

    end_offset = 0
    for forward in range(1, 24 * 60 + 1):
        if _state_for_minute(intervals, now_minute + forward) != tariff:
            end_offset = forward
            break

    if end_offset == 0:
        return None

    start_dt = now + timedelta(minutes=start_offset)
    end_dt = now + timedelta(minutes=end_offset)
    return start_dt, end_dt


def _next_tariff_window_absolute(
    intervals: list[dict], tariff: str, now: datetime
) -> tuple[datetime, datetime] | None:
    """Vrátí absolutní začátek/konec nejbližšího budoucího období tarifu vůči `now`."""
    now_minute = now.hour * 60 + now.minute

    in_tariff = _state_for_minute(intervals, now_minute) == tariff
    search_start = 1

    if in_tariff:
        for offset in range(1, 24 * 60 + 1):
            if _state_for_minute(intervals, now_minute + offset) != tariff:
                search_start = offset + 1
                break

    start_offset = None
    for offset in range(search_start, 2 * 24 * 60 + 1):
        if _state_for_minute(intervals, now_minute + offset) == tariff:
            start_offset = offset
            break

    if start_offset is None:
        return None

    end_offset = None
    for offset in range(start_offset + 1, start_offset + 24 * 60 + 1):
        if _state_for_minute(intervals, now_minute + offset) != tariff:
            end_offset = offset
            break

    if end_offset is None:
        return None

    start_dt = now + timedelta(minutes=start_offset)
    end_dt = now + timedelta(minutes=end_offset)
    return start_dt, end_dt


def _minute_to_hhmm(minute: int) -> str:
    minute %= 24 * 60
    return f"{minute // 60:02d}:{minute % 60:02d}"


def _format_nt_intervals(intervals: list[dict]) -> list[str]:
    """Vrátí intervaly v normalizované podobě vhodné pro atributy senzorů."""
    formatted: list[str] = []
    for start, end in _normalize_nt_intervals(intervals):
        start_str = _minute_to_hhmm(start)
        if end == 24 * 60:
            end_str = "24:00"
        else:
            end_str = _minute_to_hhmm(end)
        formatted.append(f"{start_str}-{end_str}")
    return formatted


def _interval_minutes(interval: dict) -> int:
    """Vrátí délku intervalu v minutách, včetně přechodu přes půlnoc."""
    start = _parse_hhmm(interval.get("from", ""))
    end = _parse_hhmm(interval.get("to", ""))
    if start is None or end is None or start == end:
        return 0
    if end >= start:
        return end - start
    return (24 * 60 - start) + end
