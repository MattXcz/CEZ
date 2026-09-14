
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
| `sensor.celkova_spotreba` | Senzor (kWh) | Celková spotřeba (odběr) aktivní energie – součet VT + NT |
| `sensor.aktualni_cena` | Senzor (Kč/kWh) | Aktuální cena dle HDO stavu (VT/NT), ceny nastavíte při konfiguraci |
| `sensor.vysoky_tarif_start` | Senzor | Začátek aktuálního (nebo nejbližšího) období VT |
| `sensor.vysoky_tarif_konec` | Senzor | Konec aktuálního (nebo nejbližšího) období VT |
| `sensor.nizky_tarif_start` | Senzor | Začátek aktuálního (nebo nejbližšího) období NT |
| `sensor.nizky_tarif_konec` | Senzor | Konec aktuálního (nebo nejbližšího) období NT |
| `sensor.odpocet_do_konce_vysokeho_tarifu` | Senzor (min) | Minuty do konce aktuálního/nejbližšího VT (ve výchozím stavu skrytý) |
| `sensor.odpocet_do_konce_nizkeho_tarifu` | Senzor (min) | Minuty do konce aktuálního/nejbližšího NT (ve výchozím stavu skrytý) |
| `binary_sensor.porucha_odstavka` | Binary senzor | Hlášená porucha nebo plánovaná odstávka |
| `sensor.hodinova_data_spotreby_k` | Senzor (diagnostický) | Konec poslední hodiny hodinové spotřeby naimportované do statistik (viz níže) |

Pokud přidáte odběrné místo typu **Výroba/Mikrozdroj** (FVE, `typ` `V`/`M` z `get_supply_points` – viz níže), entity se liší: HDO/tarifové senzory ani `sensor.spotreba_vt`/`sensor.spotreba_nt` se nevytváří (netýkají se dodávky do sítě – odečty u výrobního EAN jsou registry odběru, viz issue #30) a místo `sensor.celkova_spotreba` se vytvoří `sensor.celkova_dodavka_do_site` (+ diagnostická `sensor.hodinova_data_dodavky_k`).

## Hodinová spotřeba (beta)

> ⚠️ **Experimentální funkce (2.0.0-beta).** Vyžaduje chytrý elektroměr a používá neoficiální, reverzně analyzované API mobilní appky **Proud** (odlišné od portálu dip.cezdistribuce.cz). ČEZ ho může kdykoliv beze změny oznámení upravit. U odběratelů bez chytrého elektroměru se tahle část jen tiše přeskočí – zbytek integrace funguje beze změny.

Pokud máte chytrý elektroměr, integrace navíc stahuje hodinovou spotřebu a ukládá ji jako Home Assistant dlouhodobou statistiku (`cez:<ean>_consumption`), kterou lze zobrazit např. v kartě „Graf statistik" nebo přidat jako zdroj do Energy dashboardu.

Ukázka YAML pro kartu `statistics-graph`:

```yaml
type: statistics-graph
grid_options:
  columns: 24
  rows: 3
entities:
  - cez:859182400708532693_consumption
days_to_show: 3
period: hour
chart_type: bar-stack
stat_types:
  - change
```

Diagnostickou entitu „Hodinová data spotřeby k" najdete v **Nastavení → Zařízení a služby → ČEZ → zařízení**. Pokud přestane růst, import hodinové spotřeby vázne – zkontrolujte log (`custom_components.cez.coordinator`, viz níže).

Pro ruční ověření/ladění bez běžícího HA slouží `scripts/test_pnd_consumption.py` (viz komentáře ve skriptu).

Díky za reverzní analýzu appky Proud a první implementaci patří Romanu Vohradníkovi.

## Nastavení

Je nutné mít login a heslo do : https://dip.cezdistribuce.cz/ (v tuto chvíli doplněk nepracuje s přihlášením přes google či apple)

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

### Celková dodávka (přetok) do sítě – výroba/mikrozdroj

Pokud máte FVE/mikrozdroj, dodávka/přetok vede přes samostatné odběrné místo (jiný EAN, `typ` `V` Výroba nebo `M` Mikrozdroj v `get_supply_points`). Při nastavení znovu spusťte průvodce (**Nastavení → Zařízení a služby → ČEZ → Přidat zařízení**) a vyberte to druhé odběrné místo (typ Výroba/Mikrozdroj). Založí se samostatná config entry bez HDO/tarifových senzorů, se senzorem `sensor.celkova_dodavka_do_site` a hodinovou statistikou `cez:<ean>_production` (analogicky k `_consumption`).

**Odkud se dodávka bere (issue #30).** Historie odečtů z portálu (`meter-reading-history`, pole `stavVt`/`stavNt`) vrací i pro výrobní EAN registry **odběru** téhož elektroměru (+E VT/NT) – tedy přesně ta čísla, co má spotřební EAN. Registr dodávky (−E) v ní portál vůbec nemá, je vidět jen na Portálu naměřených dat jako denní stav registru. Verze 2.0.7 z těchto odečtů „dodávku“ sčítala, proto byla do kWh shodná s celkovou spotřebou. Od 2.0.8 je `sensor.celkova_dodavka_do_site` **kumulativní součet hodinové dodávky z `pnd/data`** (assemblyCode `06`, stejná data jako ve statistice `cez:<ean>_production`):

- hodnota roste od nejstarší hodiny, kterou se podařilo dohledat (při prvním spuštění až 3 roky zpátky, podle toho, odkdy máte chytrý elektroměr) – **není to stav registru −E od instalace elektroměru**, takže se od čísla na Portálu naměřených dat liší o dodávku před začátkem dohledané historie;
- odkdy a dokdy součet platí, ukazují atributy `od` a `do`; `statistic_id` odkazuje na příslušnou statistiku;
- do **Energy dashboardu** přidejte raději přímo statistiku `cez:<ean>_production` (jako „Vrácení do sítě“) – má plnou hodinovou historii;
- entita je `unknown`, dokud se hodinová dodávka nenaimportuje (chytrý elektroměr + dohledané `partner`/`anlage`, viz výše); pokud zůstane `unknown` trvale, podívejte se na diagnostickou entitu „Hodinová data dodávky k“ a do logu.

Pokud víte, který `assemblyCode` vrací přímo stav registru −E (aby entita mohla ukazovat totéž číslo jako Portál naměřených dat), pomůže výstup `scripts/test_pnd_consumption.py --ean <výrobní EAN> --assembly 02,04,06,08,10,12` – skript stáhne všechny kódy najednou a vypíše srovnávací tabulku (jednotka, krok, první/poslední hodnota, součet). Anonymizovaný výstup prosím přiložte k issue #30.

**Hodinová dodávka (`pnd/data`) pro výrobní/mikrozdrojové EAN.** MEPAS gateway pro ně nabízí samostatnou, SUDOU sadu `assemblyCode` (`02`, `04`, `06`, `08`, `10`, `12`) jako protějšek liché sady u spotřeby (`01`, `03`, `05`, `07`, `09`, `11`) na stejném partnerovi – seznam pro váš účet zjistíte přes `scripts/test_pnd_consumption.py --status`. Kód `05` (spotřeba) u výrobního EAN vrací HTTP 400. Živě ověřeno (issue #24): integrace pro ně teď používá `06`, který vrací přímo hodinovou energii v kWh – stejný vzor jako `05` u spotřeby, číselně potvrzený křížovou kontrolou proti `02` (15minutový výkon v kW, obdoba `03`).
