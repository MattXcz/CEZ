"""Koordinátor aktualizací dat pro ČEZ Distribuce."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)

try:
    from homeassistant.components.recorder.statistics import StatisticMeanType
except ImportError:  # starší HA bez StatisticMeanType - has_mean/has_sum stačí
    StatisticMeanType = None  # type: ignore[assignment,misc]
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import CezApiError, CezAuthError, CezDistribuceApiClient
from .const import (
    DATA_OUTAGES,
    DATA_READINGS,
    DATA_SIGNALS,
    DOMAIN,
    MAX_PND_INTERVAL_DAYS,
    PND_ASSEMBLY_CODE,
    PND_INTERVAL_MINUTES,
    PND_TRAILING_SAFETY_DAYS,
    UPDATE_INTERVAL_SECONDS,
)
from .pnd_processing import fetch_pnd_chunked, process_pnd_response

_LOGGER = logging.getLogger(__name__)

# Jak daleko do minulosti se má integrace pokusit dohledat data při úplně
# prvním spuštění (kdy ještě neexistují žádné dlouhodobé statistiky).
# Pokud chytrý elektroměr existuje kratší dobu, starší okna jednoduše
# vrátí prázdno/chybu - to se jen zaloguje a přeskočí, neshodí zbytek
# aktualizace.
MAX_BACKFILL_YEARS = 3


class CezDistribuceCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Koordinátor dat ČEZ Distribuce."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: CezDistribuceApiClient,
        ean: str,
        uid: str,
        partner: str = "",
        anlage: str = "",
    ) -> None:
        self._client = client
        self._ean = ean
        self._uid = uid
        self._partner = partner
        self._anlage = anlage

        # Kdy se naposledy podařilo stáhnout úplně všechna data (bez fallbacku
        # na poslední známá data). Entity vypadají "živě" i při rozbitém
        # stahování (lokálně tikající odpočty), takže tohle je způsob, jak
        # obnovu skutečně ověřit – např. přes automatizaci nebo šablonu
        # (viz issue #21).
        self.last_successful_update: datetime | None = None

        # Časové razítko "data máme uložená až do..." (konec poslední
        # úspěšně naimportované hodiny hodinové spotřeby) - čte ho
        # CezConsumptionFreshnessSensor v sensor.py. None dokud neproběhne
        # první úspěšný import (nebo pokud tahle instalace 'partner'/'anlage'
        # vůbec nemá - viz __init__.py).
        self.last_pnd_timestamp: datetime | None = None

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

        # Import hodinové/15min spotřeby do dlouhodobých statistik NIKDY
        # nesmí shodit zbytek aktualizace (odečty, HDO, odstávky mají dál
        # fungovat i kdyby se recorder API mezi verzemi HA změnilo, nebo
        # kdyby MEPAS přihlášení zrovna selhalo). Tahle část je nezávislá
        # na 'all_fresh' výše - last_successful_update sleduje portálová
        # data, last_pnd_timestamp jen čerstvost hodinové spotřeby.
        if self._partner and self._anlage:
            try:
                await self._async_update_pnd_statistics()
            except Exception:  # noqa: BLE001
                _LOGGER.exception(
                    "Import hodinové spotřeby (pnd/data) selhal "
                    "(ostatní senzory tím nejsou dotčeny)."
                )
        else:
            _LOGGER.debug(
                "Přeskakuji import hodinové spotřeby - chybí 'partner'/'anlage' "
                "v config entry (stará instalace před touto funkcí, nebo "
                "odběratel bez chytrého elektroměru)."
            )

        return merged_data

    # ------------------------------------------------------------------
    # Hodinová/15min spotřeba (appka Proud, MEPAS/AWS Gateway) jako
    # externí dlouhodobá statistika, stejný princip jako u integrace
    # vodoměru.
    # ------------------------------------------------------------------

    def _statistic_id(self) -> str:
        # DŮLEŽITÉ: statistic_id smí obsahovat jen [a-z0-9_] za dvojtečkou.
        safe_id = re.sub(r"[^a-z0-9_]", "_", self._ean.lower())
        return f"{DOMAIN}:{safe_id}_consumption"

    async def _async_update_pnd_statistics(self) -> None:
        statistic_id = self._statistic_id()
        recorder = get_instance(self.hass)
        now = datetime.now(timezone.utc)

        last_stats = await recorder.async_add_executor_job(
            get_last_statistics, self.hass, 1, statistic_id, True, {"start", "sum"}
        )
        has_existing_data = bool(last_stats and last_stats.get(statistic_id))

        if has_existing_data:
            row = last_stats[statistic_id][0]
            # 'start' z get_last_statistics je epoch sekundy (float).
            last_known_start = datetime.fromtimestamp(row["start"], tz=timezone.utc)
            self.last_pnd_timestamp = last_known_start + timedelta(hours=1)
            # Vždy se ohlédneme aspoň PND_TRAILING_SAFETY_DAYS zpátky, ne
            # jen od posledního naimportovaného bodu dál - ČEZ občas
            # doplňuje historii nespolehlivě (den vrátí prázdno, další den
            # rovnou jen nejnovější bod bez zpětného doplnění mezery), bez
            # tohohle by taková díra ve statistikách zůstala navždy.
            fetch_start = min(
                last_known_start + timedelta(minutes=PND_INTERVAL_MINUTES),
                now - timedelta(days=PND_TRAILING_SAFETY_DAYS),
            )
        else:
            fetch_start = now - timedelta(days=365 * MAX_BACKFILL_YEARS)
            _LOGGER.info(
                "Žádné dřívější statistiky pro %s - zkouším dohledat historii "
                "až %d let zpátky (kolik reálně existuje, závisí na tom, "
                "odkdy máš chytrý elektroměr).",
                statistic_id,
                MAX_BACKFILL_YEARS,
            )

        if fetch_start >= now:
            return

        # Běžící součet (kumulativní kWh pro HA Energy dashboard) musíme
        # navázat na hodnotu odpovídající bodu TĚSNĚ PŘED fetch_start - ne
        # nutně poslednímu naimportovanému bodu, protože díky ohlédnutí
        # zpátky (výše) může fetch_start ležet dřív, než kam jsme se
        # dostali minule. Přeimportování už uložených hodin je v pořádku
        # (async_add_external_statistics existující body podle "start"
        # přepíše), jen musí navazovat na správný základ. Při úplně prvním
        # běhu prostě žádná dřívější data nenajde a zůstane na 0.
        prior_stats = await recorder.async_add_executor_job(
            statistics_during_period,
            self.hass,
            fetch_start - timedelta(hours=1),
            fetch_start,
            {statistic_id},
            "hour",
            None,
            {"sum"},
        )
        baseline_sum = 0.0
        rows = prior_stats.get(statistic_id) if prior_stats else None
        if rows:
            baseline_sum = float(rows[-1].get("sum") or 0.0)

        chunk_errors: list[Exception] = []

        def _log_chunk_error(chunk_start: datetime, chunk_end: datetime, err: Exception) -> None:
            chunk_errors.append(err)
            _LOGGER.debug(
                "pnd/data pro okno %s .. %s selhalo (%s), přeskakuji.",
                chunk_start,
                chunk_end,
                err,
            )

        raw_points, unit = await fetch_pnd_chunked(
            self._client,
            self._partner,
            self._ean,
            fetch_start,
            now,
            PND_ASSEMBLY_CODE,
            MAX_PND_INTERVAL_DAYS,
            on_chunk_error=_log_chunk_error,
        )
        if not raw_points:
            if chunk_errors:
                # Všechna okna selhala (typicky opakovaný 403 z MEPAS/AWS
                # Gateway) - statistika "{DOMAIN}:<ean>_consumption" se tedy
                # tenhle cyklus nevytvoří/neaktualizuje vůbec (viz issue
                # #24 - "Nemam eventu"). Bez tohoto varování to bylo vidět
                # jen v DEBUG logu, takže si toho uživatel běžně nevšiml.
                _LOGGER.warning(
                    "Import hodinové spotřeby (%s) selhal pro všech %d "
                    "stažených oken - statistika '%s' se v tomto cyklu "
                    "nevytvoří/neaktualizuje. Poslední chyba: %s",
                    self._ean,
                    len(chunk_errors),
                    self._statistic_id(),
                    chunk_errors[-1],
                )
            return

        hourly_buckets, trimmed = process_pnd_response(
            raw_points, unit, PND_INTERVAL_MINUTES, fetch_start, now
        )
        if trimmed:
            _LOGGER.debug(
                "Ořezán souvislý 'ocas' %d nulových (zatím neúplných) "
                "záznamů z konce pro %s.",
                trimmed,
                self._ean,
            )
        if not hourly_buckets:
            return

        newest_hour_start = max(hourly_buckets)

        statistics = []
        running_sum = baseline_sum
        for hour_start in sorted(hourly_buckets):
            hour_kwh = hourly_buckets[hour_start]
            running_sum += hour_kwh
            statistics.append(
                {
                    "start": hour_start,
                    "sum": running_sum,
                    "state": hour_kwh,
                }
            )

        metadata = {
            "has_mean": False,
            "has_sum": True,
            "name": f"ČEZ spotřeba {self._ean}",
            "source": DOMAIN,
            "statistic_id": statistic_id,
            "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
            # Vyžadováno novějšími verzemi HA (recorder od podzimu 2025 bez
            # tohohle pole import statistik odmítá).
            "unit_class": "energy",
        }
        if StatisticMeanType is not None:
            # Nahrazuje deprecated has_mean bool - HA 2026.11+ ho bude
            # vyžadovat povinně, se starším has_mean ale zatím funguje
            # obojí zároveň.
            metadata["mean_type"] = StatisticMeanType.NONE

        _LOGGER.debug(
            "Importuji %s nových bodů hodinové spotřeby pro %s (statistic_id=%s)",
            len(statistics),
            self._ean,
            statistic_id,
        )
        # 'last_pnd_timestamp' (diagnostická entita) se aktualizuje AŽ PO
        # úspěšném zápisu, ne dřív - kdyby async_add_external_statistics
        # selhalo (výjimka), diagnostická entita nemá lhát, že máme data,
        # která ve skutečnosti neproběhla. Skutečný zdroj pravdy pro
        # fetch_start příštího běhu je vždy get_last_statistics() (reálný
        # stav HA databáze), ne tahle proměnná - tahle je jen pro zobrazení.
        async_add_external_statistics(self.hass, metadata, statistics)
        self.last_pnd_timestamp = newest_hour_start + timedelta(hours=1)
