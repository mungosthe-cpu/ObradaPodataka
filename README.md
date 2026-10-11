# TAURIS -> PDF (Analiza leta)

Mali Python program koji:

1. poveze racunar sa TAURIS uredjajem (preko serijskog porta),
2. preuzme podatke sa TAURIS uređaja preko COM-a (isto kao u programu **Analiza leta** — čitanje bajtova, bez komande `GET_RACE`),
3. generise PDF dokument spreman za kasnije skeniranje/obradu u programu **Analiza leta**.

## Sta je novo

- padajuca lista COM portova + dugme `Osvezi`
- prozor se otvara na sredini ekrana
- dugme `Preuzmi podatke` radi u pozadini (GUI se ne zamrzava)
- izbor **baud rate** (9600–115200, kao u Analiza leta)
- posle uspeha: prozor **Pregled preuzimanja** (putanje, slanje, sadržaj)
- validacija obaveznih polja trke (`race_id`, `start_time`, `duration_sec`); sirovi tekst sa COM-a ide u `raw_com_text` ako nije JSON
- unapredjen PDF sablon (fiksna forma za skeniranje + QR kod)
- auto ime izlaznih fajlova (datum/vreme + pilot + port)
- logovanje u `logs/app.log`
- `Test podaci` dugme (radi i bez uredjaja)
- `build_exe.bat` za kreiranje `.exe`
- zastita ocitanih podataka (hash + HMAC potpis + read-only JSON)
- automatsko slanje rezultata na server ili email

## Instalacija

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Pokretanje

```bash
python main.py
```

Program otvara mali prozor:

1. poruka "Povezi se sa TAURIS satom",
2. izaberete port iz padajuce liste i unesete ime pilota,
3. kliknete `Povezi se`,
4. nakon uspesnog povezivanja aktivira se dugme `Preuzmi podatke`,
5. klikom na `Preuzmi podatke` program ~15 s čita COM; **na uređaju pokrenite slanje podataka** u tom intervalu (isto ponašanje kao dijalog „Preuzmi sa TauRis“ u Analiza leta).
6. alternativno, klikom na `Test podaci` koristi se lokalni test JSON.

Program ce napraviti:

- `output/YYYYMMDD_HHMMSS_PILOT_PORT.json` - zasticeni rezultat (meta + race_data + hash + potpis)
- `output/YYYYMMDD_HHMMSS_PILOT_PORT.pdf` - PDF izvestaj

JSON fajl se cuva kao `read-only` i sadrzi digitalni potpis.
Ako neko promeni sadrzaj, hash/potpis vise ne odgovaraju originalu.

## Test rezim

Test fajl je `test_data/sample_race.json`.

Moze da se menja po potrebi, ali mora da sadrzi obavezna polja:

- `race_id`
- `start_time`
- `duration_sec`

## EXE build (Windows)

Ako zelite samostalan `.exe`:

```bash
build_exe.bat
```

Nakon build-a, fajl ce biti u:

- `dist/TaurisPdfApp.exe`

## Logovi

Program zapisuje sve greske i bitne dogadjaje u:

- `logs/app.log`

## Slanje rezultata (email/server)

Podesava se preko environment varijabli.

Podrazumevano u programu: `TAURIS_DELIVERY_MODE=email` i primalac `analizaletagolubova@gmail.com` ako `TAURIS_MAIL_TO` nije postavljen. Za stvarno slanje preko Gmail-a i dalje treba SMTP (host, korisnik, app lozinka, From).

HTTP upload podrazumevano ide na `https://www.golubovipostari.com/api/upload` ako `TAURIS_SERVER_URL` nije postavljen (mora postojati isti endpoint na serveru kao u `server_verify_example.py`).

### 1) Iskljuceno slanje

```bash
set TAURIS_DELIVERY_MODE=none
```

### 2) Slanje na server

```bash
set TAURIS_DELIVERY_MODE=server
set TAURIS_SERVER_URL=https://vas-server/api/upload
set TAURIS_SERVER_TOKEN=vas_token
set TAURIS_SIGNING_KEY=promeni_ovu_tajnu
```

### 3) Slanje na email

```bash
set TAURIS_DELIVERY_MODE=email
set TAURIS_SMTP_HOST=smtp.gmail.com
set TAURIS_SMTP_PORT=587
set TAURIS_SMTP_USER=vas_email@gmail.com
set TAURIS_SMTP_PASSWORD=app_password
set TAURIS_MAIL_FROM=vas_email@gmail.com
set TAURIS_MAIL_TO=primaoc@domen.com
set TAURIS_SIGNING_KEY=promeni_ovu_tajnu
```

### 4) Email + HTTP server (oba)

```bash
set TAURIS_DELIVERY_MODE=both
set TAURIS_SERVER_URL=https://vas-server/api/upload
set TAURIS_SERVER_TOKEN=opciono
set TAURIS_SMTP_HOST=smtp.gmail.com
set TAURIS_SMTP_PORT=587
set TAURIS_SMTP_USER=vas_email@gmail.com
set TAURIS_SMTP_PASSWORD=app_password
set TAURIS_MAIL_FROM=vas_email@gmail.com
set TAURIS_MAIL_TO=analizaletagolubova@gmail.com
set TAURIS_SIGNING_KEY=promeni_ovu_tajnu
```

Opciona varijabla:

```bash
set TAURIS_DELIVERY_TIMEOUT=15
```

## Primer server-side verifikacije

Dodat je primer endpoint-a u `server_verify_example.py`.
Endpoint prima `payload`, `json_file`, `pdf_file`, proverava hash + HMAC i tek onda cuva fajlove.

Instalacija server zavisnosti:

```bash
pip install -r requirements-server.txt
```

Pokretanje servera:

```bash
set TAURIS_SIGNING_KEY=ista_tajna_kao_u_klijentu
python server_verify_example.py
```

Default endpoint:

- `http://localhost:8080/api/upload`

U klijentu podesite:

```bash
set TAURIS_DELIVERY_MODE=server
set TAURIS_SERVER_URL=http://localhost:8080/api/upload
set TAURIS_SIGNING_KEY=ista_tajna_kao_u_klijentu
```

Ako je fajl menjan, server vraca gresku i odbija upload.

## PHP endpoint za hosting (golubovipostari.com)

Ako imate klasican hosting (cPanel/PHP), koristite fajl:

- `api/upload.php`

### Koraci

1. Uploadujte `api/upload.php` na server (npr. `public_html/api/upload.php`).
2. Na hostingu podesite env varijable:
   - `TAURIS_SERVER_TOKEN`
   - `TAURIS_SIGNING_KEY`
3. Napravite folder sa dozvolom upisa za cuvanje fajlova:
   - `server_uploads`
4. U klijentu podesite:

```bash
set TAURIS_DELIVERY_MODE=server
set TAURIS_SERVER_URL=https://www.golubovipostari.com/api/upload.php
set TAURIS_SERVER_TOKEN=isti_token_kao_na_serveru
set TAURIS_SIGNING_KEY=isti_signing_key_kao_na_serveru
```

Endpoint proverava token, hash i HMAC potpis.
Ako je JSON menjan nakon ocitavanja, upload se odbija.

## BRICON preko USB kabla (Print Manager protokol)

Program čita BRICON sat (Speedy / Speedy-Xtreme / X-treme) istim binarnim protokolom kao
Bricon Print Manager — rekonstruisan iz `BriconLib.dll` (`Communication.SendCommand`):

- port **9600 baud, 8N2, RTS=1**; svaki paket `0x6C | payload | ck` (ck = (0x6C + Σ) & 0xFF),
  odgovor `0x33 | adresa | komanda | len | status | podaci | ck`
- otkrivanje sata: `ActivateMaster` (0x10) pa `ActivateBA` (0x80–0x85), 100 ms po pokušaju
- čitanje: `GetFancier` → `GetPigeon(0…)` → `GetTimer(0…)` (kraj = status 5)
- na satu ne treba ništa da se pritiska; Print Manager mora biti zatvoren (isti COM port)

Ako se sat ne javi na izabranom portu, program ga sam traži na ostalim USB-serijskim portovima
(„COM: Auto“ kao u Print Manageru) i pamti port na kome ga je našao. Iz vremena ukorpavanja i
očitavanja računa se **kontrola sata** (odstupanje internog sata od mastera); preko 60 s PDF dobija
crveno upozorenje na vrhu. «Uvezi BRICON fajl» podrazumevano otvara `C:\ReadOuts\Printmanager`.

Rezultat se renderuje kao „lista doleta“ (isti oblik kao štampana lista) i čuva strukturirano
(`bricon_pm_*` polja u JSON-u). Proba bez GUI-ja: `python scripts\bricon_pm_frame.py COM7`.
Detalji i dijagnostika: `scripts/BRICON_COM_CAPTURE.txt`.

## Slanje bez interneta

Ako na terenu nema veze, paket ide u red (`pending_sends.json`) i program ga sam šalje kad veza
bude dostupna (pri startu, posle preuzimanja) ili ručno: Baza → «Pošalji neposlate pakete».

## Vazno — TAURIS protokol (stvarno ponasanje, ne pretpostavka)

TAURIS nema aktivnu komandu koju program salje uredjaju (nema `GET_RACE`, nema handshake) —
program samo otvori COM port i cita bajtove dok ih uredjaj salje; korisnik mora rucno pokrenuti
slanje na samom satu. Kraj prenosa se prepoznaje po zatisju (N uzastopnih praznih citanja posto
su bajtovi pocelu da stizu), ne po fiksnom marker-u — ako TAURIS-ova "kontrol lista" zaglavlje
navodi ocekivani poslednji redni broj (Lag), program dodatno saceka dok se taj red
(`NNNN SRB ...`, redovi su uvuceni razmacima) ne pojavi (do 4 dodatna kruga citanja) pre nego sto
zakljuci da je prenos gotov; ako se red ne pojavi, to je normalan kraj (Rezult.list ne mora da
sadrzi sve ukorpane golubove). Ostecen prenos (nečitljivi bajtovi) se odbija uz jasnu poruku.

Primljeni tekst moze biti ili JSON (ako pocinje sa `{`) ili obican tekst — obican tekst se cuva
u celini kao `raw_com_text` i tako se i stampa u PDF (isto kao izlaz iz programa Analiza leta);
pojedinacni redovi (broj goluba, vreme dolaska) se ne rastavljaju u posebna polja osim imena
takmicara. Kodiranje teksta se automatski pogadja (UTF-8 / CP1250 / CP1252 / Latin-1) jer TAURIS
na srpskom Windows-u cesto salje CP1250.

Ako se pojavi TAURIS uredjaj ciji odgovor ne odgovara ovom opisu (npr. stvarno salje JSON sa
markerom na kraju), javite tacan format odgovora sa uredjaja i prilagodicu parser — ne
izmisljamo protokol unapred.
