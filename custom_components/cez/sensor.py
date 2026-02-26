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

    async_add_entities(
        [
            CezHdoStateSensor(coordinator, entry, ean),
            CezHdoScheduleSensor(coordinator, entry, ean),
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
    """Aktuální stav HDO."""

    _attr_icon = "mdi:transmission-tower"
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: CezDistribuceCoordinator,
        entry: ConfigEntry,
        ean: str,
    ) -> None:
        super().__init__(coordinator)
        self._ean = ean
        self._attr_unique_id = f"{ean}_hdo_state"
        self._attr_name = "Stav HDO"
        self._attr_device_info = _device_info(entry, ean)

    @property
    def native_value(self) -> str:
        """Vrátí aktuální stav HDO."""
        signals = self.coordinator.data.get(DATA_SIGNALS) if self.coordinator.data else None
        if not signals:
            return HDO_STATE_UNKNOWN

        # Příklad struktury: signals může mít klíč 'currentTariff' nebo 'activeTariff'
        tariff = (
            signals.get("currentTariff")
            or signals.get("activeTariff")
            or signals.get("currentSignal")
        )
        if tariff:
            return HDO_STATE_VT if "VT" in str(tariff).upper() else HDO_STATE_NT

        # Alternativně: zkusíme zjistit z časového plánu, zda právě teď je VT/NT
        schedule = self._get_todays_schedule(signals)
        return _determine_current_state(schedule)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        signals = self.coordinator.data.get(DATA_SIGNALS) if self.coordinator.data else {}
        return {"raw_signals": signals}

    def _get_todays_schedule(self, signals: dict) -> list[dict]:
        today = date.today().isoformat()
        schedules = signals.get("schedule") or signals.get("dailySignals") or []
        for entry in schedules:
            if entry.get("date") == today:
                return entry.get("intervals") or entry.get("periods") or []
        return []


# ---------------------------------------------------------------------------
# Senzor 2 – časy spínání HDO dnes
# ---------------------------------------------------------------------------

class CezHdoScheduleSensor(CoordinatorEntity[CezDistribuceCoordinator], SensorEntity):
    """Plán spínání HDO pro dnešní den."""

    _attr_icon = "mdi:clock-time-eight-outline"
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: CezDistribuceCoordinator,
        entry: ConfigEntry,
        ean: str,
    ) -> None:
        super().__init__(coordinator)
        self._ean = ean
        self._attr_unique_id = f"{ean}_hdo_schedule"
        self._attr_name = "Spínání HDO dnes"
        self._attr_device_info = _device_info(entry, ean)

    @property
    def native_value(self) -> str | None:
        """Počet NT intervalů dnes."""
        intervals = self._todays_nt_intervals()
        return str(len(intervals)) if intervals is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Detailní rozpis spínání jako atributy."""
        intervals = self._todays_nt_intervals() or []
        return {
            "intervals": intervals,
            "count": len(intervals),
            "date": date.today().isoformat(),
        }

    def _todays_nt_intervals(self) -> list[dict] | None:
        signals = self.coordinator.data.get(DATA_SIGNALS) if self.coordinator.data else None
        if not signals:
            return None
        today = date.today().isoformat()
        schedules = signals.get("schedule") or signals.get("dailySignals") or []
        for entry in schedules:
            if entry.get("date") == today:
                return entry.get("intervals") or entry.get("periods") or []
        return []


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
        self._attr_name = f"Spotřeba {tariff}"
        self._attr_device_info = _device_info(entry, ean)

    @property
    def native_value(self) -> float | None:
        """Poslední hodnota odečtu v kWh."""
        readings = self.coordinator.data.get(DATA_READINGS) if self.coordinator.data else None
        if not readings:
            return None
        return _extract_reading_value(readings, self._tariff)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        readings = self.coordinator.data.get(DATA_READINGS) if self.coordinator.data else {}
        return {
            "tariff": self._tariff,
            "raw": readings,
        }


# ---------------------------------------------------------------------------
# Pomocné funkce
# ---------------------------------------------------------------------------

def _determine_current_state(intervals: list[dict]) -> str:
    """Zjistí z intervalu, zda právě teď platí NT nebo VT."""
    now = datetime.now().time()
    for interval in intervals:
        start_str = interval.get("from") or interval.get("start")
        end_str = interval.get("to") or interval.get("end")
        if start_str and end_str:
            try:
                start = datetime.strptime(start_str, "%H:%M").time()
                end = datetime.strptime(end_str, "%H:%M").time()
                if start <= now <= end:
                    return HDO_STATE_NT
            except ValueError:
                pass
    return HDO_STATE_VT


def _extract_reading_value(readings: Any, tariff: str) -> float | None:
    """Vytáhne hodnotu odečtu pro daný tarif z různých možných struktur odpovědi."""
    if isinstance(readings, list) and readings:
        # Vezmeme nejnovější záznam
        latest = readings[0]
        for key, val in latest.items():
            if tariff.upper() in key.upper():
                try:
                    return float(val)
                except (ValueError, TypeError):
                    pass

    if isinstance(readings, dict):
        # Zkusit přímý klíč
        for key in [tariff, tariff.lower(), f"reading{tariff}", f"value{tariff}"]:
            if key in readings:
                try:
                    return float(readings[key])
                except (ValueError, TypeError):
                    pass

    _LOGGER.debug("Nepodařilo se najít hodnotu pro tarif %s v: %s", tariff, readings)
    return None
