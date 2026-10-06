# Audyt PHONE — ETAP 3

## Cel
Dodać wybór folderu Android i obsługę karty SD bez przenoszenia aktywnej bazy SQLite na nośnik zewnętrzny.

## Architektura
- Źródło prawdy nadal: prywatna baza `przewijak_core/database/przewijak.sqlite3`.
- Użytkownik wybiera folder przez `ACTION_OPEN_DOCUMENT_TREE`.
- Aplikacja zapisuje persistable URI permission.
- Eksport tworzy najpierw spójny snapshot bazy przez `sqlite3.Connection.backup()`.
- Następnie Android kopiuje snapshot oraz pliki eksportu do `<wybrany folder>/PrzewijakLIVE/` przez `DocumentsContract`.
- Nie używa surowej ścieżki `/storage/...`, więc działa zgodnie z modelem pamięci Androida i może obsługiwać kartę SD wybraną przez użytkownika.

## Bezpieczeństwo danych
- Główna baza nie jest przenoszona ani otwierana bezpośrednio na karcie SD.
- Snapshot jest wykonywany pod blokadą bridge, więc ingest nie zapisuje równolegle podczas przygotowania kopii.
- SQLite backup jest spójny również przy aktywnym WAL.
- Usunięcie wyboru folderu nie kasuje danych już wyeksportowanych.

## Zakres poza Etapem 3
- foreground service / praca przy zgaszonym ekranie,
- testy soak,
- Observer DOM Android,
- cross-source intelligence.
