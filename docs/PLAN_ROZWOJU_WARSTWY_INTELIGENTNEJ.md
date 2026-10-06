# Przewijak LIVE — plan rozwoju Warstwy Inteligentnej

## Zasada nadrzędna

RDZEŃ 2.9.16 pozostaje zamrożony. Warstwa inteligentna może czytać dane RDZENIA, ale nie może sama zmieniać parserów, `collector.js`, transportu, `MASTER_MATCH_UID`, `SOURCE_MATCH_UID` ani surowych snapshotów.

Fizyczny podział:

- `database/przewijak.sqlite3` — RDZEŃ / źródło prawdy,
- `intelligence/observer.sqlite3` — kandydaci DOM i ich historia,
- `intelligence/analytics.sqlite3` — markery, rewanże, serie i testy strategii.

## ETAP 1 — CENTRUM MECZÓW — ZREALIZOWANY

- 1 karta = 1 `MASTER_MATCH_UID`,
- wszystkie źródła w jednym widoku,
- wynik, czas, linia, kursy, świeżość,
- `ready_score`, `ready_clock`, `ready_market`, `ready_live_model`, `ready_ft`,
- ostatni gol i zmiana linii,
- seria pary / numer spotkania,
- podstawowe markery 30/60/90 s.

## ETAP 2 — OBSERWATOR DOM — ZREALIZOWANY W 3.1.0

Observer działa jako osobny proces przeglądarki tylko do odczytu. Nie używa ani nie modyfikuje przeglądarki collectora.

Szuka kandydatów:

- WYNIK,
- CZAS,
- LINIA,
- KURS POWYŻEJ,
- KURS PONIŻEJ,
- KONIEC MECZU.

Każdy kandydat zapisuje m.in.:

- selektor techniczny,
- źródło,
- ostatnią wartość,
- liczbę obserwacji,
- stabilność,
- zgodność z bieżącą referencją RDZENIA,
- kolizje,
- confidence 0–100,
- ocenę A–D,
- status ODKRYCIE / TEST.

Etap 2 nie może sam przepisać parsera.

## ETAP 3 — UCZENIE MIĘDZY ŹRÓDŁAMI

Kandydaci Observera będą oceniani przez niezależne źródła. Przykład: Fortuna i Betcris potwierdzają `3:2`, a jeden element Superbet również pokazuje `3:2` i po golu przechodzi na `3:3` — jego confidence rośnie.

Planowane składowe oceny:

- zgodność między źródłami,
- stabilność czasowa,
- reakcja na gol/zmianę rynku,
- lokalność względem właściwej karty meczu,
- brak kolizji z sąsiednimi kartami.

Nadal bez automatycznej zmiany parsera.

## ETAP 4 — ANALITYKA LIVE

Macierze m.in.:

- minuta × gole,
- minuta × linia,
- linia × kurs,
- linia × czas od gola,
- przewaga × linia,
- sekwencja linii × przyszłe 30/60/90 s,
- stan meczu → FT.

Analiza korzysta wyłącznie z odpowiednich `ready_*`.

## ETAP 5 — REWANŻE / SERIE PAR

Rozbudowa zmiennych:

- `pair_encounter_no`,
- `seconds_since_previous_pair_match`,
- poprzedni FT,
- poprzedni HT,
- poprzedni zwycięzca,
- średnia serii,
- Last3 / Last5,
- profil po krótkiej i długiej przerwie.

Kolejne mecze tej samej pary są osobnymi realnymi próbami, a nie duplikatami.

## ETAP 6 — AUTOMATYCZNE ODKRYWANIE MARKERÓW

Co określoną liczbę nowych meczów system skanuje kombinacje zmiennych i zapisuje nowe hipotezy do LABORATORIUM.

Automatyczne statusy tylko:

`ODKRYCIE → TEST → POTWIERDZANIE`

Bez automatycznego przejścia do strategii.

## ETAP 7 — WALIDACJA STRATEGII

Rozdzielenie trafialności od opłacalności:

- kurs wejścia,
- marża/podatek,
- ROI,
- out-of-sample,
- stabilność między dniami/sesjami,
- minimalna próbka.

Docelowy przepływ:

`ODKRYCIE → TEST → POTWIERDZANIE → AKTUALNY → KANDYDAT STRATEGII → STRATEGIA`

albo `ODRZUCONY`.

Wysoka trafialność bez dodatniego edge pozostaje markerem tempa, a nie strategią.
