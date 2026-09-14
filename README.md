# ČEZ Distribuce – integrace pro Home Assistant

<img height="90" alt="Logo ČEZ" src="https://github.com/user-attachments/assets/cf5f5141-946a-4303-a515-2a65bd3a5efe" />

[![✅ HACS Validation](https://github.com/MattXcz/CEZ/actions/workflows/hacs.yaml/badge.svg?branch=main)](https://github.com/MattXcz/CEZ/actions/workflows/hacs.yaml)
[![🔍 Code Quality](https://github.com/MattXcz/CEZ/actions/workflows/quality.yaml/badge.svg)](https://github.com/MattXcz/CEZ/actions/workflows/quality.yaml)
[![🏠 Home Assistant Validation](https://github.com/MattXcz/CEZ/actions/workflows/hassfest.yaml/badge.svg)](https://github.com/MattXcz/CEZ/actions/workflows/hassfest.yaml)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

Integrace pro Home Assistant, která získává data z portálu [ČEZ Distribuce](https://dip.cezdistribuce.cz) a z aplikace [Proud](https://www.cezdistribuce.cz/proud).

Umožňuje sledovat stav HDO, odečty elektroměru, spotřebu, cenu elektřiny, poruchy a plánované odstávky. U chytrých elektroměrů podporuje také import hodinové spotřeby a dodávky do dlouhodobých statistik Home Assistantu.

[![Buy me a beer](https://img.shields.io/badge/Buy_me_a_beer-Odměň_mě_pivkem-yellow?style=for-the-badge&logo=buy-me-a-coffee&logoColor=black)](https://www.buymeacoffee.com/mattxcz)

## Ukázky

### Spotřeba

<img width="1615" height="485" alt="Graf spotřeby v Home Assistantu" src="https://github.com/user-attachments/assets/3642cec5-063c-4aa2-a0e4-c6796b6320fa" />

### Přetoky a výroba

<img width="1455" height="914" alt="Graf přetoků a výroby v Home Assistantu" src="https://github.com/user-attachments/assets/8a5e63be-1f50-4ab0-98f9-30f6acd0f104" />

## Instalace

### Instalace přes HACS

[![Otevřít repozitář v HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?category=Integration&owner=mattxcz&repository=CEZ)

1. Otevřete **HACS → Integrace → ⋮ → Vlastní repozitáře**.
2. Přidejte URL tohoto repozitáře a jako kategorii zvolte **Integrace**.
3. Vyhledejte integraci **ČEZ** a nainstalujte ji.
4. Restartujte Home Assistant.
5. Přejděte do **Nastavení → Zařízení a služby → Přidat integraci → ČEZ**.

### Ruční instalace

1. Zkopírujte složku `custom_components/cez` do adresáře `config/custom_components/` ve své instalaci Home Assistantu.
2. Restartujte Home Assistant.
3. Přejděte do **Nastavení → Zařízení a služby → Přidat integraci → ČEZ**.

## Nastavení

Integrace vyžaduje přihlašovací údaje k portálu [ČEZ Distribuce](https://dip.cezdistribuce.cz/). Přihlášení prostřednictvím účtu Google nebo Apple zatím není podporováno.

Po přidání integrace zadejte:

- **Uživatelské jméno** – e-mail používaný pro přihlášení k portálu ČEZ Distribuce.
- **Heslo** – heslo k portálu ČEZ Distribuce.

Pokud máte více odběrných míst, průvodce vás vyzve k výběru konkrétního místa. Při výběru HDO signálu nastavíte také:

- **Cenu VT (Kč/kWh)**
- **Cenu NT (Kč/kWh)**

## Dostupné entity

| Entita | Typ | Popis |
| --- | --- | --- |
| `sensor.stav_hdo` | Senzor | Aktuální stav HDO – **VT** nebo **NT** |
| `sensor.spinani_hdo_dnes` | Senzor | Počet intervalů NT pro dnešní den; podrobný rozpis je dostupný v atributech |
| `sensor.spotreba_vt` | Senzor (kWh) | Poslední naměřený stav elektroměru ve vysokém tarifu |
| `sensor.spotreba_nt` | Senzor (kWh) | Poslední naměřený stav elektroměru v nízkém tarifu |
| `sensor.celkova_spotreba` | Senzor (kWh) | Celkový odběr aktivní energie – součet VT a NT |
| `sensor.aktualni_cena` | Senzor (Kč/kWh) | Aktuální cena podle stavu HDO; ceny VT a NT se nastavují při konfiguraci |
| `sensor.vysoky_tarif_start` | Senzor | Začátek aktuálního nebo nejbližšího období VT |
| `sensor.vysoky_tarif_konec` | Senzor | Konec aktuálního nebo nejbližšího období VT |
| `sensor.nizky_tarif_start` | Senzor | Začátek aktuálního nebo nejbližšího období NT |
| `sensor.nizky_tarif_konec` | Senzor | Konec aktuálního nebo nejbližšího období NT |
| `sensor.odpocet_do_konce_vysokeho_tarifu` | Senzor (min) | Počet minut do konce aktuálního nebo nejbližšího období VT; ve výchozím stavu je skrytý |
| `sensor.odpocet_do_konce_nizkeho_tarifu` | Senzor (min) | Počet minut do konce aktuálního nebo nejbližšího období NT; ve výchozím stavu je skrytý |
| `binary_sensor.porucha_odstavka` | Binární senzor | Hlášená porucha nebo plánovaná odstávka |
| `sensor.hodinova_data_spotreby_k` | Diagnostický senzor | Konec poslední hodiny spotřeby importované do dlouhodobých statistik |

### Výroba a mikrozdroj

Pokud přidáte odběrné místo typu **Výroba** nebo **Mikrozdroj** (FVE; `typ` `V` nebo `M` z `get_supply_points`), vytvoří se jiná sada entit:

- nevytvoří se HDO a tarifové senzory ani `sensor.spotreba_vt` a `sensor.spotreba_nt`, protože se netýkají dodávky do sítě;
- místo `sensor.celkova_spotreba` se vytvoří `sensor.celkova_dodavka_do_site`;
- vytvoří se diagnostická entita `sensor.hodinova_data_dodavky_k`.

Podrobnosti o výpočtu dodávky do sítě najdete v části [Celková dodávka do sítě](#celková-dodávka-do-sítě).

## Hodinová spotřeba

> [!IMPORTANT]
> Tato funkce vyžaduje chytrý elektroměr a používá neoficiální, reverzně analyzované API mobilní aplikace **Proud**, které je odlišné od API portálu `dip.cezdistribuce.cz`. ČEZ může toto API kdykoliv změnit bez předchozího upozornění. U odběrných míst bez chytrého elektroměru se import hodinových dat tiše přeskočí a ostatní části integrace fungují beze změny.

Pokud máte chytrý elektroměr, integrace stahuje hodinovou spotřebu a ukládá ji jako dlouhodobou statistiku Home Assistantu:

```text
cez:<ean>_consumption
```

Statistiku lze zobrazit například pomocí karty **Graf statistik** nebo ji přidat jako zdroj do Energy dashboardu.

Ukázka YAML konfigurace karty `statistics-graph`:

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

Diagnostickou entitu **Hodinová data spotřeby k** najdete v **Nastavení → Zařízení a služby → ČEZ → zařízení**. Pokud se její hodnota přestane posouvat, import hodinové spotřeby pravděpodobně vázne. V takovém případě zkontrolujte log komponenty `custom_components.cez.coordinator`.

K ručnímu ověření a ladění bez spuštěného Home Assistantu slouží skript `scripts/test_pnd_consumption.py`. Podrobnosti jsou uvedené v komentářích přímo ve skriptu.

Za reverzní analýzu aplikace Proud a první implementaci děkuji Romanu Vohradníkovi.

## Frekvence aktualizací

Data se aktualizují **každou hodinu**. Odečty elektroměru jsou na straně ČEZ obvykle dostupné jednou denně.

## Celková dodávka do sítě

Pokud máte FVE nebo mikrozdroj, dodávka (přetok) do sítě je vedena jako samostatné odběrné místo s jiným EAN. V odpovědi `get_supply_points` má toto místo `typ` `V` (**Výroba**) nebo `M` (**Mikrozdroj**).

Integraci přidejte znovu přes **Nastavení → Zařízení a služby → ČEZ → Přidat zařízení** a v průvodci vyberte výrobní odběrné místo. Vytvoří se samostatná položka konfigurace:

- bez HDO a tarifových senzorů;
- se senzorem `sensor.celkova_dodavka_do_site`;
- s hodinovou statistikou `cez:<ean>_production`.

### Zdroj dat pro celkovou dodávku

Historie odečtů z portálu (`meter-reading-history`, pole `stavVt` a `stavNt`) vrací i pro výrobní EAN registry **odběru** stejného elektroměru (`+E VT/NT`). Jde tedy o stejné hodnoty, jaké poskytuje spotřební EAN. Registr dodávky (`−E`) v této historii dostupný není; denní stav registru je viditelný pouze na Portálu naměřených dat.

Verze 2.0.7 tyto odečty sčítala jako „dodávku“, a proto výsledná hodnota v kWh odpovídala celkové spotřebě. Od verze 2.0.8 je `sensor.celkova_dodavka_do_site` kumulativním součtem hodinové dodávky z `pnd/data` s `assemblyCode` `06`. Jde o stejná data, která se ukládají do statistiky `cez:<ean>_production`.

Je proto potřeba počítat s následujícím chováním:

- Hodnota se sčítá od nejstarší hodiny, kterou se podařilo získat. Při prvním spuštění se integrace pokusí dohledat až tři roky historie podle toho, odkdy je chytrý elektroměr dostupný.
- Nejde o stav registru `−E` od instalace elektroměru. Od hodnoty na Portálu naměřených dat se proto může lišit o dodávku před začátkem dostupné historie.
- Rozsah součtu zobrazují atributy `od` a `do`. Atribut `statistic_id` odkazuje na příslušnou dlouhodobou statistiku.
- Do **Energy dashboardu** doporučujeme přidat přímo statistiku `cez:<ean>_production` jako **Vrácení do sítě**, protože obsahuje celou dostupnou hodinovou historii.
- Entita zůstává ve stavu `unknown`, dokud se hodinová dodávka nenaimportuje. Pokud tento stav přetrvává, zkontrolujte diagnostickou entitu **Hodinová data dodávky k** a log integrace.

### Ověření `assemblyCode`

Pokud víte, který `assemblyCode` vrací přímo stav registru `−E`, můžete pomoci s dalším vývojem. Spusťte:

```bash
scripts/test_pnd_consumption.py --ean <výrobní EAN> --assembly 02,04,06,08,10,12
```

Skript stáhne všechny uvedené kódy a vypíše srovnávací tabulku obsahující jednotku, časový krok, první a poslední hodnotu a součet. Anonymizovaný výstup prosím přiložte k issue #30.

MEPAS gateway nabízí pro výrobní a mikrozdrojové EAN samostatnou sadu sudých hodnot `assemblyCode` (`02`, `04`, `06`, `08`, `10`, `12`) jako protějšek liché sady používané u spotřeby (`01`, `03`, `05`, `07`, `09`, `11`) na stejném partnerovi. Seznam dostupný pro váš účet zjistíte příkazem:

```bash
scripts/test_pnd_consumption.py --status
```

Kód `05` u výrobního EAN vrací HTTP 400. Integrace proto používá kód `06`, který podle živého ověření v issue #24 vrací přímo hodinovou energii v kWh. Jde o stejný vzor jako u kódu `05` pro spotřebu; hodnoty byly potvrzeny křížovou kontrolou proti kódu `02`, který poskytuje 15minutový výkon v kW obdobně jako kód `03` u spotřeby.

## Řešení problémů

### Debug logování

Do souboru `configuration.yaml` přidejte:

```yaml
logger:
  logs:
    custom_components.cez: debug
```

Po uložení konfigurace restartujte Home Assistant a následně zkontrolujte protokol.

### Intervaly přes půlnoc

Integrace správně slučuje navazující intervaly NT přes půlnoc. Například `22:00–24:00` a `00:00–00:16` vyhodnotí jako jeden souvislý interval `22:00–00:16`.

## Důležité informace

- Integrace využívá neoficiální REST API portálu ČEZ Distribuce a mobilní aplikace Proud.
- API i struktura odpovědí se mohou bez předchozího upozornění změnit.
- V případě problémů nejprve ověřte, zda používáte nejnovější verzi integrace, a přiložte relevantní debug log.
