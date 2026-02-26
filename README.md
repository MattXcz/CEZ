# ČEZ  – Home Assistant integrace

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)


## ALPHA VERZE v tuto chvíli ! (potřebuji více testerů)

<img width="342" height="648" alt="Snímek obrazovky 2026-02-26 v 17 06 56" src="https://github.com/user-attachments/assets/0f673e07-fb5f-42da-93ae-d0957f17d468" />


Vlastní integrace pro Home Assistant, která stahuje data z portálu [ČEZ Distribuce](https://dip.cezdistribuce.cz).

<a href="https://www.buymeacoffee.com/mattxcz"><img src="https://img.buymeacoffee.com/button-api/?text=Odměn mě pivkem&emoji=🍺&slug=mattxcz&button_colour=FFDD00&font_colour=000000&font_family=Poppins&outline_colour=000000&coffee_colour=ffffff" /></a>

## Co integrace umí

| Entita | Typ | Popis |
|--------|-----|-------|
| `sensor.stav_hdo` | Senzor | Aktuální stav HDO – **VT** nebo **NT** |
| `sensor.spinani_hdo_dnes` | Senzor | Počet NT intervalů dnes + detailní rozpis v atributech |
| `sensor.spotreba_vt` | Senzor (kWh) | Poslední naměřená hodnota elektroměru – vysoký tarif |
| `sensor.spotreba_nt` | Senzor (kWh) | Poslední naměřená hodnota elektroměru – nízký tarif |
| `sensor.vysoky_tarif_start` | Senzor | Začátek aktuálního (nebo nejbližšího) období VT |
| `sensor.vysoky_tarif_konec` | Senzor | Konec aktuálního (nebo nejbližšího) období VT |
| `sensor.nizky_tarif_start` | Senzor | Začátek aktuálního (nebo nejbližšího) období NT |
| `sensor.nizky_tarif_konec` | Senzor | Konec aktuálního (nebo nejbližšího) období NT |
| `sensor.odpocet_do_konce_vysokeho_tarifu` | Senzor (min) | Minuty do konce aktuálního/nejbližšího VT (ve výchozím stavu skrytý) |
| `sensor.odpocet_do_konce_nizkeho_tarifu` | Senzor (min) | Minuty do konce aktuálního/nejbližšího NT (ve výchozím stavu skrytý) |
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


### Poznámka k intervalům přes půlnoc

Integrace nově správně slučuje navazující NT intervaly přes půlnoc (např. `22:00-24:00` + `00:00-00:16` se vyhodnotí jako souvislé `22:00-00:16`).
