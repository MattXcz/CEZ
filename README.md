
# ČEZ  – Home Assistant integrace


<img height="90" alt="logo" src="https://github.com/user-attachments/assets/cf5f5141-946a-4303-a515-2a65bd3a5efe" />

[![✅ HACS Validation](https://github.com/MattXcz/CEZ/actions/workflows/hacs.yaml/badge.svg?branch=main)](https://github.com/MattXcz/CEZ/actions/workflows/hacs.yaml)
[![🔍 Code Quality](https://github.com/MattXcz/CEZ/actions/workflows/quality.yaml/badge.svg)](https://github.com/MattXcz/CEZ/actions/workflows/quality.yaml)
[![🏠 Home Assistant Validation](https://github.com/MattXcz/CEZ/actions/workflows/hassfest.yaml/badge.svg)](https://github.com/MattXcz/CEZ/actions/workflows/hassfest.yaml)

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)


<img width="340" height="648" alt="Screen" src="https://github.com/user-attachments/assets/0f673e07-fb5f-42da-93ae-d0957f17d468" />
<img width="340" height="242" alt="Screen" src="https://github.com/user-attachments/assets/04fd0df6-dae3-49cb-930b-b1b5db8d8db0" />


Integrace pro Home Assistant, která stahuje data z portálu [ČEZ Distribuce](https://dip.cezdistribuce.cz).

[![Buy me a beer](https://img.shields.io/badge/Buy_me_a_beer-Odměn_mě_pivkem-yellow?style=for-the-badge&logo=buy-me-a-coffee&logoColor=black)](https://www.buymeacoffee.com/mattxcz)

## Instalace přes HACS
[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?category=Integration&owner=mattxcz&repository=CEZ)

1. Otevřete **HACS → Integrace → ⋮ → Vlastní repozitáře**
2. Přidejte URL tohoto repozitáře, kategorie: **Integrace**
3. Vyhledejte „ČEZ" a nainstalujte
4. Restartujte Home Assistant
5. Přejděte do **Nastavení → Zařízení a služby → Přidat integraci → ČEZ**

## Ruční instalace

Zkopírujte složku `custom_components/cez` do adresáře `config/custom_components/` ve vašem Home Assistant.


## Co integrace umí

| Entita | Typ | Popis |
|--------|-----|-------|
| `sensor.stav_hdo` | Senzor | Aktuální stav HDO – **VT** nebo **NT** |
| `sensor.spinani_hdo_dnes` | Senzor | Počet NT intervalů dnes + detailní rozpis v atributech |
| `sensor.spotreba_vt` | Senzor (kWh) | Poslední naměřená hodnota elektroměru – vysoký tarif |
| `sensor.spotreba_nt` | Senzor (kWh) | Poslední naměřená hodnota elektroměru – nízký tarif |
| `sensor.aktualni_cena` | Senzor (Kč/kWh) | Aktuální cena dle HDO stavu (VT/NT), ceny nastavíte při konfiguraci |
| `sensor.vysoky_tarif_start` | Senzor | Začátek aktuálního (nebo nejbližšího) období VT |
| `sensor.vysoky_tarif_konec` | Senzor | Konec aktuálního (nebo nejbližšího) období VT |
| `sensor.nizky_tarif_start` | Senzor | Začátek aktuálního (nebo nejbližšího) období NT |
| `sensor.nizky_tarif_konec` | Senzor | Konec aktuálního (nebo nejbližšího) období NT |
| `sensor.odpocet_do_konce_vysokeho_tarifu` | Senzor (min) | Minuty do konce aktuálního/nejbližšího VT (ve výchozím stavu skrytý) |
| `sensor.odpocet_do_konce_nizkeho_tarifu` | Senzor (min) | Minuty do konce aktuálního/nejbližšího NT (ve výchozím stavu skrytý) |
| `binary_sensor.porucha_odstavka` | Binary senzor | Hlášená porucha nebo plánovaná odstávka |

## Nastavení

Po přidání integrace zadejte:
- **Uživatelské jméno** – e-mail k portálu ČEZ Distribuce
- **Heslo** – heslo k portálu ČEZ Distribuce

Pokud máte více odběrných míst, budete vyzváni k výběru.

V kroku výběru HDO signálu nastavíte i:
- **Cena VT (Kč/kWh)**
- **Cena NT (Kč/kWh)**

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
    custom_components.cez: debug
```


### Poznámka k intervalům přes půlnoc

Integrace nově správně slučuje navazující NT intervaly přes půlnoc (např. `22:00-24:00` + `00:00-00:16` se vyhodnotí jako souvislé `22:00-00:16`).
