from __future__ import annotations

import json
import shutil
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from live_engine import EngineConfig, LiveEngine

_lock = threading.RLock()
_engine: LiveEngine | None = None
_base_dir: Path | None = None


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def init(base_dir: str, snapshot_interval: float = 5.0, log_odds_changes: bool = False) -> str:
    global _engine, _base_dir
    with _lock:
        target = Path(str(base_dir)) / "przewijak_core"
        target.mkdir(parents=True, exist_ok=True)
        if _engine is None or _base_dir != target:
            if _engine is not None:
                try:
                    _engine.stop()
                except Exception:
                    pass
                try:
                    _engine.db.close()
                except Exception:
                    pass
            _base_dir = target
            _engine = LiveEngine(target, EngineConfig(
                snapshot_interval=float(snapshot_interval),
                log_odds_changes=bool(log_odds_changes),
            ))
        else:
            _engine.configure(
                snapshot_interval=float(snapshot_interval),
                log_odds_changes=bool(log_odds_changes),
            )
        return _json({"ok": True, "base_dir": str(target), "status": _engine.status()})


def _get() -> LiveEngine:
    if _engine is None:
        raise RuntimeError("RDZEŃ nie został zainicjalizowany")
    return _engine


def start() -> str:
    with _lock:
        return _json({"ok": True, "live": _get().start()})


def stop() -> str:
    with _lock:
        return _json({"ok": True, "live": _get().stop()})


def configure(snapshot_interval: float, log_odds_changes: bool) -> str:
    with _lock:
        return _json({"ok": True, "live": _get().configure(
            snapshot_interval=float(snapshot_interval),
            log_odds_changes=bool(log_odds_changes),
        )})


def heartbeat(payload_json: str) -> str:
    payload = json.loads(payload_json or "{}")
    with _lock:
        return _json(_get().heartbeat(payload))


def ingest(payload_json: str) -> str:
    payload = json.loads(payload_json or "{}")
    with _lock:
        return _json(_get().ingest(payload))


def status() -> str:
    with _lock:
        return _json({"ok": True, "live": _get().status()})


def prepare_export(cache_dir: str) -> str:
    """Create a consistent read-only export snapshot without moving the live DB.

    The live SQLite database stays in app-private storage. A SQLite backup plus
    current exported files are staged in cache and can then be copied by Android
    Storage Access Framework to the user-selected folder/SD card.
    """
    with _lock:
        eng = _get()
        if _base_dir is None:
            raise RuntimeError("Brak katalogu RDZENIA")
        root = Path(str(cache_dir)) / "przewijak_export_snapshot"
        if root.exists():
            shutil.rmtree(root, ignore_errors=True)
        (root / "database").mkdir(parents=True, exist_ok=True)

        # Refresh regular CSV/JSON exports first when possible. This does not
        # change the source-of-truth identity or parser state.
        try:
            eng.db.export_global_views()
        except Exception:
            pass

        # SQLite online backup produces a coherent DB even when WAL is active.
        db_out = root / "database" / "przewijak.sqlite3"
        with eng.db.lock:
            dest = sqlite3.connect(db_out)
            try:
                eng.db.conn.backup(dest)
                dest.commit()
            finally:
                dest.close()

        # Copy non-database exports/sessions. Skip volatile WAL/SHM and any
        # previous temporary export snapshots.
        for child in _base_dir.iterdir():
            if child.name == "database":
                continue
            target = root / child.name
            if child.is_dir():
                shutil.copytree(child, target, dirs_exist_ok=True)
            elif child.is_file():
                shutil.copy2(child, target)

        manifest = {
            "exported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "core": "2.9.16 FROZEN",
            "database": "database/przewijak.sqlite3",
            "mode": "ANDROID_SAF_SNAPSHOT",
            "source_of_truth_location": "app-private storage",
        }
        (root / "EXPORT_MANIFEST.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return _json({"ok": True, "snapshot_dir": str(root), "manifest": manifest})
