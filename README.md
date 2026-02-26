# ČEZ  – Home Assistant integrace

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

Vlastní integrace pro Home Assistant, která stahuje data z portálu [ČEZ Distribuce](https://dip.cezdistribuce.cz).

## Co integrace umí

| Entita | Typ | Popis |
|--------|-----|-------|
| `sensor.stav_hdo` | Senzor | Aktuální stav HDO – **VT** nebo **NT** |
| `sensor.spinani_hdo_dnes` | Senzor | Počet NT intervalů dnes + detailní rozpis v atributech |
| `sensor.spotreba_vt` | Senzor (kWh) | Poslední naměřená hodnota elektroměru – vysoký tarif |
| `sensor.spotreba_nt` | Senzor (kWh) | Poslední naměřená hodnota elektroměru – nízký tarif |
| `binary_sensor.porucha_odstavka` | Binary senzor | Hlášená porucha nebo plánovaná odstávka |

## Instalace přes HACS

1. Otevřete **HACS → Integrace → ⋮ → Vlastní repozitáře**
2. Přidejte URL tohoto repozitáře, kategorie: **Integrace**
3. Vyhledejte „ČEZ Distribuce" a nainstalujte
4. Restartujte Home Assistant
5. Přejděte do **Nastavení → Zařízení a služby → Přidat integraci → ČEZ Distribuce**

## Ruční instalace

Zkopírujte složku `custom_components/cez_distribuce` do adresáře `config/custom_components/` ve vašem Home Assistant.

## Nastavení

Po přidání integrace zadejte:
- **Uživatelské jméno** – e-mail k portálu ČEZ Distribuce
- **Heslo** – heslo k portálu ČEZ Distribuce

Pokud máte více odběrných míst, budete vyzváni k výběru.

## Frekvence aktualizací

Data se obnovují **každou hodinu**. Odečty elektroměru jsou ze strany ČEZ dostupné typicky jednou denně.

## Poznámky

- Integrace využívá neoficiální REST API portálu ČEZ Distribuce
- API může být bez upozornění změněno – sledujte prosím aktualizace
- Struktura odpovědí API může vyžadovat drobné úpravy po ověření s reálnými daty

## Řešení problémů

Zapněte debug logování přidáním do `configuration.yaml`:

```yaml
logger:
  logs:
    custom_components.cez_distribuce: debug
```
