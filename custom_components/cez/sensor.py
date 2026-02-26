"""Senzory pro ČEZ Distribuce."""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_EAN,
    CONF_HDO_SIGNAL,
    DATA_READINGS,
    DATA_SIGNALS,
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
            CezReadingSensor(coordinator, entry, ean, "VT"),
            CezReadingSensor(coordinator, entry, ean, "NT"),
        ]
    )


def _device_info(entry: ConfigEntry, ean: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, ean)},
        name=entry.title,
        manufacturer="ČEZ Distribuce",
        model="Elektroměr",
    )


# ---------------------------------------------------------------------------
# Senzor 1 – aktuální stav HDO (VT / NT)
# ---------------------------------------------------------------------------

class CezHdoStateSensor(CoordinatorEntity[CezDistribuceCoordinator], SensorEntity):
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
        intervals = _get_todays_intervals(
            self.coordinator.data, self._hdo_signal
        )
        if intervals is None:
            return HDO_STATE_UNKNOWN
        return _current_hdo_state(intervals)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        intervals = _get_todays_intervals(self.coordinator.data, self._hdo_signal) or []
        return {
            "hdo_signal": self._hdo_signal,
            "nt_intervals_dnes": [f"{i['from']}-{i['to']}" for i in intervals],
        }


# ---------------------------------------------------------------------------
# Senzor 2 – časy spínání HDO dnes
# ---------------------------------------------------------------------------

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
        return {
            "hdo_signal": self._hdo_signal,
            "datum": date.today().strftime("%d.%m.%Y"),
            "pocet_intervalu": len(intervals),
            "intervaly": [f"{i['from']}-{i['to']}" for i in intervals],
            # Celková délka NT v minutách
            "nt_celkem_minut": sum(_interval_minutes(i) for i in intervals),
        }


# ---------------------------------------------------------------------------
# Senzor 3 & 4 – poslední odečet VT / NT
# ---------------------------------------------------------------------------

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
        tariff: str,  # "VT" nebo "NT"
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


# ---------------------------------------------------------------------------
# Pomocné funkce – parsování reálné struktury ČEZ API
# ---------------------------------------------------------------------------

def _get_todays_intervals(
    data: dict | None, hdo_signal: str
) -> list[dict] | None:
    """
    Najde záznamy pro dnešní datum a daný signál a vrátí list intervalů.

    Reálná struktura API:
      data["signals"] = [
        {"signal": "a3b7dp01", "datum": "26.02.2026", "casy": "00:00-00:16;   01:14-07:56; ..."},
        ...
      ]

    Vrací list: [{"from": "00:00", "to": "00:16"}, ...]
    Vrací None pokud data nejsou k dispozici.
    """
    if not data:
        return None

    signals_data = data.get(DATA_SIGNALS)
    if not signals_data:
        return None

    signal_list = signals_data.get("signals", [])
    if not signal_list:
        return None

    today_str = date.today().strftime("%d.%m.%Y")  # formát "26.02.2026"

    for entry in signal_list:
        if entry.get("datum") != today_str:
            continue
        # Pokud je hdo_signal zadán, filtrujeme podle něj; jinak bereme první dnešní záznam
        if hdo_signal and entry.get("signal") != hdo_signal:
            continue
        casy_raw = entry.get("casy", "")
        return _parse_casy(casy_raw)

    return []


def _parse_casy(casy_str: str) -> list[dict]:
    """
    Parsuje string s intervaly jako "00:00-00:16;   01:14-07:56;   08:55-13:16;"
    na list: [{"from": "00:00", "to": "00:16"}, {"from": "01:14", "to": "07:56"}, ...]
    """
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
    now = datetime.now().strftime("%H:%M")
    for interval in intervals:
        if interval.get("from", "") <= now <= interval.get("to", ""):
            return HDO_STATE_NT
    return HDO_STATE_VT


def _interval_minutes(interval: dict) -> int:
    """Vrátí délku intervalu v minutách."""
    try:
        start = datetime.strptime(interval["from"], "%H:%M")
        end = datetime.strptime(interval["to"], "%H:%M")
        return max(0, int((end - start).total_seconds() // 60))
    except (ValueError, KeyError):
        return 0
