"""Koordinátor aktualizací dat pro ČEZ Distribuce."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

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

        # Kdy se naposledy podařilo stáhnout úplně všechna data (bez fallbacku
        # na poslední známá data). Entity vypadají "živě" i při rozbitém
        # stahování (lokálně tikající odpočty), takže tohle je způsob, jak
        # obnovu skutečně ověřit – např. přes automatizaci nebo šablonu
        # (viz issue #21).
        self.last_successful_update: datetime | None = None

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL_SECONDS),
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Stáhne všechna potřebná data z ČEZ API."""
        previous_data = self.data or {}
        merged_data: dict[str, Any] = dict(previous_data)
        all_fresh = True

        async def _load_dataset(key: str, fetcher: Any) -> None:
            nonlocal all_fresh
            try:
                merged_data[key] = await fetcher()
            except CezAuthError as err:
                all_fresh = False
                if key in previous_data:
                    _LOGGER.warning(
                        "Nelze obnovit %s kvůli autentizaci (%s), ponechávám poslední známá data.",
                        key,
                        err,
                    )
                    return
                raise UpdateFailed(f"Chyba autentizace ČEZ: {err}") from err
            except CezApiError as err:
                all_fresh = False
                if key in previous_data:
                    _LOGGER.warning(
                        "Nelze obnovit %s (%s), ponechávám poslední známá data.",
                        key,
                        err,
                    )
                    return
                raise UpdateFailed(f"Chyba ČEZ API: {err}") from err
            except Exception as err:
                all_fresh = False
                if key in previous_data:
                    _LOGGER.warning(
                        "Neočekávaná chyba při obnově %s (%s), ponechávám poslední známá data.",
                        key,
                        err,
                    )
                    return
                raise UpdateFailed(f"Neočekávaná chyba: {err}") from err

        try:
            await _load_dataset(DATA_READINGS, lambda: self._client.get_readings(self._uid))
            await _load_dataset(DATA_SIGNALS, lambda: self._client.get_signals(self._ean))
            await _load_dataset(DATA_OUTAGES, lambda: self._client.get_outages(self._ean))
        except UpdateFailed:
            raise

        if not merged_data:
            raise UpdateFailed("ČEZ nevrátil žádná data.")

        if all_fresh:
            self.last_successful_update = dt_util.utcnow()

        return merged_data
