# MADI Preflight Web

Lokalna aplikacja WWW oparta na istniejacym silniku MADI Preflight.
Nie wymaga konta ani uslug zewnetrznych. Pliki robocze sa usuwane po operacji.

## Uruchomienie

Python 3.10+:

```powershell
python -m pip install -r requirements.txt
python server.py
```

Otworz http://127.0.0.1:8765. Inny port: `python server.py 8766`.

## Zakres pierwszej wersji

- Interfejs inspirowany MADI Flow: ciemna nawigacja, jasny obszar pracy,
  panel parametrow i przelaczanie jasnego/ciemnego motywu.
- Wczytanie PDF do 40 MB, podglad oryginalu i wyniku, nawigacja po stronach.
- Format/spady, separacja wskazanego koloru spot, WHITE z bieli CMYK,
  normalizacja czerni, wykrojnik prostokatny lub kolowy o zadanych wymiarach.
- Linie technologiczne, usuwanie koloru spot i impozycja arkuszowa.
- Parametry formatu, ksztaltu wykrojnika, kolorow WHITE i czerni.
- Pobranie PDF po wykonaniu jednej operacji (kazda operacja korzysta z oryginalu).

Podglad pozwala przelaczac oryginal i wynik. Ta wersja nie wykonuje pelnego audytu
zgodnosci PDF/X, osadzenia fontow, DPI ani poprawnosci produkcyjnej.
Przed produkcja sprawdz wynik. Parametry nieudostepnione w panelu
korzystaja z wartosci domyslnych odziedziczonego silnika.

## Sprawdzenie dzialania

`python -m unittest test_smoke -v` sprawdza osiem operacji i podglad wynikow
na syntetycznym PDF oraz odrzucanie blednego pliku i numeru strony.

## Architektura i wdrozenie

`engine/foil_preflight.py` to niezmieniona kopia roboczego silnika z
Documents/Grafika. `server.py` uruchamia go w osobnym procesie z limitem
90 sekund. Kazde zadanie ma osobny katalog tymczasowy. Nazwy przeslanych
plikow nie sa uzywane jako sciezki serwera.

Serwer nasluchuje wylacznie lokalnie. To wersja do pracy na komputerze,
nie serwer publiczny. Publiczny hosting wymaga backendu Python, izolacji
zadan, uwierzytelniania i limitow zasobow. Nie wystarczy hosting statyczny.

## Otwarcie kodu

Kod projektu udostepniony na licencji GNU AGPL-3.0 (plik LICENSE).
Repozytorium nie zawiera dokumentow klientow ani danych roboczych.
PyMuPDF: AGPL-3.0 lub licencja komercyjna; pikepdf: MPL-2.0;
Pillow: licencja HPND. Zaleznosci zachowuja swoje licencje.

Impozycja korzysta z niezmienionego modulu `engine/imposer.py` z aplikacji
desktopowej. To impozycja siatkowa, nie skladanie ksiazek. Pelny zestaw
funkcji EXE (m.in. presety, lancuch operacji, pozostale warianty kolorow)
nie zostal jeszcze przeniesiony.
