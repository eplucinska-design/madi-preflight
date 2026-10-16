# Uruchomienie na serwerze

Repozytorium GitHub przechowuje kod. GitHub Pages nie uruchamia backendu
Python i nie zapewnia przetwarzania PDF. Nie publikuj samego katalogu static
jako gotowej aplikacji.

## Serwer Docker

1. Pobierz repozytorium na serwer.
2. Utworz `.env` z `MADI_PASSWORD` (dlugie, unikalne haslo).
3. Uruchom `docker compose up -d --build`.
4. Skieruj istniejacy reverse proxy HTTPS do `127.0.0.1:8765`.
   Przekazuj oryginalny naglowek Host. Zezwol na zadania do 40 MB
   i ustaw timeout co najmniej 100 sekund.

Login przegladarki: `madi`; haslo pochodzi z `.env`.
Nie wystawiaj HTTP z Basic Auth bez TLS. Port kontenera jest dostepny tylko
na loopback serwera. Pliki robocze trafiaja do /tmp i sa usuwane po zadaniu.
Kontener ma limit pamieci, CPU i procesow; wymaga 512 MB miejsca roboczego.

## Aktualizacja

`git pull` i `docker compose up -d --build`.

## Status

Konfiguracja przygotowana, ale brak wybranego serwera, domeny i dostepu do
hostingu. Dodanie tych plikow na GitHub nie tworzy dzialajacego wdrozenia.
To pojedynczy serwer pracowni; skalowanie publiczne wymaga kolejki zadan,
kont uzytkownikow i dalszych testow obciazeniowych.
