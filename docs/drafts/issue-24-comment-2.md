# Draft odpovědi do issue #24 (2. kolo)

https://github.com/MattXcz/CEZ/issues/24

Reaguje na komentář @mendreuk z 10. 9. 2026 15:26 (log + dump_readings-1-anon.log).
Zatím jen draft k review, neposláno.

---

Ahoj, díky za log i nový dump - obojí posunulo věci hodně dopředu, a bohužel i vyvrátilo, co jsem tvrdil. Popořadě:

**1. Máš pravdu, `ammAktivni` s tím nesouvisí.** Omlouvám se, byl to špatný závěr. Tvůj spotřební EAN má `ammAktivni: false` a přesto ti v tom logu **hodinová data v pořádku naskočila**:

```
Importuji 118 nových bodů hodinové spotřeby pro 859182400602558577 (statistic_id=cez:859182400602558577_consumption)
```

Co `ammAktivni` skutečně znamená, nevím - a nebudu hádat znovu. Podstatné pro hodinová data je zjevně `typMereni: "B"` (průběhové měření) a `potencialniPND: true`, což máš u obou EANů. Takže po ČEZ nic aktivovat nechtěj, u nich je všechno v pořádku. README jsem opravil.

**2. Statistika `cez:859182400602558577_consumption` existuje - jen to není entita.** HA dlouhodobé statistiky z externího zdroje se nezobrazují v seznamu entit ani v Developer tools → States. Najdeš ji:
- v **Energy dashboardu**: Nastavení → Řídicí panely → Energie → Spotřeba ze sítě → *Přidat spotřebu* → v seznamu vyber `ČEZ spotřeba 859182400602558577`, nebo
- v kartě **Graf statistik** (`statistics-graph`) - YAML příklad je v README v sekci *Hodinová spotřeba*, nebo
- nepřímo přes diagnostickou entitu **„Hodinová data spotřeby k"** u zařízení ČEZ - ta ukazuje čas poslední naimportované hodiny a měla by ti teď ukazovat něco kolem 9. 9. večer.

Dej prosím vědět, jestli ji tam vidíš - jestli ne, je to jiný problém, než jsme dosud řešili.

**3. Výrobní EAN (859182400611643615) je skutečný problém, a je na naší straně.** Z dumpu je vidět, že oba EANy sedí na **stejném fyzickém elektroměru** (výrobní číslo 44564559, shodné stavy VT/NT) - mikrozdroj je u ČEZ registrace výroby na tomtéž místě (`vyrobaNaStejnemOM: "M"`). MEPAS gateway na `pnd/data` pro tenhle EAN vrací `HTTP 400 BAD REQUEST`, protože se ptáme se stejným `assemblyCode` jako u spotřeby (`05` = hodinový odběr). Pro dodávku do sítě gateway zjevně chce jiný kód, a ten zatím neznáme - na účtu s výrobou to nikdo z nás nemá kde vyzkoušet.

Navíc tvoje výrobní config entry vznikla ještě před verzí, která typ `M` rozlišuje, takže ji integrace bere jako spotřebu (proto v logu `(859182400611643615, spotřeba)` a `_consumption`). To samo o sobě 400 nezpůsobuje, ale až budeme mít správný kód, bude potřeba ten EAN odebrat a přidat znovu (nebo počkat na automatickou migraci, kterou chystám).

**Prosba:** mohl bys na aktuální verzi z `main` spustit tohle a přiložit výstup (klidně zase anonymizovaně)?

```
python3 scripts/test_pnd_consumption.py --ean 859182400611643615 --status
```

`--status` vypíše `pnd/status/<partner>` - seznam measurement/assembly kódů, které gateway pro tvůj účet nabízí. Podle něj poznáme, kterým kódem se ptát na dodávku, a pak to můžeš rovnou zkusit přes `--assembly <kód>`. Pokud appka Proud hodinovou výrobu ukazuje, ten kód tam být musí.
