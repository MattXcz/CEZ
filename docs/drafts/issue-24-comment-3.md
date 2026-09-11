# Draft odpovědi do issue #24 (3. kolo)

https://github.com/MattXcz/CEZ/issues/24

Reaguje na komentář @mendreuk z 11. 9. 2026 08:27 (--status + assembly=02 logy).
Zatím jen draft k review, neposláno.

---

Ahoj, výborně, moc díky za oba logy! Radost, že spotřebu v Energy dashboardu i grafu vidíš 🎉 (dashboard „Historie" bohužel neumí externí dlouhodobé statistiky, jen entity - to je omezení HA, ne integrace).

K té dodávce - potvrdilo se přesně to, co jsem čekal, a je to skvělá zpráva pro budoucí podporu FVE/mikrozdrojů: **gateway nabízí pro dodávku samostatné, sudé kódy** (02, 04, 06, 08, 10, 12) jako protějšek lichých kódů u spotřeby (01, 03, 05, 07, 09, 11) - přesně jak jsem odhadoval. `assemblyCode=02` u tebe vrátilo 191 záznamů a data se zpracovala.

Než to ale zabudujeme do integrace natvrdo, přišel jsem na jednu věc, na kterou upozorňuju raději hned: **191 záznamů za tvoje 48hodinové okno odpovídá 15minutovému kroku, ne hodinovému** (48h × 4 = 192, sedí). To je stejný vzorec jako `03` u spotřeby (15min výkon v kW), ne jako `05` (přímo hodinová energie v kWh). Diagnostický skript to ale tou dobou ještě neuměl rozeznat - u neznámých kódů si krok jen domýšlel jako hodinový, takže "Celková spotřeba v okně: 4.569 kWh" v tvém výstupu je nejspíš **4× moc** (špatný přepočet kW→kWh i špatně zarovnané hodiny). Není to chyba v tvých datech, je to chyba v tom, jak jsme je _přepočítávali_ - opraveno teď (skript krok nově pozná z časových značek samotných dat, ne z hádání podle kódu).

Abychom měli pro dodávku stejně "čistý" kód jako `05` u spotřeby (přímo v kWh, bez přepočtu), potřebujeme najít jeho sudý protějšek - podle vzorce nejspíš `06`. Mohl bys prosím ještě jednou (na aktuální `main`):

```
python3 scripts/test_pnd_consumption.py --ean 859182400611643615 --assembly 06 --raw
```

Skript teď u neznámých kódů sám upozorní, pokud odvozený krok nesedí s předpokladem, takže uvidíš přímo v konzoli, jestli `06` vrací hodinová data (kWh) nebo taky 15minutová (kW) jako `02`.

Tvůj původní problém (chybějící entita) je vyřešený, takže klidně zavírej, jak jsi navrhoval - dodávku dotáhneme v samostatném navazujícím PR, ať se to nemotá do uzavřeného issue. Ještě jednou moc díky za trpělivost a přesná data, hodně to pomohlo.
