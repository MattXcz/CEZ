"""Koordinátor aktualizací dat pro ČEZ Distribuce."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import CezApiError, CezAuthError, CezDistribuceApiClient
from .const import (
    DATA_OUTAGES,
    DATA_READINGS,
    DATA_SIGNALS,
    DOMAIN,
    UPDATE_INTERVAL_SECONDS,
)

_LOGGER = logging.getLogger(__name__)


class CezDistribuceCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Koordinátor dat ČEZ Distribuce."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: CezDistribuceApiClient,
        ean: str,
        uid: str,
    ) -> None:
        self._client = client
        self._ean = ean
        self._uid = uid

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL_SECONDS),
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Stáhne všechna potřebná data z ČEZ API."""
        try:
            readings = await self._client.get_readings(self._uid)
            signals = await self._client.get_signals(self._ean)
            outages = await self._client.get_outages(self._ean)

            return {
                DATA_READINGS: readings,
                DATA_SIGNALS: signals,
                DATA_OUTAGES: outages,
            }
        except CezAuthError as err:
            raise UpdateFailed(f"Chyba autentizace ČEZ: {err}") from err
        except CezApiError as err:
            raise UpdateFailed(f"Chyba ČEZ API: {err}") from err
        except Exception as err:
            raise UpdateFailed(f"Neočekávaná chyba: {err}") from err
