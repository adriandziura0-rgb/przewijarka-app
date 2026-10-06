# PLAN PHONE

- ETAP 1 — natywny szkielet Android: ZREALIZOWANY.
- ETAP 2 — RDZEŃ 2.9.16 + Android Browser Collector + heartbeat/ACK: ZREALIZOWANY W KODZIE, wymaga E2E na prawdziwym telefonie.
- ETAP 3 — Storage Access Framework: wybór folderu / SD + trwałe uprawnienie + spójny eksport SQLite: ZREALIZOWANY W KODZIE.
- ETAP 4 — foreground service, odporność na wygaszenie i przejście w tło.
- ETAP 5 — soak 30 min / 2 h / 6 h, Wi-Fi/LTE/reconnect.
- ETAP 6 — Observer DOM Android tylko do odczytu.
- ETAP 7 — uczenie między źródłami i dalsza Warstwa Inteligentna.

Zasada: nie zmieniamy zamrożonego RDZENIA tylko po to, aby dopasować Android. Różni się warstwa transportowa i magazyn eksportu.
