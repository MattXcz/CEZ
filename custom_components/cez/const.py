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

# Aktivní volba - změň na PND_ASSEMBLY_15MIN, pokud by HA v budoucnu
# umožnilo import externích statistik s kratším intervalem než hodina.
PND_ASSEMBLY_CODE = PND_ASSEMBLY_HOURLY

PND_INTERVAL_MINUTES = {
    PND_ASSEMBLY_HOURLY: 60,
    PND_ASSEMBLY_15MIN: 15,
}[PND_ASSEMBLY_CODE]

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
