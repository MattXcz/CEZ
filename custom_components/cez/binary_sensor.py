"""Binary senzory pro ČEZ – poruchy a odstávky."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_EAN, DATA_OUTAGES, DOMAIN
from .coordinator import CezDistribuceCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: CezDistribuceCoordinator = hass.data[DOMAIN][entry.entry_id]
    ean = entry.data[CONF_EAN]
    async_add_entities([CezOutageSensor(coordinator, entry, ean)])


class CezOutageSensor(CoordinatorEntity[CezDistribuceCoordinator], BinarySensorEntity):
    """Indikátor plánované nebo aktuální odstávky/poruchy v síti."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_icon = "mdi:power-plug-off"
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: CezDistribuceCoordinator,
        entry: ConfigEntry,
        ean: str,
    ) -> None:
        super().__init__(coordinator)
        self._ean = ean
        self._attr_unique_id = f"{ean}_outage"
        self._attr_name = "Porucha / odstávka"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, ean)},
            name=entry.title,
            manufacturer="ČEZ",
            model="Elektroměr",
        )

    @property
    def is_on(self) -> bool:
        """True pokud je hlášena porucha nebo odstávka."""
        outages = self.coordinator.data.get(DATA_OUTAGES) if self.coordinator.data else None
        if not outages:
            return False

        # Odpověď může být list výpadků nebo dict s klíčem se seznamem
        outage_list = outages if isinstance(outages, list) else outages.get("outages") or outages.get("shutdowns") or []
        return len(outage_list) > 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        outages = self.coordinator.data.get(DATA_OUTAGES) if self.coordinator.data else {}
        outage_list = outages if isinstance(outages, list) else outages.get("outages") or outages.get("shutdowns") or []
        return {
            "poruchy_odstavky": outage_list,
            "pocet": len(outage_list),
        }
