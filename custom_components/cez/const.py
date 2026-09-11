"""Konstanty pro ČEZ integraci."""

DOMAIN = "cez"

# Konfigurační klíče
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_EAN = "ean"
CONF_HDO_SIGNAL = "hdo_signal"
CONF_PRICE_VT = "price_vt"
CONF_PRICE_NT = "price_nt"
DEFAULT_PRICE_VT = 3.30
DEFAULT_PRICE_NT = 2.60

# Typ odběrného místa (pole "typ" v odpovědi get_supply_points, viz enum
# "typyOM"). Běžná spotřeba je "S" - integrace se historicky chovala, jako
# by žádný jiný typ neexistoval. Výroba/mikrozdroj (FVE apod.) má vlastní
# odběrné místo (jiný EAN) typu "V"/"M" - HDO signály a cenové tarify se ho
# netýkají, jde o dodávku (přetok) aktivní energie DO sítě, ne odběr z ní
# (viz README a issue #24). Chybějící/neznámý "typ" (staré config entries
# založené před touto verzí) se bere jako běžná spotřeba, aby se chování
# stávajících instalací nezměnilo.
CONF_OM_TYPE = "om_type"
OM_TYPE_CONSUMPTION = "S"
OM_TYPES_PRODUCTION = {"V", "M"}

# Nové konfigurační klíče (hodinová/15min spotřeba přes MEPAS/AWS gateway).
# "partner" je dostupný zdarma ze stejné odpovědi jako "ean"/"uid"
# (get_supply_points), "anlage" vyžaduje jedno volání get_supply_point_detail
# navíc - obojí se ukládá do config entry při nastavení, aby se nemuselo
# dohledávat při každém startu.
CONF_PARTNER = "partner"
CONF_ANLAGE = "anlage"

# Interval aktualizace dat (v sekundách)
UPDATE_INTERVAL_SECONDS = 3600  # 1 hodina

# Klíče koordinátoru
DATA_READINGS = "readings"
DATA_SIGNALS = "signals"
DATA_OUTAGES = "outages"
DATA_SUPPLY_POINTS = "supply_points"

# Stavy HDO
HDO_STATE_VT = "VT"
HDO_STATE_NT = "NT"
HDO_STATE_UNKNOWN = "Neznámý"

# --- MEPAS / AWS Gateway (appka "Proud") - hodinová/15min spotřeba --------
#
# Appka Proud pro tohle NEPOUŽÍVÁ portálové /irj/portal API vůbec - má
# samostatné OAuth2 + PKCE přihlášení proti stejnému CAS serveru
# (mepas.cez.cz), ale s jiným client_id, a data čte přes AWS API Gateway
# (prodng.cezdis-cloudapps.net). Hodnoty ověřeny zachyceným síťovým
# provozem appky (MITM), ne jen odvozeny z dekompilace.

MEPAS_TOKEN_URL = "https://mepas.cez.cz/cas/oidc/accessToken"
MEPAS_CLIENT_ID = "9T07rTQVB5ITpkeX.dip.proud.padm.ext.zak.prod.v1"
MEPAS_SCOPE = "openid"
MEPAS_LOGIN_REDIRECT_URI = "cda://validation/"

AWS_API_GATEWAY_STATIC_URL = "https://prodng.cezdis-cloudapps.net"

# appversion/buildnumber odpovídají appce Proud v době zachycení provozu.
# ČEZ je podle všeho nekontroluje striktně (appka verzuje sama sebe), ale
# posíláme je pro jistotu shodně s appkou.
APP_VERSION = "2.4.7"
APP_BUILD_NUMBER = "447"

# assemblyCode "05" = hodinová data přímo v kWh (energie za hodinu, žádný
# přepočet netřeba) - VÝCHOZÍ VOLBA. Méně dat na přenos i výpočet, a
# stejně přesné - HA dlouhodobé statistiky (async_add_external_statistics)
# umí ukládat jen po celých hodinách, takže jemnější rozlišení by se
# beztak muselo zpětně agregovat na hodiny.
#
# assemblyCode "03" = 15minutový PRŮMĚRNÝ VÝKON v kW (ne energie).
# Přepočet na kWh za interval: hodnota_kW * (15/60) = hodnota_kW / 4.
# Ověřeno křížovou kontrolou vůči "05" - součet čtyř 15min hodnot děleno 4
# dá přesně stejné číslo jako hodinový záznam z "05". Zatím zbytečné (viz
# výše), ale necháno připravené pro budoucnost, kdyby HA někdy umožnilo
# ukládat externí statistiky i po kratších intervalech.
#
# Real-time dotahování po 15 minutách jsme záměrně NEIMPLEMENTOVALI -
# elektroměr sám odesílá data na server ČEZ nárazově (v dávkách po
# hodinách, řádově podobně jako u vodoměru), ne plynule, takže by "živá"
# entita stejně ukazovala jen zastaralé/trhavé hodnoty bez skutečného
# přínosu.
PND_ASSEMBLY_HOURLY = "05"
PND_ASSEMBLY_15MIN = "03"

# Aktivní volba pro spotřebu - změň na PND_ASSEMBLY_15MIN, pokud by HA
# v budoucnu umožnilo import externích statistik s kratším intervalem
# než hodina.
PND_ASSEMBLY_CODE = PND_ASSEMBLY_HOURLY

# Dodávka do sítě (výroba/mikrozdroj, typ V/M) - MEPAS gateway pro ni
# nabízí samostatnou, SUDOU sadu assemblyCode, zjevně jako protějšek
# liché sady u spotřeby výše (issue #24, ověřeno živě na reálném účtu
# s mikrozdrojem, pnd/status vrátil pro spotřebu 01/03/05/07/09/11 a pro
# dodávku na stejném partnerovi 02/04/06/08/10/12). "06" vrací přímo
# hodinovou energii v kWh (jednotka "kWh", 48 záznamů za 48h okno) -
# stejný vzor jako "05" u spotřeby, a číselně sedí na "02" (15minutový
# výkon v kW) po přepočtu na kWh - křížová kontrola stejná jako u "03"
# vs "05". Použití "05" pro výrobní EAN vrací HTTP 400 (odtud issue #24
# "Nemam eventu" pro mikrozdrojový OM).
PND_ASSEMBLY_HOURLY_PRODUCTION = "06"

# Mapování na skutečný krok dat v minutách - používá ho i
# pnd_processing.infer_interval_minutes() jako fallback, kdyby se krok
# nepodařilo odvodit z dat samotných.
PND_INTERVAL_MINUTES_BY_ASSEMBLY = {
    PND_ASSEMBLY_HOURLY: 60,
    PND_ASSEMBLY_15MIN: 15,
    PND_ASSEMBLY_HOURLY_PRODUCTION: 60,
}
PND_INTERVAL_MINUTES = PND_INTERVAL_MINUTES_BY_ASSEMBLY[PND_ASSEMBLY_CODE]


def pnd_assembly_code_for(om_type: str) -> str:
    """Vrátí správný assemblyCode pro pnd/data podle typu odběrného místa.

    Spotřeba (typ "S") a dodávka do sítě (typ "V"/"M") mají u MEPAS
    gateway oddělené číselné řady - viz komentář u
    PND_ASSEMBLY_HOURLY_PRODUCTION (issue #24)."""
    return (
        PND_ASSEMBLY_HOURLY_PRODUCTION
        if om_type in OM_TYPES_PRODUCTION
        else PND_ASSEMBLY_CODE
    )

# DŮLEŽITÉ: časové razítko "time" v odpovědi pnd/data označuje KONEC
# intervalu, ne začátek (ověřeno křížovou kontrolou). Při importu do HA
# statistik je nutné odečíst PND_INTERVAL_MINUTES, aby "start" pole
# odpovídalo skutečnému začátku intervalu.

# API vrací HTTP 400 "Byl překročen přípustný interval volaných dat" nad
# tímto rozsahem jednoho požadavku (ověřeno: 366 dní projde, 367 ne -
# zaokrouhleno na rok i s přestupným). Delší backfill je nutné rozdělit
# do více požadavků po sobě.
MAX_PND_INTERVAL_DAYS = 366

DATA_PND_LAST_IMPORTED = "pnd_last_imported"

# Bezpečnostní "ohlédnutí zpátky" při KAŽDÉ aktualizaci, ne jen posun
# dopředu od posledního naimportovaného bodu. Důvod: ČEZ občas doplňuje
# historii nespolehlivě (jeden den vrátí prázdno, další den rovnou jen
# nejnovější bod, ne zpětně doplněnou mezeru) - bez tohohle by taková
# díra zůstala v HA statistikách navždy, protože bychom se tam po
# postupu dopředu už nikdy nevrátili zkontrolovat.
PND_TRAILING_SAFETY_DAYS = 5
