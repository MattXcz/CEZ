# Draft komentáře do issue #24

https://github.com/MattXcz/CEZ/issues/24

Zatím jen draft k review, neposláno.

---

Ahoj, díky za detailní dump. Zkusili jsme `test_pnd_consumption.py` i běžící integraci na dvou vlastních účtech (jiné EANy) a MEPAS/pnd flow proběhl bez jediné chyby – přihlášení, token, `pnd/data` i import hodinových bodů do statistik v pořádku (109 bodů). Takže samotný login/pnd kód aktuálně funguje, ČEZ na něm nic nerozbil.

K tomu `Authorize response: 404`: je očekávané a neškodné, není to změna na straně ČEZ ani rozbité API - vidíme ho stejně na obou našich funkčních účtech. Dohledali jsme, odkud se bere: **nevrací ho CAS, ale SAP portál** `dip.cezdistribuce.cz` na konci řetězu přesměrování (`sap-isc-etag: J2EE/irj`). CAS authorize proběhne v pořádku, vydá kód a přesměruje na portál, ten si dokončí session a jeho landing iView pak odpoví 404 s hláškou "Could not open iView. The iView is not compatible with your browser..." - kontrola prohlížeče na ne-browserový User-Agent. Log integrace zatím ukazoval jen status, ne URL, proto to vypadalo jako chyba CASu. Ukázalo se, že integrace jela opačně než prohlížeč (login napřímo, authorize až potom) - proto ten krok byl nosný. `login()` je teď přeuspořádaný do pořadí prohlížeče (authorize → login → callback → portál dostane kód), takže ten řádek z logu zmizel úplně.

V tvém `dump_readings-anon.log` je ale u obou tvých odběrných míst (spotřeba i mikrozdroj) `"ammAktivni": false`. To je pole ČEZ, které říká, jestli má dané odběrné místo aktivovaný dálkový odečet – pokud je `false`, appka Proud / MEPAS gateway na `pnd/data` bude vždycky vracet 403, bez ohledu na to, jak správně proběhne přihlášení. Vypadá to jako pravděpodobná příčina toho, že ti entita `cez:<ean>_consumption` nevzniká, ne bug v integraci.

Můžeš zkusit:
1. Zjistit/požádat ČEZ Distribuci o aktivaci dálkového odečtu pro tvé odběrné místo (přes portál nebo linku).
2. Aktualizovat na nejnovější verzi z branch `feature/pnd-hourly-consumption` – přidali jsme lepší chybové hlášky, takže pokud by to bylo něčím jiným než `ammAktivni`, uvidíš teď v logu konkrétní odpověď serveru, ne jen obecnou hlášku.

K tomu druhému OM (mikrozdroj/FVE) – na tom teď pracujeme (podpora pro typ V/M jako samostatnou dodávku do sítě), ale zatím jsme to nemohli ověřit na reálném účtu s výrobou. Až budeš mít znovu čas, klidně přilož i výstup `dump_readings.py` spuštěný přímo na tvém mikrozdrojovém EANu – pomůže nám to doladit.
