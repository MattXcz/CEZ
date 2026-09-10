# Draft komentáře do issue #24

https://github.com/MattXcz/CEZ/issues/24

Navazuje na už zveřejněné komentáře (10. 9. 2026, 08:00 / 08:52 / 09:20 / 09:22).
Musí korigovat komentář z 09:20 ("špatně napsaný endpoint, hotfix v2.0.1") -
ten závěr byl mylný. Zatím jen draft k review, neposláno.

---

Ahoj @mendreuk, ještě jednou k tomu `Authorize response: 404` - musím opravit svůj předchozí komentář. Nebyl to špatně napsaný endpoint a v2.0.1 ve skutečnosti nic neopravila (jen přejmenovala URL na ekvivalentní alias, 404 v logu zůstalo). Dohledal jsem to až dnes do konce, tak sem dávám celý obrázek:

**Odkud se 404 bralo.** Nevracel ho CAS na `mepas.cez.cz`, ale až **SAP portál** `dip.cezdistribuce.cz` na konci řetězu přesměrování. CAS `authorize` proběhl správně, vydal kód a přesměroval na portál (`/irj/portal?code=OC-…`); portál si kódem založil session a jeho úvodní iView pak odpověděla 404 s hláškou *"Could not open iView. The iView is not compatible with your browser…"* - kontrola prohlížeče, která ne-browserovému User-Agentu neprojde. Diagnostický skript i integrace ukazovaly jen status, ne koncovou URL, takže to vypadalo jako chyba CASu. Bylo to **kosmetické a neškodné** - přihlášení i token fungovaly, což potvrzuje i tvůj vlastní výpis (KROK 4 vrátil JSON).

**Proč tam ten request vůbec byl.** Integrace se přihlašovala v opačném pořadí než prohlížeč: šla rovnou na `/cas/login?service=…` a `authorize` volala až po přihlášení. Prohlížeč (tlačítko „Přihlásit" na portálu) začíná právě tím `authorize` requestem a login je jeho důsledek. Na `main` je teď `login()` přeuspořádaný do pořadí prohlížeče - jeden `authorize` na začátku, žádný opakovaný request po loginu, a ten řádek z logu zmizel úplně. Vyjde to v příštím release.

**Pro tvůj problém se ale nic nemění** - ten 404 s ním nesouvisel. U obou tvých odběrných míst je v dumpu `"ammAktivni": false`, tj. bez aktivovaného dálkového odečtu, a MEPAS gateway pak na `pnd/data` vrací 403 bez ohledu na to, jak správně proběhne přihlášení. Proto ti entita `cez:<ean>_consumption` nevzniká. Doporučení zůstává: ověřit/požádat u ČEZ Distribuce aktivaci dálkového odečtu (portál nebo linka 800 850 860). Až bude `ammAktivni: true`, mělo by to naskočit bez dalších změn.

Až budeš mít čas, pošli prosím ještě jednou výstup `dump_readings.py` (klidně i pro to mikrozdrojové EAN) - podle něj poznáme, jestli se stav u ČEZ změnil. Issue nechávám otevřené, dokud se to nepotvrdí.
