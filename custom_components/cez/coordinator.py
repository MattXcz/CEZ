"""Koordinátor aktualizací dat pro ČEZ Distribuce."""
from __future__ import annotations

import json
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
    OM_TYPE_CONSUMPTION,
    OM_TYPES_PRODUCTION,
    PND_INTERVAL_MINUTES_BY_ASSEMBLY,
    PND_TRAILING_SAFETY_DAYS,
    UPDATE_INTERVAL_SECONDS,
    pnd_assembly_code_for,
)
from .pnd_processing import fetch_pnd_chunked, infer_interval_minutes, process_pnd_response

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
        om_type: str = OM_TYPE_CONSUMPTION,
    ) -> None:
        self._client = client
        self._ean = ean
        self._uid = uid
        self._partner = partner
        self._anlage = anlage
        self._om_type = om_type or OM_TYPE_CONSUMPTION
        # Výroba/mikrozdroj (FVE) - dodávka aktivní energie DO sítě, ne
        # odběr z ní. HDO signály/tarify se jí netýkají a hodinová
        # statistika se pojmenovává jinak (viz issue #24).
        self._is_production = self._om_type in OM_TYPES_PRODUCTION
        # Spotřeba a dodávka mají u MEPAS gateway oddělené assemblyCode
        # řady (issue #24) - viz pnd_assembly_code_for()/const.py.
        self._assembly_code = pnd_assembly_code_for(self._om_type)
        self._assembly_interval_minutes = PND_INTERVAL_MINUTES_BY_ASSEMBLY[self._assembly_code]

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

        # Kumulativní součet (kWh) hodinových dat naimportovaných do
        # dlouhodobé statistiky - tj. poslední hodnota 'sum' statistiky
        # "{DOMAIN}:<ean>_consumption/_production". U výroby/mikrozdroje
        # je to JEDINÝ použitelný zdroj pro entitu "Celková dodávka do
        # sítě" (issue #30): historie odečtů (get_readings) vrací i pro
        # výrobní EAN registry ODBĚRU (+E VT/NT, stejný fyzický
        # elektroměr), registr dodávky (-E) v ní portál vůbec nemá.
        # Počítá se od nejstarší hodiny, kterou se podařilo dohledat (viz
        # MAX_BACKFILL_YEARS), ne od instalace elektroměru - proto se
        # vedle toho drží i 'pnd_first_timestamp', aby entita uměla říct,
        # od kdy součet platí. None dokud statistika neexistuje.
        self.pnd_total_kwh: float | None = None
        self.pnd_first_timestamp: datetime | None = None

        # Diagnostika pnd/status (issue #30) - jednou za běh HA zjistíme a
        # zalogujeme, jaké assemblyCode kódy ČEZ pro tohoto partnera/EAN
        # vůbec nabízí, ať to nemusí uživatelé zjišťovat ručně přes
        # scripts/test_pnd_consumption.py --status. 'pnd_status_info' je
        # navíc vystavené jako atribut diagnostické entity (sensor.py), ať
        # je to vidět i bez čtení logu.
        self._pnd_status_checked = False
        self.pnd_status_info: dict[str, Any] | None = None

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
            if not self._is_production:
                # Historie odečtů (stavVt/stavNt) a HDO spínací signály
                # dávají smysl jen u odběrného místa běžné spotřeby. U
                # výroby/mikrozdroje vrací get_readings registry ODBĚRU
                # téhož elektroměru (issue #30 - tři nezávislé účty,
                # hodnoty do kWh shodné se spotřebním EAN), takže by šlo
                # jen o zbytečné volání API a zavádějící entity; HDO
                # signály výrobní OM nemá vůbec.
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

    @property
    def statistic_id(self) -> str:
        """ID dlouhodobé statistiky hodinových dat tohoto odběrného místa
        ("cez:<ean>_consumption" / "cez:<ean>_production")."""
        return self._statistic_id()

    def _statistic_id(self) -> str:
        # DŮLEŽITÉ: statistic_id smí obsahovat jen [a-z0-9_] za dvojtečkou.
        safe_id = re.sub(r"[^a-z0-9_]", "_", self._ean.lower())
        suffix = "production" if self._is_production else "consumption"
        return f"{DOMAIN}:{safe_id}_{suffix}"

    async def _async_update_pnd_statistics(self) -> None:
        statistic_id = self._statistic_id()
        recorder = get_instance(self.hass)
        now = datetime.now(timezone.utc)

        # Diagnostika (issue #30) - nezávislá na zbytku metody, nikdy
        # nesmí zablokovat/shodit reálný import dat, proto vlastní try/except
        # navíc k tomu, co má metoda samotná.
        try:
            await self._async_log_pnd_status()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Diagnostika pnd/status selhala (%s), pokračuji.", err)

        last_stats = await recorder.async_add_executor_job(
            get_last_statistics, self.hass, 1, statistic_id, True, {"start", "sum"}
        )
        has_existing_data = bool(last_stats and last_stats.get(statistic_id))

        if has_existing_data:
            row = last_stats[statistic_id][0]
            # 'start' z get_last_statistics je epoch sekundy (float).
            last_known_start = datetime.fromtimestamp(row["start"], tz=timezone.utc)
            self.last_pnd_timestamp = last_known_start + timedelta(hours=1)
            # Kumulativní součet z DB hned po startu (a v cyklech, kdy
            # nepřijde nic nového) - entita "Celková dodávka do sítě"
            # nemá čekat na první nový hodinový bod (issue #30).
            if row.get("sum") is not None:
                self.pnd_total_kwh = float(row["sum"])
            if self.pnd_first_timestamp is None:
                self.pnd_first_timestamp = await self._async_find_first_statistic_start(
                    recorder, statistic_id, now
                )
            # Vždy se ohlédneme aspoň PND_TRAILING_SAFETY_DAYS zpátky, ne
            # jen od posledního naimportovaného bodu dál - ČEZ občas
            # doplňuje historii nespolehlivě (den vrátí prázdno, další den
            # rovnou jen nejnovější bod bez zpětného doplnění mezery), bez
            # tohohle by taková díra ve statistikách zůstala navždy.
            fetch_start = min(
                last_known_start + timedelta(minutes=self._assembly_interval_minutes),
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
            self._assembly_code,
            MAX_PND_INTERVAL_DAYS,
            on_chunk_error=_log_chunk_error,
        )
        if not raw_points:
            if chunk_errors:
                # Všechna okna selhala (typicky opakovaný 403 z MEPAS/AWS
                # Gateway) - statistika "{DOMAIN}:<ean>_consumption/_production"
                # se tedy tenhle cyklus nevytvoří/neaktualizuje vůbec (viz
                # issue #24 - "Nemam eventu"). Bez tohoto varování to bylo
                # vidět jen v DEBUG logu, takže si toho uživatel běžně nevšiml.
                _LOGGER.warning(
                    "Import hodinových dat (ean=%s, partner=%s, "
                    "assemblyCode=%s, %s) selhal pro všech %d stažených "
                    "oken - statistika '%s' se v tomto cyklu "
                    "nevytvoří/neaktualizuje. Poslední chyba: %s "
                    "(viz i pnd/status výše v logu - issue #30).",
                    self._ean,
                    self._partner,
                    self._assembly_code,
                    "výroba" if self._is_production else "spotřeba",
                    len(chunk_errors),
                    statistic_id,
                    chunk_errors[-1],
                )
            return

        # Bezpečnostní pojistka (issue #24): krok dat odvozujeme přímo
        # z časových značek odpovědi, ne jen z assemblyCode - u kódu
        # PND_ASSEMBLY_HOURLY_PRODUCTION ("06", dodávka) máme zatím jen
        # jeden ověřený reálný účet, a stejná chyba (špatný předpoklad
        # intervalu -> 4x špatný přepočet kW/kWh) se dřív skryla i v
        # diagnostickém skriptu. Pro známý spotřební kód "05" se odvozená
        # hodnota vždy shoduje, takže tady nic neměníme.
        interval_minutes = infer_interval_minutes(
            raw_points, fallback=self._assembly_interval_minutes
        )
        if interval_minutes != self._assembly_interval_minutes:
            _LOGGER.warning(
                "pnd/data pro %s (assemblyCode=%s): krok dat podle časových "
                "značek je %d min, ne předpokládaných %d min - používám "
                "odvozenou hodnotu (issue #24).",
                self._ean,
                self._assembly_code,
                interval_minutes,
                self._assembly_interval_minutes,
            )

        hourly_buckets, trimmed = process_pnd_response(
            raw_points, unit, interval_minutes, fetch_start, now
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

        stat_name = (
            f"ČEZ dodávka do sítě {self._ean}"
            if self._is_production
            else f"ČEZ spotřeba {self._ean}"
        )
        metadata = {
            "has_mean": False,
            "has_sum": True,
            "name": stat_name,
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
        self.pnd_total_kwh = running_sum
        oldest_hour_start = min(hourly_buckets)
        if self.pnd_first_timestamp is None or oldest_hour_start < self.pnd_first_timestamp:
            self.pnd_first_timestamp = oldest_hour_start

    async def _async_find_first_statistic_start(
        self, recorder: Any, statistic_id: str, now: datetime
    ) -> datetime | None:
        """Začátek nejstarší hodiny uložené ve statistice (nebo None).

        Recorder nemá "první řádek" dotaz, a hodinových řádků za až
        MAX_BACKFILL_YEARS let jsou desítky tisíc - proto dvoukrokově:
        nejdřív měsíční agregace (pár desítek řádků), pak hodinové řádky
        jen v nalezeném prvním měsíci. Volá se jednou po startu, výsledek
        se drží v 'pnd_first_timestamp'. Slouží jen jako informace pro
        atribut entity (od kdy platí kumulativní součet dodávky, issue
        #30) - selhání se zaloguje a nic dalšího neovlivní."""
        search_start = now - timedelta(days=365 * MAX_BACKFILL_YEARS + 31)
        try:
            monthly = await recorder.async_add_executor_job(
                statistics_during_period,
                self.hass,
                search_start,
                None,
                {statistic_id},
                "month",
                None,
                {"sum"},
            )
            month_rows = monthly.get(statistic_id) if monthly else None
            if not month_rows:
                return None
            month_start = _stat_row_start(month_rows[0])
            if month_start is None:
                return None
            hourly = await recorder.async_add_executor_job(
                statistics_during_period,
                self.hass,
                month_start,
                month_start + timedelta(days=32),
                {statistic_id},
                "hour",
                None,
                {"sum"},
            )
            hour_rows = hourly.get(statistic_id) if hourly else None
            if not hour_rows:
                return month_start
            return _stat_row_start(hour_rows[0]) or month_start
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Nepodařilo se zjistit začátek statistiky %s (%s) - atribut "
                "'od' u kumulativního součtu zůstane prázdný.",
                statistic_id,
                err,
            )
            return None

    async def _async_log_pnd_status(self) -> None:
        """Jednou za běh HA zaloguje syrovou odpověď pnd/status/{partner}
        (issue #30).

        Cíl: zjistit, jaké assemblyCode kódy ČEZ pro tohoto partnera/EAN
        vůbec nabízí, přímo v HA logu - bez nutnosti ručně spouštět
        scripts/test_pnd_consumption.py --status. Nevoláme to každou
        hodinu (partner/EAN se v rámci běhu HA nemění), jen jednou po
        startu, a NEBLOKUJE to skutečný import dat - volá se před ním,
        ale jakákoliv chyba se jen zaloguje a pokračuje se dál.

        Neznáme přesnou strukturu odpovědi (žádný zachycený vzorek), proto
        se nepokoušíme nic automaticky rozhodovat - jen co nejvíc
        informací do logu a na diagnostickou entitu, ať to jde vyhodnotit
        z reportů uživatelů (viz issue #30 diskuze)."""
        if self._pnd_status_checked:
            return
        self._pnd_status_checked = True

        try:
            status = await self._client.get_pnd_status(self._partner)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "pnd/status/%s selhalo (%s) - nelze zjistit, jaké assemblyCode "
                "kódy ČEZ pro tohoto partnera nabízí (issue #30, EAN %s).",
                self._partner,
                err,
                self._ean,
            )
            self.pnd_status_info = {"error": str(err)}
            return

        codes_found = _extract_assembly_codes(status)
        code_offered = self._assembly_code in codes_found if codes_found else None
        self.pnd_status_info = {
            "partner": self._partner,
            "assembly_code_used": self._assembly_code,
            "codes_found": codes_found,
            "assembly_code_offered": code_offered,
        }

        _LOGGER.info(
            "pnd/status/%s (EAN=%s, %s): používaný "
            "assemblyCode=%s, nalezené kódy v odpovědi=%s, používaný kód "
            "nabízen=%s.",
            self._partner,
            self._ean,
            "výroba" if self._is_production else "spotřeba",
            self._assembly_code,
            codes_found if codes_found is not None else "nerozpoznáno ze schématu odpovědi",
            code_offered,
        )
        # Syrová odpověď zvlášť (i kdyby výše extrakce podle klíče
        # "assembly*" nic nenašla, protože neznáme přesné schéma) - ať má
        # člověk čtoucí log kompletní podklad, ne jen náš (možná chybný)
        # výklad (issue #30).
        _LOGGER.info(
            "pnd/status/%s syrová odpověď (zkráceno na 4000 znaků): %s",
            self._partner,
            json.dumps(status, ensure_ascii=False, default=str)[:4000],
        )

        if codes_found and not code_offered:
            _LOGGER.warning(
                "ČEZ pro partnera %s (EAN %s) v pnd/status NEnabízí "
                "assemblyCode %s, který integrace používá pro %s - dálkový "
                "odečet pro tuhle granularitu/registr zřejmě není na tomto "
                "účtu aktivovaný (issue #30). Nabízené kódy: %s.",
                self._partner,
                self._ean,
                self._assembly_code,
                "výrobu" if self._is_production else "spotřebu",
                codes_found,
            )


def _extract_assembly_codes(raw: Any) -> list[str] | None:
    """Nejlepší odhad seznamu assemblyCode kódů z odpovědi pnd/status.

    Neznáme přesné schéma odpovědi (žádný zachycený vzorek) - hledáme
    proto obecně jakýkoliv klíč obsahující "assembly" (case-insensitive)
    kdekoliv ve vnořené struktuře. Vrací None, pokud se nic takového
    nenajde (to NEMUSÍ znamenat, že kódy nejsou nabízené - jen že odpověď
    má jiné schéma, než jsme čekali; proto se vždy loguje i syrová
    odpověď, viz _async_log_pnd_status)."""
    found: set[str] = set()

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if "assembly" in key.lower() and isinstance(value, (str, int)):
                    found.add(str(value))
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(raw)
    return sorted(found) if found else None


def _stat_row_start(row: dict[str, Any]) -> datetime | None:
    """'start' řádku statistiky jako tz-aware UTC datetime.

    statistics_during_period vrací 'start' v novějších verzích HA jako
    epoch sekundy (float), ve starších jako datetime - bereme obojí."""
    start = row.get("start")
    if isinstance(start, datetime):
        return start if start.tzinfo else start.replace(tzinfo=timezone.utc)
    if isinstance(start, (int, float)):
        return datetime.fromtimestamp(start, tz=timezone.utc)
    return None
