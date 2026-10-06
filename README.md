# Przewijak PHONE Stage 3

Normalne repozytorium projektu Android używanego do budowy Przewijak_PHONE_STAGE3.apk.

## Budowanie

GitHub Actions uruchamia .github/workflows/build-apk.yml po każdym pushu do main oraz ręcznie.

Projekt korzysta z:
- Java 17
- Android SDK 35
- Gradle 8.9
- Python 3.10 / Chaquopy

Przed kompilacją uruchamiany jest SELFTEST_PHONE_STAGE3.py.

## Pochodzenie

Źródła zostały odtworzone 1:1 z paczki użytej w poprawnym buildzie Stage 3, a następnie rozłożone do normalnej struktury repozytorium. Stary ap.zip.b64 został usunięty z main.

Punkt cofnięcia sprzed migracji: backup/pre-normalize-20261007.
