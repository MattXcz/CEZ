"""Konstanty pro ČEZ Distribuce integraci."""

DOMAIN = "cez"

# Konfigurační klíče
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_EAN = "ean"
CONF_HDO_SIGNAL = "hdo_signal"

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
