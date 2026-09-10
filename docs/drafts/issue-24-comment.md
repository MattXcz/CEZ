# Draft komentáře do issue #24

https://github.com/MattXcz/CEZ/issues/24

Zatím jen draft k review, neposláno.

---

Ahoj, díky za detailní dump. Zkusili jsme `test_pnd_consumption.py` i běžící integraci na dvou vlastních účtech (jiné EANy) a MEPAS/pnd flow proběhl bez jediné chyby – přihlášení, token, `pnd/data` i import hodinových bodů do statistik v pořádku (109 bodů). Takže samotný login/pnd kód aktuálně funguje, ČEZ na něm nic nerozbil.

K tomu `Authorize response: 404`: není to změna na straně ČEZ ani rozbité API. Portálový `client_id` je u CAS registrovaný jako **OAuth2.0** klient (`client_name=CasOAuthClient`, service míří na `/oauth2.0/callbackAuthorize`), takže OIDC `authorize` endpoint ho po vytvoření SSO session nenajde a vrátí 404 - na obou našich funkčních účtech úplně stejně. Cesta v URL s tím nesouvisí: CAS discovery na `mepas.cez.cz` uvádí jako `authorization_endpoint` právě `/cas/oidc/oidcAuthorize` a nepřihlášeně oba tvary (`/oidc/authorize` i `/oidc/oidcAuthorize`) vracejí shodně 302 na `/cas/login`. Odpověď z toho kroku se nikde nevyužívala (session drží cookies z předchozího POSTu), takže jsme ten krok úplně odstranili - jen plodil zavádějící 404 v diagnostice.

V tvém `dump_readings-anon.log` je ale u obou tvých odběrných míst (spotřeba i mikrozdroj) `"ammAktivni": false`. To je pole ČEZ, které říká, jestli má dané odběrné místo aktivovaný dálkový odečet – pokud je `false`, appka Proud / MEPAS gateway na `pnd/data` bude vždycky vracet 403, bez ohledu na to, jak správně proběhne přihlášení. Vypadá to jako pravděpodobná příčina toho, že ti entita `cez:<ean>_consumption` nevzniká, ne bug v integraci.

Můžeš zkusit:
1. Zjistit/požádat ČEZ Distribuci o aktivaci dálkového odečtu pro tvé odběrné místo (přes portál nebo linku).
2. Aktualizovat na nejnovější verzi z branch `feature/pnd-hourly-consumption` – přidali jsme lepší chybové hlášky, takže pokud by to bylo něčím jiným než `ammAktivni`, uvidíš teď v logu konkrétní odpověď serveru, ne jen obecnou hlášku.

K tomu druhému OM (mikrozdroj/FVE) – na tom teď pracujeme (podpora pro typ V/M jako samostatnou dodávku do sítě), ale zatím jsme to nemohli ověřit na reálném účtu s výrobou. Až budeš mít znovu čas, klidně přilož i výstup `dump_readings.py` spuštěný přímo na tvém mikrozdrojovém EANu – pomůže nám to doladit.
