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

- Warsztat DTP blizszy aplikacji EXE: zakladki do 12 dokumentow,
  miniatury stron, centralny podglad, pasek narzedzi i dokowane panele.
- Panele: Wlasciwosci / Kolejka / Inspektor / Presety. Parametry w rozwijanych
  grupach, pola zalezne od wybranego modelu koloru i ksztaltu.
- Widoczna biblioteka narzedzi i przyciski nazwane wedlug konkretnej operacji.
- Osobny warsztat impozycji: przod/tyl arkusza, przechodzenie miedzy arkuszami,
  szybkie ustawienia A3/A4/SRA3 i rzeczywisty podglad PDF ustawien.
  Podglad korzysta z tej samej funkcji ukladu co eksport. Wypelnienia pustych
  pozycji sa tylko w podgladzie; nie sa drukowane w PDF wynikowym.
- Powiekszanie 50-300%, dopasowanie strony, przesuwanie widoku, zaznaczanie
  ramki geometrii po analizie i przeciaganie plikow PDF do obszaru dokumentu.
- Wczytanie PDF do 40 MB, podglad oryginalu i wyniku, nawigacja po stronach.
- Format/spady, separacja wskazanego koloru spot, WHITE z bieli CMYK,
  normalizacja czerni, wykrojnik prostokatny lub kolowy o zadanych wymiarach.
- Linie technologiczne, usuwanie koloru spot i impozycja arkuszowa.
- Parametry formatu, ksztaltu wykrojnika, kolorow WHITE i czerni.
- Kolejka do 12 operacji z wykonywaniem po kolei, zmiana kolejnosci i usuwaniem.
- Wybor wejscia: oryginal albo poprzedni wynik.
- Lokalne presety parametrow i kolejek (localStorage przegladarki).
- Organizacja stron: wybor, kolejnosc, powielanie, zakresy i obrot.
- Czyszczenie znacznikow (heurystyczne), format po wykrojniku,
  nieregularny obrys, perforacja i mapowanie kolorow linii.
- Rozszerzone ustawienia WHITE, czerni, CMYK/RGB/spot, tolerancji i impozycji.
- Inspektor biezacej strony: kolory spot z zasobow, osadzenie fontow,
  efektywna rozdzielczosc obrazow, pola strony i deklaracje overprint.
- Klikniecie geometrii w podgladzie po analizie pokazuje jej atrybuty podgladowe.

Podglad pozwala przelaczac oryginal i wynik. Ta wersja nie wykonuje pelnego audytu
zgodnosci PDF/X, osadzenia fontow, DPI ani poprawnosci produkcyjnej.
Przed produkcja sprawdz wynik. Parametry nieudostepnione w panelu
korzystaja z wartosci domyslnych odziedziczonego silnika.

## Sprawdzenie dzialania

`python -m unittest test_smoke -v` sprawdza operacje i podglad wynikow,
kolejnosc workflow, obrot i powielanie stron, inspektor oraz bledne wejscie.
Testy nie korzystaja z dokumentow klientow.
Sprawdzana jest tez zgodnosc renderu podgladu i eksportu impozycji,
lustrzany uklad tylu oraz brak farby w pustych pozycjach produkcyjnych.

## Architektura i wdrozenie

`engine/foil_preflight.py` to niezmieniona kopia roboczego silnika z
Documents/Grafika. `server.py` uruchamia go w osobnym procesie z limitem
90 sekund na cala kolejke. Kazde zadanie ma osobny katalog tymczasowy. Nazwy przeslanych
plikow nie sa uzywane jako sciezki serwera.

Domyslnie serwer nasluchuje lokalnie. Przy dostepie sieciowym wymaga
MADI_PASSWORD; login to madi. Dostep zdalny wymaga reverse proxy HTTPS.
Dockerfile i compose.yaml przygotowuja kontener dla serwera pracowni.
Instrukcja: DEPLOY.md. Wdrozenie na zewnetrznym serwerze nie zostalo wykonane.
GitHub Pages nie uruchomi silnika Python. Kontener wymaga osobnego hostingu.

## Otwarcie kodu

Kod projektu udostepniony na licencji GNU AGPL-3.0 (plik LICENSE).
Repozytorium nie zawiera dokumentow klientow ani danych roboczych.
PyMuPDF: AGPL-3.0 lub licencja komercyjna; pikepdf: MPL-2.0;
Pillow: licencja HPND. Zaleznosci zachowuja swoje licencje.
Ikony Lucide sa dolaczone lokalnie na licencji ISC (static/vendor/lucide-LICENSE).

Impozycja i czyszczenie znacznikow korzystaja z modulow aplikacji desktopowej.
To impozycja siatkowa, nie skladanie ksiazek. Czyszczenie moze usunac grafike
przypominajaca znaczniki: nalezy sprawdzic podglad wyniku.

## Granice inspekcji i dalsze DTP

Inspektor analizuje biezaca strone, w tym zasoby formularzy XObject.
Deklaracje ExtGState nie dowodza efektywnego nadruku konkretnego obiektu.
Brak osadzenia fontu jest informacja, nie automatyczna ocena zgodnosci PDF/X.
Kolory geometrii pochodza z RGB podgladu, nie z oryginalnych probek PDF.
Klikniecie wybiera najmniejsza otaczajaca ramke geometrii (maks. 2000),
nie wykonuje dokladnego hit-testu sciezki. Tekst i obrazy nie sa edytowane.

Ta wersja nie jest pelnym edytorem Acrobat/Illustrator. Nadal wymagaja
implementacji: edycja wezlow i tekstu, warstwy, przypisywanie probek do
wybranych obiektow, ich efektywny overprint, zarzadzanie ICC i walidacja PDF/X.
