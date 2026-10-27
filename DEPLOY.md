# Wdrozenie online

GitHub przechowuje kod. GitHub Pages nie uruchomi backendu Python, dlatego
aplikacja dziala jako kontener Docker na hostingu kontenerow. Nie potrzebuje
komputera uzytkowniczki ani tunelu.

## Wariant A: Render (najprostszy)

1. Zaloz konto na render.com i polacz je z GitHubem.
2. Dashboard > New > Blueprint > wybierz repozytorium `madi-preflight`.
   Render odczyta `render.yaml` i zbuduje `Dockerfile`.
3. Po zbudowaniu aplikacja jest pod adresem `https://madi-preflight.onrender.com`
   (lub podobnym). Kazdy push na `main` wdraza nowa wersje.

Plan `starter` (platny, okolo kilku USD miesiecznie) ma 512 MB RAM. Plan `free`
usypia sie po bezczynnosci i ma za malo pamieci na wieksze PDF; mozna go uzyc
do proby, zmieniajac `plan` w `render.yaml` i `MADI_CHILD_MEMORY_MB` na `350`.
Wieksze pliki i impozycje wymagaja planu z 2 GB RAM (`standard`).

## Wariant B: dowolny serwer z Dockerem

```
docker build -t madi-preflight .
docker run -d --restart unless-stopped -p 127.0.0.1:8765:8765 \
  -e MADI_PUBLIC=1 -e MADI_TRUST_PROXY=1 \
  --read-only --tmpfs /tmp:size=512m --memory 2g --cpus 2 --pids-limit 64 \
  --cap-drop ALL --security-opt no-new-privileges madi-preflight
```

Przed kontenerem reverse proxy HTTPS (np. Caddy) z limitem zadania 40 MB i
timeoutem 100 s. `compose.yaml` zawiera wariant z haslem (`MADI_PASSWORD`).

## Tryby dostepu

- domyslnie serwer slucha tylko na 127.0.0.1;
- `MADI_PASSWORD`: dostep sieciowy z logowaniem (login `madi`), tylko przez HTTPS;
- `MADI_PUBLIC=1`: dostep publiczny bez hasla, z limitami ponizej.
  Bez jednej z tych zmiennych serwer odmawia startu na adresie sieciowym.

## Zabezpieczenia i limity (zmienne srodowiskowe)

| Zmienna | Domyslnie | Znaczenie |
|---|---|---|
| `MADI_MAX_JOBS` | 2 | rownolegle procesy silnika; nadmiar dostaje 503 |
| `MADI_RATE_PER_MINUTE` | 60 | zapytan POST na adres IP na minute; nadmiar 429 |
| `MADI_CHILD_MEMORY_MB` | 1536 | limit pamieci procesu silnika (Linux) |
| `MADI_MAX_PAGES` | 500 | maksymalna liczba stron dokumentu |
| `MADI_TRUST_PROXY` | 0 | ustaw 1 za proxy hostingu, aby limit liczyl IP klienta |
| `PORT` | 8765 | port, ustawiany zwykle przez hosting |

Dodatkowo: upload do 40 MB, tylko pliki zaczynajace sie od `%PDF-`; kazda
operacja w osobnym procesie z limitem czasu (90 s) i CPU (120 s); pliki robocze
w osobnym katalogu tymczasowym usuwanym po zadaniu, osierocone katalogi
sprzatane przy starcie; nazwy plikow uzytkownika nie sa sciezkami serwera;
naglowki CSP, X-Frame-Options, Referrer-Policy, nosniff; sprawdzanie Origin;
timeout polaczenia 60 s; `/healthz` dla monitoringu hostingu.

Podglad i inspekcja strony (`/api/analyze`, `/api/inspect`) dzialaja w procesie
serwera (pod blokada), bez osobnego procesu: to szybsze, ale slabsza izolacja.
Pliki nie sa zapisywane na stale ani logowane.

## Czego to nie zapewnia

To pojedyncza instancja bez kont uzytkownikow i kolejki zadan. Przy wiekszym
ruchu publicznym potrzebne beda: kolejka, wiecej instancji, ochrona przed
naduzyciami na poziomie hostingu (np. Cloudflare) i testy obciazeniowe.
Wdrozenia na zewnetrznym hostingu nie wykonano z tego repozytorium.

## Licencje

Kod: AGPL-3.0. Udostepniajac aplikacje publicznie, udostepniasz tez kod zrodlowy
(spelnione przez publiczne repozytorium). PyMuPDF: AGPL-3.0 lub licencja
komercyjna; pikepdf: MPL-2.0; Pillow: HPND; Lucide: ISC.
