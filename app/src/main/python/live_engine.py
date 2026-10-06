from __future__ import annotations

import csv
import json
import hashlib
import os
import re
import threading
import time
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from typing import Dict, List, Optional

from live_core import CanonicalLiveState, merge_layout_scores, parse_metadata_records, score_pair
from gold_quality import pair_uid, source_match_uid, record_completeness, source_health
from semantic_quality import (
    cross_source_pair_key, semantic_filter, semantic_score, stable_pair_key
)
from live_database import LiveDatabase, readiness_flags, format_seconds, market_sanity


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def safe(v):
    return "" if v is None else v


def human_source_label(source: str, rec: dict) -> str:
    """Return the configured human label; never replace SUPERBET/FORTUNA with parser IDs."""
    label = str(rec.get("_source_label") or "").strip()
    parser = str(rec.get("_parser_family") or "").strip()
    machine_like = bool(re.fullmatch(r"source[a-z0-9_-]+", label, re.I))
    if not label or machine_like:
        label = parser.upper() if parser else str(source or "source")
    return label


def record_key(rec: dict) -> str:
    return str(rec.get("key") or f"{rec.get('league','')}|{rec.get('player1','')}|{rec.get('player2','')}")


def expected_parser_family(url: str, reported: str = "auto") -> str:
    """Return the parser family that is compatible with the actual page host.

    The URL wins over a stale/incorrect client-side parser label. This prevents
    a betsport.pl page from being accepted as a Fortuna DOM layout.
    """
    try:
        host = (urlparse(str(url or "")).hostname or "").casefold()
    except Exception:
        host = ""
    if host == "betcris.com" or host.endswith(".betcris.com"):
        return "betcris"
    if host == "betsport.pl" or host.endswith(".betsport.pl"):
        return "betcris_pl"
    if "efortuna.pl" in host or host.endswith("fortuna.pl"):
        return "fortuna"
    if host == "superbet.pl" or host.endswith(".superbet.pl"):
        return "superbet"
    r = str(reported or "auto").strip().casefold()
    return r if r in {"fortuna", "superbet", "betcris", "betcris_pl"} else "auto"


def layout_adapter_compatible(expected: str, records: list) -> bool:
    adapters = {str((r or {}).get("_layout_adapter") or "").casefold() for r in (records or []) if isinstance(r, dict)}
    adapters.discard("")
    if not adapters or expected == "auto":
        return True
    if expected == "fortuna":
        return all(a.startswith("fortuna") for a in adapters)
    if expected == "superbet":
        return all(a.startswith("superbet") for a in adapters)
    if expected == "betcris":
        return all(a.startswith("betcris") and not a.startswith("betcris_pl") for a in adapters)
    if expected == "betcris_pl":
        return all(a.startswith("betcris_pl") for a in adapters)
    return True


@dataclass
class EngineConfig:
    snapshot_interval: float = 5.0
    log_odds_changes: bool = False
    max_raw_evidence: int = 30


class LiveEngine:
    """Local live data engine.

    It receives DOM/layout snapshots from the dedicated Edge/Chromium collector.
    This class is the single source of truth for accepted LIVE state.
    """

    SNAP_FIELDS = [
        "timestamp", "session", "key", "league", "format", "status", "half", "minute",
        "team1", "player1", "team2", "player2", "score1", "score2", "total_goals",
        "line", "under_odds", "over_odds", "score_quality", "score_source",
        "score_confidence", "score_confirmations", "score_transition", "analysis_allowed",
        "ready_score", "ready_clock", "ready_market", "ready_live_model", "ready_ft",
        "pair_uid", "match_uid", "master_match_uid",
        "final_confirmed", "final_score", "finish_reason",
        "data_completeness", "source_url",
        "source", "source_label", "parser_family", "collector_id", "packet_seq"
    ]
    CHANGE_FIELDS = [
        "timestamp", "session", "key", "league", "player1", "player2", "field",
        "old", "new", "score", "status", "minute", "pair_uid", "match_uid", "master_match_uid",
        "source_url", "source", "source_label", "parser_family", "collector_id", "packet_seq"
    ]
    GOAL_FIELDS = [
        "timestamp_detected", "session", "key", "league", "player1", "player2",
        "score_old", "score_new", "delta_goals", "transition", "window_start",
        "window_end", "uncertainty_seconds", "status", "minute", "pair_uid", "match_uid", "master_match_uid",
        "source_url", "source", "source_label", "parser_family", "collector_id", "packet_seq"
    ]
    ANOM_FIELDS = [
        "first_seen", "last_seen", "session", "master_match_uid", "source", "source_label",
        "parser_family", "type", "old_value", "new_value", "occurrences", "example_reason", "evidence_file"
    ]
    TIMELINE_FIELDS = [
        "timestamp", "session", "key", "league", "player1", "team1", "player2", "team2",
        "event_type", "field", "old", "new", "score", "scorer", "status", "minute",
        "uncertainty_seconds", "reason", "pair_uid", "match_uid", "master_match_uid", "source_url", "source", "source_label", "parser_family", "collector_id", "packet_seq"
    ]
    MATCH_SUMMARY_FIELDS = [
        "session", "master_match_uid", "match_uid", "pair_uid",
        "league", "player1", "team1", "player2", "team2", "first_seen", "last_seen",
        "final_confirmed", "final_score", "finish_reason", "source_ids", "source_labels", "semantic_score",
        "ready_score", "ready_clock", "ready_market", "ready_live_model", "ready_ft"
    ]

    def __init__(self, base_dir: Path, config: Optional[EngineConfig] = None):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.db = LiveDatabase(self.base_dir)
        self.config = config or EngineConfig()
        self.lock = threading.RLock()
        self.state = CanonicalLiveState(
            min_score_confidence=0.72,
            confirmations=2,
            catchup_confirmations=3,
            min_confirmation_gap=0.35,
        )
        self.running = False
        self.session_id = ""
        self.session_dir: Optional[Path] = None
        self.raw_dir: Optional[Path] = None
        self.current: Dict[str, dict] = {}
        self.previous_for_events: Dict[str, dict] = {}
        self.last_ingest_wall = 0.0
        self.last_ingest_iso = ""
        self.last_url = ""
        self.last_title = ""
        self.collector_count = 0
        self.saved_snapshot_batches = 0
        self.saved_snapshot_rows = 0
        self.goal_count = 0
        self.change_count = 0
        self.anomaly_count = 0
        self.anomaly_agg: Dict[tuple, dict] = {}
        self.ingest_count = 0
        self.last_snapshot_mono = 0.0
        self.evidence_signatures = set()
        self.recent_events: List[dict] = []
        self.last_error = ""
        # Niezależny stan każdej strony źródłowej. Dzięki temu różne źródła
        # mogą działać jednocześnie bez nadpisywania tych samych par.
        self.states: Dict[str, CanonicalLiveState] = {}
        self.current_by_source: Dict[str, Dict[str, dict]] = {}
        self.source_status: Dict[str, dict] = {}
        self.last_snapshot_mono_by_source: Dict[str, float] = {}
        self.seen_packet_ids = set()
        self.seen_packet_order: List[str] = []
        self.last_seq_by_collector: Dict[str, int] = {}
        self.packet_duplicates = 0
        self.packet_gaps = 0
        self.packet_recovered = 0
        self.packet_stale = 0
        self.match_uid_by_key: Dict[str, str] = {}
        self.match_generation_by_key: Dict[str, int] = {}
        self.match_last_seen_mono: Dict[str, float] = {}
        self.match_meta: Dict[str, dict] = {}
        self.last_raw_snapshot_id: Dict[str, int] = {}
        self.new_match_gap_seconds = 90.0
        self.finish_grace_seconds = 20.0
        self.resource_sample_interval_seconds = 60.0
        self._resource_last_wall = 0.0
        self._resource_last_process = time.process_time()
        self._resource_last_sample: dict = {}

    def start(self) -> dict:
        with self.lock:
            if self.running:
                return self.status()
            # Milisekundy + kontrola kolizji: szybki STOP→START nie może dopisać
            # nowej sesji do katalogu poprzedniej sesji uruchomionej w tej samej sekundzie.
            dt = datetime.now().astimezone()
            stamp = dt.strftime("%Y%m%d_%H%M%S_") + f"{dt.microsecond // 1000:03d}"
            base_session_id = f"sesja_{stamp}"
            self.session_id = base_session_id
            self.session_dir = self.base_dir / "live" / self.session_id
            suffix = 2
            while self.session_dir.exists():
                self.session_id = f"{base_session_id}_{suffix}"
                self.session_dir = self.base_dir / "live" / self.session_id
                suffix += 1
            self.raw_dir = self.session_dir / "raw"
            self.raw_dir.mkdir(parents=True, exist_ok=True)
            self.state.reset()
            self.current.clear()
            self.previous_for_events.clear()
            self.last_ingest_wall = 0.0
            self.last_ingest_iso = ""
            self.last_url = ""
            self.last_title = ""
            self.saved_snapshot_batches = 0
            self.saved_snapshot_rows = 0
            self.goal_count = 0
            self.change_count = 0
            self.anomaly_count = 0
            self.anomaly_agg.clear()
            self.ingest_count = 0
            self.last_snapshot_mono = 0.0
            self.evidence_signatures.clear()
            self.recent_events.clear()
            self.last_error = ""
            self.states.clear()
            self.current_by_source.clear()
            self.source_status.clear()
            self.last_snapshot_mono_by_source.clear()
            self.seen_packet_ids.clear()
            self.seen_packet_order.clear()
            self.last_seq_by_collector.clear()
            self.packet_duplicates = 0
            self.packet_gaps = 0
            self.packet_recovered = 0
            self.packet_stale = 0
            self.match_uid_by_key.clear()
            self.match_generation_by_key.clear()
            self.match_last_seen_mono.clear()
            self.match_meta.clear()
            self.last_raw_snapshot_id.clear()
            self._resource_last_wall = 0.0
            self._resource_last_process = time.process_time()
            self._resource_last_sample = {}
            self.running = True
            self._write_diag()
            return self.status()

    def stop(self) -> dict:
        with self.lock:
            self.running = False
            for meta in self.match_meta.values():
                if not meta.get("finish_reason"):
                    meta["final_confirmed"] = False
                    meta["finish_reason"] = "engine_stop_unconfirmed"
                    meta["final_score"] = meta.get("last_score") or ""
                    try:
                        srcs = meta.get("_source_ids_set") or set()
                        source_id = sorted(srcs)[0] if isinstance(srcs, set) and srcs else ""
                        self.db.mark_disappeared(meta.get("master_match_uid"), source_id, reason="engine_stop_unconfirmed")
                    except Exception:
                        pass
            self._write_match_summary()
            self._write_diag(final=True)
            try:
                self.db.export_global_views()
            except Exception as db_exc:
                self.last_error = f"sqlite export: {type(db_exc).__name__}: {db_exc}"
            return self.status()

    def close(self):
        """Release the persistent SQLite connection when this engine is replaced."""
        with self.lock:
            if self.running:
                self.stop()
            self.db.close()

    def _append_csv(self, path: Path, fields: List[str], row: dict):
        path.parent.mkdir(parents=True, exist_ok=True)
        exists = path.exists() and path.stat().st_size > 0
        # BOM only once, never before every appended row.
        enc = "utf-8" if exists else "utf-8-sig"
        with path.open("a", encoding=enc, newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields, delimiter=";", extrasaction="ignore")
            if not exists:
                w.writeheader()
            w.writerow({k: safe(row.get(k)) for k in fields})

    def _append_jsonl(self, path: Path, obj: dict):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")

    def _flat_record(self, rec: dict, ts: Optional[str] = None) -> dict:
        readiness_rec = dict(rec)
        if rec.get("_trusted_clock") is False:
            for fld in ("status", "half", "minute", "clock_second_in_period", "match_second", "elapsed_seconds"):
                readiness_rec[fld] = None
        if rec.get("_trusted_market") is False:
            readiness_rec["line"] = readiness_rec["under_odds"] = readiness_rec["over_odds"] = None
        flags = readiness_flags(readiness_rec, bool(rec.get("_final_confirmed", False)))
        if not bool(rec.get("_analysis_allowed", True)):
            flags = {k: 0 for k in flags}
        return {
            "timestamp": ts or now_iso(),
            "session": self.session_id,
            "key": record_key(rec),
            "league": rec.get("league"),
            "format": rec.get("format"),
            "status": rec.get("status"),
            "half": rec.get("half"),
            "minute": rec.get("minute"),
            "team1": rec.get("team1"),
            "player1": rec.get("player1"),
            "team2": rec.get("team2"),
            "player2": rec.get("player2"),
            "score1": rec.get("score1"),
            "score2": rec.get("score2"),
            "total_goals": rec.get("total_goals"),
            "line": rec.get("line"),
            "under_odds": rec.get("under_odds"),
            "over_odds": rec.get("over_odds"),
            "score_quality": rec.get("_score_quality"),
            "score_source": rec.get("_score_source"),
            "score_confidence": rec.get("_score_confidence"),
            "score_confirmations": rec.get("_score_confirmations"),
            "score_transition": rec.get("_score_transition"),
            "analysis_allowed": bool(rec.get("_analysis_allowed", True)),
            "ready_score": flags["ready_score"],
            "ready_clock": flags["ready_clock"],
            "ready_market": flags["ready_market"],
            "ready_live_model": flags["ready_live_model"],
            "ready_ft": flags["ready_ft"],
            "pair_uid": rec.get("_pair_uid"),
            "match_uid": rec.get("_match_uid"),
            "master_match_uid": rec.get("_master_match_uid"),
            "final_confirmed": rec.get("_final_confirmed", False),
            "final_score": rec.get("_final_score", ""),
            "finish_reason": rec.get("_finish_reason", ""),
            "data_completeness": rec.get("_data_completeness"),
            "source_url": rec.get("_source_url") or self.last_url,
            "source": rec.get("_source_platform") or "fortuna",
            "source_label": rec.get("_source_label") or rec.get("_source_platform") or "fortuna",
            "parser_family": rec.get("_parser_family") or "",
            "collector_id": rec.get("_collector_id") or "",
            "packet_seq": rec.get("_packet_seq") if rec.get("_packet_seq") is not None else "",
        }

    @staticmethod
    def _score_text(rec: Optional[dict]) -> str:
        p = score_pair(rec or {})
        return "" if p is None else f"{p[0]}:{p[1]}"

    def _new_state(self) -> CanonicalLiveState:
        return CanonicalLiveState(
            min_score_confidence=0.72,
            confirmations=2,
            catchup_confirmations=3,
            min_confirmation_gap=0.35,
        )

    @staticmethod
    def _source_name(payload: dict) -> str:
        src = str(payload.get("source_id") or payload.get("platform") or "fortuna").strip().lower()
        return re.sub(r"[^a-z0-9._-]+", "_", src)[:40] or "fortuna"

    @staticmethod
    def _collector_key(payload: dict, source: str) -> str:
        # Sequence numbers are local to one collector runtime. Include source and
        # collector_session so two tabs/sources can never mark each other STALE,
        # even if a browser restores the same tab/collector id after a crash.
        collector = str(payload.get("collector_id") or payload.get("tab_id") or "")
        session = str(payload.get("collector_session") or "")
        return f"{source}|{collector}|{session}"

    def _remember_packet(self, packet_id: str):
        if not packet_id or packet_id in self.seen_packet_ids:
            return
        self.seen_packet_ids.add(packet_id)
        self.seen_packet_order.append(packet_id)
        if len(self.seen_packet_order) > 6000:
            old = self.seen_packet_order.pop(0)
            self.seen_packet_ids.discard(old)

    def heartbeat(self, payload: dict) -> dict:
        """Lekki heartbeat oddzielony od danych DOM.

        Pozwala odróżnić: karta kolektora żyje, ale nie widzi meczów, od sytuacji
        w której kolektor został faktycznie wstrzymany przez przeglądarkę lub system.
        """
        with self.lock:
            source = self._source_name(payload)
            now = time.time()
            st = self.source_status.setdefault(source, {})
            st.update({
                "source": source,
                "source_label": str(payload.get("source_label") or source),
                "parser_family": str(payload.get("parser_family") or payload.get("platform") or "auto"),
                "collect": payload.get("collect") if isinstance(payload.get("collect"), dict) else {},
                "last_heartbeat_wall": now,
                "last_heartbeat": now_iso(),
                "collector_id": str(payload.get("collector_id") or payload.get("tab_id") or ""),
                "collector_session": str(payload.get("collector_session") or ""),
                "tab_id": str(payload.get("tab_id") or ""),
                "visibility": str(payload.get("visibility") or ""),
                "focused": bool(payload.get("focused", False)),
                "queue_depth": int(payload.get("queue_depth") or 0),
                "queue_dropped": int(payload.get("queue_dropped") or 0),
                "queue_recovered": int(payload.get("queue_recovered") or 0),
                "last_error": str(payload.get("last_error") or ""),
                "url": str(payload.get("url") or st.get("url") or ""),
                "title": str(payload.get("title") or st.get("title") or ""),
            })
            try:
                self.db.update_source_heartbeat(source, st.get("last_heartbeat"))
            except Exception:
                pass
            return {
                "ok": True, "kind": "heartbeat", "source": source,
                "server_session": self.session_id, "server_running": self.running,
                "server_time": now_iso(),
            }

    def _cross_source_confirms_score(self, source: str, rec: dict, cand1: int, cand2: int) -> bool:
        """Accept an ambiguous Superbet score only when a fresh independent source agrees."""
        direct = stable_pair_key(rec.get("player1"), rec.get("player2"), rec.get("team1"), rec.get("team2"))
        reverse = stable_pair_key(rec.get("player2"), rec.get("player1"), rec.get("team2"), rec.get("team1"))
        for other_source, source_map in self.current_by_source.items():
            if other_source == source:
                continue
            meta = self.source_status.get(other_source, {})
            try:
                if self.last_ingest_wall - float(meta.get("last_data_wall") or 0.0) > 8.0:
                    continue
            except Exception:
                continue
            for other in source_map.values():
                if not bool(other.get("_analysis_allowed", True)):
                    continue
                if str(other.get("_parser_family") or "").casefold() == "betcris_pl" and not bool(other.get("_active_card_bound", False)):
                    continue
                try:
                    if float(other.get("_score_confidence") or 0.0) < 0.82:
                        continue
                except Exception:
                    continue
                if other.get("score1") is None or other.get("score2") is None:
                    continue
                okey = stable_pair_key(other.get("player1"), other.get("player2"), other.get("team1"), other.get("team2"))
                oscore = (int(other.get("score1")), int(other.get("score2")))
                if okey == direct and oscore == (int(cand1), int(cand2)):
                    return True
                if okey == reverse and oscore == (int(cand2), int(cand1)):
                    return True
        return False

    @staticmethod
    def _status_final_score(status: str) -> tuple[Optional[int], Optional[int]]:
        text = str(status or "")
        if not re.search(r"(?:finished|final|zako[nń]cz|koniec|full\s*time|\bft\b)", text, re.I):
            return None, None
        pairs = re.findall(r"(?<!\d)(\d{1,2})\s*[:\-]\s*(\d{1,2})(?!\d)", text)
        if not pairs:
            return None, None
        a, b = pairs[-1]
        return int(a), int(b)

    def ingest(self, payload: dict) -> dict:
        with self.lock:
            source = self._source_name(payload)
            collector_key = self._collector_key(payload, source)
            packet_id = str(payload.get("packet_id") or "")
            try:
                packet_seq = int(payload.get("seq")) if payload.get("seq") is not None else None
            except Exception:
                packet_seq = None

            self.collector_count += 1
            self.last_ingest_wall = time.time()
            self.last_ingest_iso = now_iso()
            self.last_url = str(payload.get("url") or self.last_url or "")
            self.last_title = str(payload.get("title") or self.last_title or "")

            st = self.source_status.setdefault(source, {})
            st.update({
                "source": source,
                "source_label": str(payload.get("source_label") or source),
                "parser_family": str(payload.get("parser_family") or payload.get("platform") or "auto"),
                "collect": payload.get("collect") if isinstance(payload.get("collect"), dict) else {},
                "last_heartbeat_wall": self.last_ingest_wall,
                "last_heartbeat": self.last_ingest_iso,
                "collector_id": collector_key,
                "collector_session": str(payload.get("collector_session") or ""),
                "tab_id": str(payload.get("tab_id") or ""),
                "visibility": str(payload.get("visibility") or ""),
                "focused": bool(payload.get("focused", False)),
                "queue_depth": int(payload.get("queue_depth") or 0),
                "queue_dropped": int(payload.get("queue_dropped") or 0),
                "queue_recovered": int(payload.get("queue_recovered") or 0),
                "url": self.last_url, "title": self.last_title,
            })
            try:
                self.db.update_source_heartbeat(source, self.last_ingest_iso)
            except Exception:
                pass
            try:
                created_ms = float(payload.get("created_ms") or 0)
                if created_ms > 0:
                    packet_age_ms = max(0.0, time.time() * 1000.0 - created_ms)
                    st["last_packet_age_ms"] = round(packet_age_ms, 1)
                    prev_ema = float(st.get("packet_age_ema_ms") or packet_age_ms)
                    st["packet_age_ema_ms"] = round(prev_ema * 0.85 + packet_age_ms * 0.15, 1)
            except Exception:
                pass

            # Idempotentny ACK: retransmisja tego samego pakietu po timeout nie może
            # wygenerować drugiego gola ani drugiej zmiany kursu.
            if packet_id and packet_id in self.seen_packet_ids:
                self.packet_duplicates += 1
                st["duplicates"] = int(st.get("duplicates") or 0) + 1
                return {
                    "ok": True, "duplicate": True, "ack_packet_id": packet_id,
                    "ack_seq": packet_seq, "source": source,
                    "server_session": self.session_id, "running": self.running,
                }

            pending_seq_gap = 0
            if packet_seq is not None:
                prev = self.last_seq_by_collector.get(collector_key)
                # Pakiet o starszym/tym samym numerze, ale innym packet_id, może dojść
                # z bufora po nowszym. Nie wolno nim cofnąć minuty, linii ani statusu.
                if prev is not None and packet_seq <= prev:
                    self.packet_stale += 1
                    st["stale_packets"] = int(st.get("stale_packets") or 0) + 1
                    self._remember_packet(packet_id)
                    return {
                        "ok": True, "stale": True, "ack_packet_id": packet_id,
                        "ack_seq": packet_seq, "source": source,
                        "server_session": self.session_id, "running": self.running,
                    }
                if prev is not None and packet_seq > prev + 1:
                    pending_seq_gap = packet_seq - prev - 1

            def _ack_packet():
                # Numer sekwencji staje się zatwierdzony dopiero razem z ACK.
                # Dzięki temu błąd zapisu SQLite nie zamienia ponownej próby
                # w fałszywy pakiet STALE i nie gubi danych z kolejki mobilnej.
                if packet_seq is not None:
                    self.last_seq_by_collector[collector_key] = packet_seq
                    if pending_seq_gap:
                        self.packet_gaps += pending_seq_gap
                        st["seq_gaps"] = int(st.get("seq_gaps") or 0) + pending_seq_gap
                self._remember_packet(packet_id)

            if bool(payload.get("from_buffer")):
                self.packet_recovered += 1
                st["recovered_packets"] = int(st.get("recovered_packets") or 0) + 1

            if not self.running:
                _ack_packet()
                return {
                    "ok": True, "running": False, "matches": len(self.current),
                    "ack_packet_id": packet_id, "ack_seq": packet_seq,
                    "source": source, "server_session": self.session_id,
                }

            self.ingest_count += 1
            mono = time.monotonic()
            try:
                text = str(payload.get("full_text") or "")
                layout = payload.get("layout_records") or []
                source_url = str(payload.get("url") or "")
                parser_family = expected_parser_family(source_url, payload.get("parser_family") or payload.get("platform"))
                st["parser_family"] = parser_family
                if not layout_adapter_compatible(parser_family, layout):
                    st["data_state"] = "PARSER_MISMATCH"
                    st["last_error"] = f"parser mismatch: oczekiwano {parser_family}"
                    self.last_error = st["last_error"]
                    _ack_packet()
                    self._write_diag()
                    return {
                        "ok": True, "running": True, "hold": True,
                        "matches": len(self.current_by_source.get(source, {})),
                        "observed_matches": len(layout),
                        "ack_packet_id": packet_id, "ack_seq": packet_seq,
                        "source": source, "server_session": self.session_id,
                        "warning": st["last_error"],
                    }
                metadata = [] if parser_family in {"betcris", "betcris_pl"} else parse_metadata_records(text)
                raw_records = merge_layout_scores(metadata, layout)
                # Superbet pokazuje minutę jako absolutną minutę całego krótkiego meczu
                # również w tekstowym fallbacku. Gdy geometria DOM chwilowo nie zadziała,
                # nie wolno wracać do semantyki "minuta w połowie".
                if parser_family == "superbet":
                    for rr in raw_records:
                        if rr.get("minute") not in (None, "") and not rr.get("_clock_mode"):
                            rr["_clock_mode"] = "absolute_match_minute"
                raw_records, sem = semantic_filter(raw_records, parser_family, source_url)
                for rr in raw_records:
                    rr["_parser_family"] = parser_family
                    rr["_source_label"] = str(payload.get("source_label") or source)
                # A /match/ page represents one concrete fixture. Never accept a forest
                # of adjacent page rows as separate matches from that page.
                if parser_family == "betcris_pl" and "/match/" in source_url.casefold() and len(raw_records) > 1:
                    def _candidate_quality(r):
                        return (
                            float(r.get("_score_confidence") or 0.0),
                            sum(r.get(k) not in (None, "") for k in ("status", "minute", "line", "under_odds", "over_odds")),
                        )
                    raw_records = [max(raw_records, key=_candidate_quality)]
                    sem["accepted"] = 1
                    sem["rejected"] = max(sem.get("rejected", 0), sem.get("observed", 0) - 1)
                    sem["acceptance"] = round(1 / max(1, sem.get("observed", 1)), 3)
                    sem.setdefault("reasons", {})["single_match_scope"] = max(0, sem.get("observed", 0) - 1)
                st["semantic_observed"] = int(st.get("semantic_observed") or 0) + int(sem.get("observed") or 0)
                st["semantic_accepted"] = int(st.get("semantic_accepted") or 0) + int(sem.get("accepted") or 0)
                st["semantic_rejected"] = int(st.get("semantic_rejected") or 0) + int(sem.get("rejected") or 0)
                st["team_cleanups"] = int(st.get("team_cleanups") or 0) + int(sem.get("team_cleanups") or 0)
                st["bad_context"] = int(st.get("bad_context") or 0) + int(sem.get("bad_context") or 0)
                st["semantic_reasons"] = sem.get("reasons") or {}
                collect = payload.get("collect") if isinstance(payload.get("collect"), dict) else {}
                for rr in raw_records:
                    if collect.get("score") is False:
                        rr["score1"] = rr["score2"] = rr["total_goals"] = None
                        rr["_score_source"] = "disabled"
                        rr["_score_confidence"] = 0.0
                    if collect.get("clock") is False:
                        rr["status"] = rr["half"] = rr["minute"] = None
                    if collect.get("market") is False:
                        rr["line"] = rr["under_odds"] = rr["over_odds"] = None

                # Nagłe 0 rekordów przy wcześniej działającym źródle to HOLD, nie
                # "zero aktywnych meczów". Chroni historię przy rerenderze/awarii strony.
                previous_source = self.current_by_source.get(source, {})
                wrong_sport = bool((sem.get("reasons") or {}).get("not_esoccer_url") or (sem.get("reasons") or {}).get("not_esoccer"))
                if wrong_sport and not raw_records:
                    st["zero_streak"] = int(st.get("zero_streak") or 0) + 1
                    st["data_state"] = "WRONG_SPORT"
                    st["last_error"] = "Źródło Betcris/Betsport nie wygląda na E-Football/eSoccer — rekordy odrzucone."
                    _ack_packet()
                    self._write_diag()
                    return {
                        "ok": True, "running": True, "hold": True, "wrong_sport": True,
                        "matches": len(previous_source), "observed_matches": int(sem.get("observed") or 0),
                        "ack_packet_id": packet_id, "ack_seq": packet_seq,
                        "source": source, "server_session": self.session_id,
                        "warning": st["last_error"],
                    }
                if not raw_records and previous_source:
                    st["zero_streak"] = int(st.get("zero_streak") or 0) + 1
                    st["data_state"] = "HOLD_ZERO"
                    st["last_match_count"] = 0
                    _ack_packet()
                    self._write_diag()
                    return {
                        "ok": True, "running": True, "hold": True,
                        "matches": len(previous_source), "observed_matches": 0,
                        "ack_packet_id": packet_id, "ack_seq": packet_seq,
                        "source": source, "server_session": self.session_id,
                    }

                raw_by_logical = {
                    stable_pair_key(rr.get("player1"), rr.get("player2"), rr.get("team1"), rr.get("team2")): dict(rr)
                    for rr in raw_records
                }
                if parser_family == "superbet":
                    for rr in raw_records:
                        if not rr.get("_score_binding_suspect"):
                            continue
                        c1, c2 = rr.get("_score_candidate1"), rr.get("_score_candidate2")
                        try:
                            c1, c2 = int(c1), int(c2)
                        except (TypeError, ValueError):
                            continue
                        if self._cross_source_confirms_score(source, rr, c1, c2):
                            rr["score1"], rr["score2"] = c1, c2
                            rr["total_goals"] = c1 + c2
                            rr["_score_source"] = "cross_source_confirmed_geometry"
                            rr["_score_confidence"] = max(0.98, float(rr.get("_score_confidence") or 0.0))
                            rr["_cross_source_score_confirmed"] = True

                state = self.states.setdefault(source, self._new_state())
                # Zachowaj mały rollback stanu kanonicznego. Jest używany wyłącznie
                # gdy zapis snapshotu SQLite zawiedzie i pakiet ma zostać ponowiony.
                state_records_before = deepcopy(state.records)
                state_pending_before = deepcopy(state.pending_scores)
                for rr in raw_records:
                    logical = stable_pair_key(rr.get("player1"), rr.get("player2"), rr.get("team1"), rr.get("team2"))
                    source_logical = f"{source}|{logical}"
                    last_seen = self.match_last_seen_mono.get(source_logical)
                    if last_seen is not None and mono - last_seen >= self.new_match_gap_seconds:
                        state.records.pop(logical, None)
                        state.pending_scores.pop(logical, None)
                canonical, anomalies = state.reconcile(raw_records, mono)
            except Exception as e:
                self.last_error = f"ingest[{source}]: {type(e).__name__}: {e}"
                st["last_error"] = self.last_error
                self._write_diag()
                return {"ok": False, "error": self.last_error, "source": source}

            source_url = str(payload.get("url") or "")
            collect_cfg = payload.get("collect") if isinstance(payload.get("collect"), dict) else {}
            expected_parser = expected_parser_family(source_url, payload.get("parser_family") or payload.get("platform"))
            previous_source = self.current_by_source.get(source, {})
            identity_semantic_quality = int(round(100 * float(sem.get("acceptance", 1.0) or 0.0)))
            packet_captured_at = str(payload.get("collector_ts") or "").strip()
            try:
                packet_captured_ms = int(float(payload.get("created_ms") or 0))
            except Exception:
                packet_captured_ms = 0
            identity_before = (
                dict(self.match_uid_by_key), dict(self.match_generation_by_key), dict(self.match_last_seen_mono),
                deepcopy(self.match_meta), dict(self.last_raw_snapshot_id),
                self.goal_count, self.change_count, self.anomaly_count, list(self.recent_events), set(self.evidence_signatures),
            )
            old_map = self.current_by_source.get(source, {})
            new_map = {}
            try:
                with self.db.transaction():
                    for rec in canonical:
                        # Bufor mobilny może dostarczyć pakiet po restarcie serwera. Zachowujemy
                        # czas z chwili odczytu strony, a nie czas późniejszego replayu do SQLite.
                        if packet_captured_at:
                            rec["_captured_at"] = packet_captured_at
                        if packet_captured_ms > 0:
                            rec["_captured_at_ms"] = packet_captured_ms
                        original_key = stable_pair_key(rec.get("player1"), rec.get("player2"), rec.get("team1"), rec.get("team2"))
                        source_key = f"{source}|{original_key}"
                        rec["key"] = source_key
                        rec["_pair_uid"] = pair_uid(rec.get("league"), rec.get("player1"), rec.get("player2"))
                        rec["_semantic_pair_key"] = cross_source_pair_key(rec.get("player1"), rec.get("player2"), rec.get("team1"), rec.get("team2"))
                        rec["_data_completeness"] = record_completeness(rec, collect_cfg)

                        last_seen = self.match_last_seen_mono.get(source_key)
                        had_prior = source_key in self.match_uid_by_key
                        new_generation = not had_prior
                        generation_reason = "initial" if new_generation else ""
                        if last_seen is not None and mono - last_seen >= self.new_match_gap_seconds:
                            new_generation = True
                            generation_reason = "gap"
                        old_rec = previous_source.get(source_key)
                        old_score = score_pair(old_rec or {})
                        new_score = score_pair(rec)
                        raw_current = raw_by_logical.get(original_key) or {}
                        if packet_captured_at:
                            raw_current["_captured_at"] = packet_captured_at
                        if packet_captured_ms > 0:
                            raw_current["_captured_at_ms"] = packet_captured_ms
                        raw_score = score_pair(raw_current)
                        reset_detected = False
                        strong_raw_reset = False
                        if old_rec and new_score is not None:
                            if rec.get("_score_transition") == "reset" and new_score == (0, 0) and old_score and sum(old_score) > 0:
                                reset_detected = True
                            try:
                                if old_rec.get("half") == 2 and rec.get("half") == 1:
                                    reset_detected = True
                                elif old_rec.get("half") == rec.get("half") and old_rec.get("minute") is not None and rec.get("minute") is not None and int(rec.get("minute")) + 1 < int(old_rec.get("minute")) and new_score == (0, 0):
                                    reset_detected = True
                            except Exception:
                                pass

                        # Reconciler intentionally needs confirmations before accepting a score
                        # rollback. For fixture identity we cannot wait: a fresh 0:0 together
                        # with a real clock/half reset is strong evidence that the same pair has
                        # started the NEXT match. Split the source fixture immediately, while
                        # still keeping the raw observation for audit. A mere score correction
                        # to 0:0 without a clock/half reset does NOT create a new fixture.
                        if old_rec and old_score and sum(old_score) > 0 and raw_score == (0, 0):
                            try:
                                oh, rh = old_rec.get("half"), raw_current.get("half")
                                om, rm = old_rec.get("minute"), raw_current.get("minute")
                                if oh == 2 and rh == 1:
                                    strong_raw_reset = True
                                elif oh == rh and om is not None and rm is not None and int(rm) + 2 < int(om):
                                    strong_raw_reset = True
                            except Exception:
                                pass
                        if reset_detected or strong_raw_reset:
                            new_generation = True
                            generation_reason = "reset"
                        if new_generation:
                            gen = self.match_generation_by_key.get(source_key, 0) + 1
                            self.match_generation_by_key[source_key] = gen
                            self.match_uid_by_key[source_key] = source_match_uid(self.session_id, source, original_key, gen)
                        self.match_last_seen_mono[source_key] = mono
                        rec["_match_uid"] = self.match_uid_by_key[source_key]

                        parser_version = str(rec.get("_layout_adapter") or expected_parser or "auto")
                        identity_rec = dict(raw_current) if strong_raw_reset and raw_current else rec
                        # Preserve context that may live only on the canonical record.
                        for _field in ("league", "format", "team1", "team2", "_sport_valid", "_parser_family"):
                            if identity_rec.get(_field) in (None, "") and rec.get(_field) not in (None, ""):
                                identity_rec[_field] = rec.get(_field)
                        identity_rec["_parser_family"] = expected_parser
                        identity_rec["_generation_reason"] = generation_reason
                        identity_rec["_strong_new_match"] = bool(generation_reason == "reset")
                        master_uid, orientation = self.db.resolve_master(
                            session_id=self.session_id, source=source, source_match_uid=rec["_match_uid"], rec=identity_rec,
                            source_url=source_url, parser_version=parser_version, semantic_quality=identity_semantic_quality,
                            force_new=bool(had_prior and new_generation),
                        )
                        rec["_master_match_uid"] = master_uid
                        rec["_master_orientation"] = orientation
                        rec["_final_confirmed"] = False
                        rec["_final_score"] = ""
                        rec["_finish_reason"] = ""
                        rec["_source_platform"] = source
                        rec["_source_label"] = str(payload.get("source_label") or source)
                        rec["_parser_family"] = expected_parser
                        rec["_source_url"] = source_url
                        rec["_collector_id"] = collector_key
                        rec["_packet_seq"] = packet_seq
                        rec["_parser_version"] = parser_version
                        rec["_raw_input_record"] = raw_by_logical.get(original_key, dict(rec))
                        rec["_db_record_override"] = dict(identity_rec) if strong_raw_reset else None

                    new_map = {record_key(r): dict(r) for r in canonical}
                    old_map = self.current_by_source.get(source, {})
                    self.current_by_source[source] = new_map
                    self.current = {}
                    for src_map in self.current_by_source.values():
                        self.current.update(src_map)

                    self._update_match_lifecycle(source, new_map, mono, strict_db=True)

                    st["zero_streak"] = 0
                    st["data_state"] = "OK" if new_map else "EMPTY"
                    st["last_match_count"] = len(new_map)
                    st["observed_records"] = len(raw_records)
                    st["accepted_records"] = len(new_map)
                    if new_map:
                        st["avg_completeness"] = round(sum(float(r.get("_data_completeness") or 0) for r in new_map.values()) / len(new_map), 3)
                        confs = [float(r.get("_score_confidence") or 0) for r in new_map.values() if r.get("score1") is not None and r.get("score2") is not None]
                        st["avg_score_confidence"] = round(sum(confs) / len(confs), 3) if confs else 0.0
                    if new_map:
                        st["last_nonempty_wall"] = self.last_ingest_wall
                        st["last_nonempty"] = self.last_ingest_iso
                        st["last_data_wall"] = self.last_ingest_wall
                        st["last_data"] = self.last_ingest_iso

                    health_now = source_health(st, connection="online", heartbeat_age=0.0, data_age=0.0 if new_map else None, matches=list(new_map.values()))
                    transport_quality_now = int(health_now.get("health_score") or 0)
                    sem_meta_now = dict(st)
                    sem_meta_now["_semantic_data_age_seconds"] = 0.0 if new_map else None
                    source_semantic_quality_now = int(semantic_score(sem_meta_now, list(new_map.values())).get("semantic_score") or 0)
                    snapshot_db_errors = []
                    for rec in new_map.values():
                        adapter = str(rec.get("_layout_adapter") or "").casefold()
                        market_ok, market_reason = market_sanity(rec)
                        rec["_market_sanity_reason"] = market_reason
                        if expected_parser == "betcris_pl":
                            strict_betsport = adapter.startswith("betcris_pl_strict")
                            active_bound = bool(rec.get("_active_card_bound", False))
                            trusted_clock = (
                                strict_betsport and active_bound
                                and str(rec.get("_clock_mode") or "") == "football_90"
                                and rec.get("minute") is not None
                                and format_seconds(str(rec.get("format") or ""), str(rec.get("league") or "")) is not None
                            )
                            trusted_market = bool(strict_betsport and active_bound and market_ok)
                        elif expected_parser == "betcris":
                            trusted_clock = False
                            trusted_market = bool(market_ok)
                        else:
                            trusted_clock = True
                            trusted_market = bool(market_ok)
                        rec["_trusted_clock"] = bool(trusted_clock)
                        rec["_trusted_market"] = bool(trusted_market)
                        try:
                            raw_id, analysis_id = self.db.record_snapshot(
                                session_id=self.session_id, master_uid=rec.get("_master_match_uid"), source=source,
                                source_match_uid=rec.get("_match_uid"), rec=(rec.get("_db_record_override") or rec), semantic_quality=source_semantic_quality_now,
                                parser_version=rec.get("_parser_version") or expected_parser, source_url=source_url,
                                transport_quality=transport_quality_now, trusted_clock=trusted_clock, trusted_market=trusted_market,
                                raw_rec=rec.get("_raw_input_record"),
                            )
                            rec["_raw_snapshot_id"] = raw_id
                            rec["_analysis_snapshot_id"] = analysis_id
                            self.last_raw_snapshot_id[record_key(rec)] = raw_id
                            if rec.get("_final_confirmed"):
                                self.db.add_event(
                                    master_uid=rec.get("_master_match_uid"), event_type="FT", source=source, rec=rec,
                                    new_value=rec.get("_final_score") or self._score_text(rec), confidence=rec.get("_score_confidence"),
                                    raw_snapshot_id=raw_id, event_at=now_iso(),
                                )
                        except Exception as db_exc:
                            snapshot_db_errors.append(f"{type(db_exc).__name__}: {db_exc}")

                    if snapshot_db_errors:
                        raise RuntimeError("sqlite snapshot: " + "; ".join(snapshot_db_errors[:3]))

                    pending_side_effects = []
                    self._log_events(old_map, new_map, mono, strict_db=True, pending_side_effects=pending_side_effects)
                    self._log_anomalies(anomalies, payload, strict_db=True, pending_side_effects=pending_side_effects)
            except Exception as db_exc:
                (self.match_uid_by_key, self.match_generation_by_key, self.match_last_seen_mono,
                 self.match_meta, self.last_raw_snapshot_id, self.goal_count, self.change_count,
                 self.anomaly_count, self.recent_events, self.evidence_signatures) = identity_before
                state.records = state_records_before
                state.pending_scores = state_pending_before
                self.current_by_source[source] = old_map
                self.current = {}
                for src_map in self.current_by_source.values():
                    self.current.update(src_map)
                self.last_error = f"sqlite packet rollback: {type(db_exc).__name__}: {db_exc}"
                st["last_error"] = self.last_error
                st["data_state"] = "DB_RETRY"
                self._write_diag()
                return {
                    "ok": False, "retryable": True, "error": self.last_error,
                    "source": source, "server_session": self.session_id, "running": True,
                }

            # SQLite is committed atomically for the packet. Human-readable exports
            # and UI events are applied only AFTER commit, so a failed transaction
            # cannot leave a ghost goal/change/anomaly that would be duplicated on retry.
            try:
                self._apply_pending_side_effects(pending_side_effects)
            except Exception as side_exc:
                self.last_error = f"export after SQLite commit: {type(side_exc).__name__}: {side_exc}"
                st["last_error"] = self.last_error
            self._write_match_summary()
            for master_uid in {r.get("_master_match_uid") for r in new_map.values() if r.get("_master_match_uid")}:
                try:
                    self.db.export_match_files(master_uid)
                except Exception:
                    pass
            last_snap = self.last_snapshot_mono_by_source.get(source, 0.0)
            if mono - last_snap >= self.config.snapshot_interval:
                self._write_snapshot(new_map)
                self.last_snapshot_mono_by_source[source] = mono
                self.last_snapshot_mono = mono

            _ack_packet()
            self._write_diag()
            return {
                "ok": True, "running": True, "matches": len(new_map),
                "goals": self.goal_count, "changes": self.change_count,
                "anomalies": self.anomaly_count, "ack_packet_id": packet_id,
                "ack_seq": packet_seq, "source": source,
                "server_session": self.session_id, "duplicate": False,
            }

    def _update_match_lifecycle(self, source: str, new_map: Dict[str, dict], mono: float, *, strict_db: bool = False):
        now = now_iso()
        active_master_uids = {r.get("_master_match_uid") for r in self.current.values() if r.get("_master_match_uid")}
        active_source_uids = {r.get("_match_uid") for r in new_map.values() if r.get("_match_uid")}
        try:
            self.db.set_source_inactive(source, active_source_uids)
        except Exception as db_exc:
            if strict_db:
                raise RuntimeError(f"sqlite source lifecycle: {type(db_exc).__name__}: {db_exc}") from db_exc

        for rec in new_map.values():
            master_uid = rec.get("_master_match_uid")
            if not master_uid:
                continue
            meta = self.match_meta.setdefault(master_uid, {
                "session": self.session_id, "match_uid": rec.get("_match_uid"),
                "master_match_uid": master_uid, "pair_uid": rec.get("_pair_uid"),
                "league": rec.get("league"), "player1": rec.get("player1"), "team1": rec.get("team1"),
                "player2": rec.get("player2"), "team2": rec.get("team2"),
                "first_seen": now, "last_seen": now, "last_seen_mono": mono,
                "final_confirmed": False, "final_score": "", "finish_reason": "",
                "semantic_score": 0,
                # Internal sets stay private; exported source strings are separate fields.
                "_source_ids_set": set(), "_source_labels_set": set(), "_source_match_uids_set": set(),
            })

            def _coerce_set(value):
                if isinstance(value, set):
                    return value
                if isinstance(value, (list, tuple)):
                    return {str(x) for x in value if str(x)}
                if isinstance(value, str):
                    return {x.strip() for x in value.split(",") if x.strip()}
                return set()

            source_ids_set = _coerce_set(meta.get("_source_ids_set"))
            source_labels_set = _coerce_set(meta.get("_source_labels_set"))
            source_match_uids_set = _coerce_set(meta.get("_source_match_uids_set"))
            source_ids_set.add(str(source))
            source_labels_set.add(human_source_label(source, rec))
            if rec.get("_match_uid"):
                source_match_uids_set.add(str(rec.get("_match_uid")))
            meta["_source_ids_set"] = source_ids_set
            meta["_source_labels_set"] = source_labels_set
            meta["_source_match_uids_set"] = source_match_uids_set

            source_ids_text = ",".join(sorted(source_ids_set))
            source_labels_text = ",".join(sorted(source_labels_set))
            source_match_uids_text = ",".join(sorted(source_match_uids_set))
            new_score_text = self._score_text(rec)
            old_score_text = str(meta.get("last_score") or "")
            keep_score_text = old_score_text or new_score_text
            try:
                ns1, ns2 = (int(x) for x in new_score_text.split(":"))
                os1, os2 = (int(x) for x in old_score_text.split(":"))
                if ns1 >= os1 and ns2 >= os2:
                    keep_score_text = new_score_text
            except Exception:
                if new_score_text and not old_score_text:
                    keep_score_text = new_score_text
            meta.update({
                "master_match_uid": master_uid, "pair_uid": rec.get("_pair_uid"),
                "league": rec.get("league"), "player1": meta.get("player1") or rec.get("player1"), "team1": meta.get("team1") or rec.get("team1"),
                "player2": meta.get("player2") or rec.get("player2"), "team2": meta.get("team2") or rec.get("team2"),
                "last_seen": now, "last_seen_mono": mono, "last_score": keep_score_text,
                "source_ids": source_ids_text,
                "source_labels": source_labels_text,
                "match_uid": source_match_uids_text,
            })
            status_raw = str(rec.get("status") or "")
            status = status_raw.casefold()
            if re.search(r"(?:finished|final|zako[nń]cz|koniec|full\s*time|ft\b)", status):
                # Betsport FT is trusted only when the record is bound to the exact
                # /match/ card named by the page title.
                if str(rec.get("_parser_family") or "").casefold() == "betcris_pl" and not bool(rec.get("_active_card_bound", False)):
                    continue
                trusted1, trusted2 = self.db.trusted_master_score(master_uid)
                explicit1, explicit2 = self._status_final_score(status_raw)
                candidate_rec = dict(rec)
                if explicit1 is not None and explicit2 is not None:
                    candidate_rec["score1"], candidate_rec["score2"] = explicit1, explicit2
                cand1, cand2 = self.db.oriented_score(master_uid, candidate_rec)
                final1 = final2 = None
                if cand1 is not None and cand2 is not None:
                    cand1, cand2 = int(cand1), int(cand2)
                    if trusted1 is None or trusted2 is None or (cand1 >= trusted1 and cand2 >= trusted2):
                        final1, final2 = cand1, cand2
                if final1 is None and trusted1 is not None and trusted2 is not None:
                    final1, final2 = int(trusted1), int(trusted2)
                    if cand1 is not None and cand2 is not None and (int(cand1), int(cand2)) != (final1, final2):
                        self.db.add_anomaly(
                            master_uid=master_uid, source=source, anomaly_type="FT_SCORE_CONFLICT",
                            old_value=f"trusted {final1}:{final2}", new_value=f"finished {int(cand1)}:{int(cand2)}",
                        )
                if final1 is not None and final2 is not None:
                    self.db.confirm_final(master_uid=master_uid, score1=final1, score2=final2, source=source, reason="explicit_finished_trusted_score")
                    meta["final_confirmed"] = True
                    meta["final_score"] = f"{final1}:{final2}"
                    meta["finish_reason"] = "explicit_finished_trusted_score"
                    rec["_final_confirmed"] = True
                    rec["_final_score"] = meta["final_score"]
                    rec["_finish_reason"] = meta["finish_reason"]

        # A match absent from one bookmaker is not FT. Only when it is absent from
        # every currently accepted source do we mark DISAPPEARED_UNCONFIRMED.
        for master_uid, meta in self.match_meta.items():
            if master_uid in active_master_uids or meta.get("final_confirmed"):
                continue
            last = meta.get("last_seen_mono")
            if last is not None and mono - float(last) >= self.finish_grace_seconds:
                meta["final_confirmed"] = False
                meta["final_score"] = meta.get("last_score") or ""
                meta["finish_reason"] = "disappeared_after_grace_unconfirmed"
                try:
                    self.db.mark_disappeared(master_uid, source, reason="disappeared_after_grace_unconfirmed")
                except Exception as db_exc:
                    if strict_db:
                        raise RuntimeError(f"sqlite disappearance: {type(db_exc).__name__}: {db_exc}") from db_exc

    def _write_match_summary(self):
        if not self.session_dir:
            return
        p = self.session_dir / "mecze_master.csv"
        with p.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=self.MATCH_SUMMARY_FIELDS, delimiter=";", extrasaction="ignore")
            w.writeheader()
            export_rows = []
            for meta in sorted(self.match_meta.values(), key=lambda x: (str(x.get("first_seen") or ""), str(x.get("match_uid") or ""))):
                row = dict(meta)
                ids = row.pop("_source_ids_set", set())
                labels = row.pop("_source_labels_set", set())
                match_uids = row.pop("_source_match_uids_set", set())
                if isinstance(ids, set):
                    row["source_ids"] = ",".join(sorted(ids))
                if isinstance(labels, set):
                    row["source_labels"] = ",".join(sorted(labels))
                if isinstance(match_uids, set):
                    row["match_uid"] = ",".join(sorted(match_uids))
                db_row = self.db.match_row(str(row.get("master_match_uid") or "")) or {}
                row["semantic_score"] = int(db_row.get("semantic_score") or 0)
                for flag in ("ready_score", "ready_clock", "ready_market", "ready_live_model", "ready_ft"):
                    row[flag] = int(db_row.get(flag) or 0)
                export_rows.append(row)
                w.writerow({k: safe(row.get(k)) for k in self.MATCH_SUMMARY_FIELDS})
        (self.session_dir / "mecze_master.json").write_text(
            json.dumps(export_rows, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _log_events(self, old_map: Dict[str, dict], new_map: Dict[str, dict], mono: float, *, strict_db: bool = False, pending_side_effects: list | None = None):
        compare_fields = ["status", "half", "minute", "line"]
        if self.config.log_odds_changes:
            compare_fields += ["under_odds", "over_odds"]

        for key, new in new_map.items():
            old = old_map.get(key)
            if old is None:
                # First accepted observation belongs on the per-match timeline.
                # It is not counted as a goal/change; it simply makes the preview
                # useful immediately instead of showing an empty axis.
                start_event = {
                    "type": "start", "timestamp": str(new.get("_captured_at") or now_iso()), "session": self.session_id,
                    "key": key, "pair_uid": new.get("_pair_uid"), "match_uid": new.get("_match_uid"), "master_match_uid": new.get("_master_match_uid"),
                    "league": new.get("league"), "player1": new.get("player1"), "player2": new.get("player2"),
                    "score": self._score_text(new), "status": new.get("status"), "minute": new.get("minute"),
                    "line": new.get("line"), "source_url": new.get("_source_url") or self.last_url,
                    "source": new.get("_source_platform") or "source",
                    "source_label": new.get("_source_label") or new.get("_source_platform") or "source",
                    "parser_family": new.get("_parser_family") or "",
                    "collector_id": new.get("_collector_id") or "",
                    "packet_seq": new.get("_packet_seq") if new.get("_packet_seq") is not None else "",
                }
                if pending_side_effects is not None:
                    pending_side_effects.append(("start", start_event))
                else:
                    self._push_event(start_event)
                continue

            old_score = score_pair(old)
            new_score = score_pair(new)
            transition = new.get("_score_transition")
            if new_score is not None and old_score is not None and new_score != old_score and transition in {"goal", "catchup", "reset"}:
                if transition in {"goal", "catchup"}:
                    d = (new_score[0] - old_score[0]) + (new_score[1] - old_score[1])
                    low = new.get("_score_event_low_mono")
                    high = new.get("_score_event_high_mono")
                    uncertainty = None
                    if low is not None and high is not None:
                        try:
                            uncertainty = max(0.0, float(high) - float(low))
                        except Exception:
                            uncertainty = None
                    row = {
                        "timestamp_detected": str(new.get("_captured_at") or now_iso()),
                        "session": self.session_id,
                        "key": key,
                        "pair_uid": new.get("_pair_uid"), "match_uid": new.get("_match_uid"), "master_match_uid": new.get("_master_match_uid"),
                        "league": new.get("league"),
                        "player1": new.get("player1"),
                        "player2": new.get("player2"),
                        "score_old": f"{old_score[0]}:{old_score[1]}",
                        "score_new": f"{new_score[0]}:{new_score[1]}",
                        "delta_goals": d,
                        "transition": transition,
                        "window_start": low,
                        "window_end": high,
                        "uncertainty_seconds": uncertainty,
                        "status": new.get("status"),
                        "minute": new.get("minute"),
                        "source_url": new.get("_source_url") or self.last_url,
                        "source": new.get("_source_platform") or "fortuna",
                        "source_label": new.get("_source_label") or new.get("_source_platform") or "fortuna",
                        "parser_family": new.get("_parser_family") or "",
                        "collector_id": new.get("_collector_id") or "",
                        "packet_seq": new.get("_packet_seq") if new.get("_packet_seq") is not None else "",
                    }
                    try:
                        inserted_goals = self.db.add_goal(
                            master_uid=new.get("_master_match_uid"), source=new.get("_source_platform") or "source",
                            old_rec=old, new_rec=new, uncertainty_seconds=uncertainty,
                            raw_snapshot_id=new.get("_raw_snapshot_id"),
                        )
                        # Score events are master-level facts. A +1 is GOAL; a larger
                        # jump is CATCHUP and must never masquerade as one exact goal.
                        # A second bookmaker may confirm the same range but cannot duplicate it.
                        if inserted_goals:
                            score_event_type = "GOAL" if transition == "goal" else "CATCHUP"
                            self.db.add_event(
                                master_uid=new.get("_master_match_uid"), event_type=score_event_type, source=new.get("_source_platform") or "source",
                                rec=new, old_value=row.get("score_old"), new_value=row.get("score_new"),
                                confidence=new.get("_score_confidence"), raw_snapshot_id=new.get("_raw_snapshot_id"), event_at=row.get("timestamp_detected"),
                            )
                    except Exception as db_exc:
                        self.last_error = f"sqlite goal: {type(db_exc).__name__}: {db_exc}"
                        if strict_db:
                            raise RuntimeError(self.last_error) from db_exc
                    else:
                        if inserted_goals:
                            if pending_side_effects is not None:
                                pending_side_effects.append(("goal", row, int(inserted_goals)))
                            else:
                                self._append_csv(self.session_dir / "gole.csv", self.GOAL_FIELDS, row)
                                self._append_jsonl(self.session_dir / "gole.jsonl", row)
                                self.goal_count += int(inserted_goals)
                                self._push_event({"type": transition, **row})

                change_row = {
                    "timestamp": str(new.get("_captured_at") or now_iso()), "session": self.session_id, "key": key,
                    "pair_uid": new.get("_pair_uid"), "match_uid": new.get("_match_uid"), "master_match_uid": new.get("_master_match_uid"),
                    "league": new.get("league"), "player1": new.get("player1"), "player2": new.get("player2"),
                    "field": "score", "old": f"{old_score[0]}:{old_score[1]}", "new": f"{new_score[0]}:{new_score[1]}",
                    "score": f"{new_score[0]}:{new_score[1]}", "status": new.get("status"), "minute": new.get("minute"),
                    "source_url": new.get("_source_url") or self.last_url,
                    "source": new.get("_source_platform") or "fortuna",
                    "source_label": new.get("_source_label") or new.get("_source_platform") or "fortuna",
                    "parser_family": new.get("_parser_family") or "",
                    "collector_id": new.get("_collector_id") or "",
                    "packet_seq": new.get("_packet_seq") if new.get("_packet_seq") is not None else "",
                }
                if pending_side_effects is not None:
                    pending_side_effects.append(("change", change_row))
                else:
                    self._append_change(change_row)

            for field in compare_fields:
                ov, nv = old.get(field), new.get(field)
                if ov == nv:
                    continue
                # Ignore a disappearing field: canonical state should normally carry it,
                # and we do not want transient rerenders to become "changes".
                if nv in (None, ""):
                    continue
                row = {
                    "timestamp": str(new.get("_captured_at") or now_iso()), "session": self.session_id, "key": key,
                    "pair_uid": new.get("_pair_uid"), "match_uid": new.get("_match_uid"), "master_match_uid": new.get("_master_match_uid"),
                    "league": new.get("league"), "player1": new.get("player1"), "player2": new.get("player2"),
                    "field": field, "old": ov, "new": nv, "score": self._score_text(new),
                    "status": new.get("status"), "minute": new.get("minute"),
                    "source_url": new.get("_source_url") or self.last_url,
                    "source": new.get("_source_platform") or "fortuna",
                    "source_label": new.get("_source_label") or new.get("_source_platform") or "fortuna",
                    "parser_family": new.get("_parser_family") or "",
                    "collector_id": new.get("_collector_id") or "",
                    "packet_seq": new.get("_packet_seq") if new.get("_packet_seq") is not None else "",
                }
                try:
                    db_event_type = None
                    if field == "line":
                        db_event_type = "LINE"
                    elif field in {"under_odds", "over_odds"}:
                        db_event_type = "ODDS"
                    elif field == "half":
                        db_event_type = "2P" if new.get("half") == 2 else "HT" if new.get("half") in (None, "") else None
                    elif field == "status":
                        txt = str(nv or "").casefold()
                        if re.search(r"(?:half\s*time|halftime|przerwa|\bht\b)", txt):
                            db_event_type = "HT"
                        elif re.search(r"(?:2\.\s*po[lł]|second\s*half|2nd\s*half)", txt):
                            db_event_type = "2P"
                        elif re.search(r"(?:finished|final|zako[nń]cz|koniec|full\s*time|\bft\b)", txt):
                            db_event_type = "FT"
                    if db_event_type:
                        self.db.add_event(
                            master_uid=new.get("_master_match_uid"), event_type=db_event_type, source=new.get("_source_platform") or "source",
                            rec=new, old_value=ov, new_value=nv, confidence=new.get("_score_confidence"),
                            raw_snapshot_id=new.get("_raw_snapshot_id"), event_at=row.get("timestamp"),
                        )
                except Exception as db_exc:
                    self.last_error = f"sqlite event: {type(db_exc).__name__}: {db_exc}"
                    if strict_db:
                        raise RuntimeError(self.last_error) from db_exc
                else:
                    if pending_side_effects is not None:
                        pending_side_effects.append(("change", row))
                    else:
                        self._append_change(row)

            # ODDS are factual database events even when the human-facing CSV/UI option
            # ``log_odds_changes`` is disabled. The option only controls the noisy
            # human-facing change log; SQLite remains a complete source of truth.
            if not self.config.log_odds_changes:
                for field in ("under_odds", "over_odds"):
                    ov, nv = old.get(field), new.get(field)
                    if ov == nv or nv in (None, ""):
                        continue
                    try:
                        self.db.add_event(
                            master_uid=new.get("_master_match_uid"), event_type="ODDS",
                            source=new.get("_source_platform") or "source", rec=new,
                            old_value=ov, new_value=nv, confidence=new.get("_score_confidence"),
                            raw_snapshot_id=new.get("_raw_snapshot_id"), event_at=str(new.get("_captured_at") or now_iso()),
                        )
                    except Exception as db_exc:
                        self.last_error = f"sqlite odds event: {type(db_exc).__name__}: {db_exc}"
                        if strict_db:
                            raise RuntimeError(self.last_error) from db_exc

    def _append_change(self, row: dict):
        self._append_csv(self.session_dir / "zmiany.csv", self.CHANGE_FIELDS, row)
        self._append_jsonl(self.session_dir / "zmiany.jsonl", row)
        self.change_count += 1
        self._push_event({"type": "change", **row})

    @staticmethod
    def _score_pair_text(value):
        m = re.match(r"^\s*(\d+)\s*:\s*(\d+)\s*$", str(value or ""))
        return (int(m.group(1)), int(m.group(2))) if m else None

    def _match_folder_name(self, key: str, rec: dict) -> str:
        master_uid = str(rec.get("_master_match_uid") or "").strip()
        if master_uid:
            return re.sub(r"[^0-9A-Za-z._-]+", "_", master_uid)
        digest = hashlib.sha1(str(key).encode("utf-8", "ignore")).hexdigest()[:12]
        return f"UNKNOWN_{digest}"

    def _append_full_timeline(self, event: dict):
        if not self.session_dir:
            return
        key = str(event.get("key") or "")
        rec = self.current.get(key, {})
        etype = str(event.get("type") or "event")
        field = str(event.get("field") or "")
        # Zwykły wzrost wyniku jest już zapisany jako GOL. Nie duplikujemy go
        # drugi raz w scalonej osi czasu. Cofnięcie wyniku pozostaje jako reset.
        if etype == "change" and field == "score":
            a = self._score_pair_text(event.get("old"))
            b = self._score_pair_text(event.get("new"))
            if a and b and (b[0] + b[1]) > (a[0] + a[1]):
                return
            etype = "score_reset"

        old = event.get("old")
        new = event.get("new")
        score = event.get("score") or event.get("score_new") or self._score_text(rec)
        scorer = ""
        if etype == "start":
            field = "start"
            old = ""
            new = score or event.get("status") or "odczyt"
        if etype == "goal":
            a = self._score_pair_text(event.get("score_old"))
            b = self._score_pair_text(event.get("score_new"))
            if a and b:
                if b[0] > a[0] and b[1] == a[1]:
                    scorer = rec.get("player1") or rec.get("team1") or "1. drużyna"
                elif b[1] > a[1] and b[0] == a[0]:
                    scorer = rec.get("player2") or rec.get("team2") or "2. drużyna"
            old = event.get("score_old")
            new = event.get("score_new")
            field = "score"

        row = {
            "timestamp": event.get("timestamp_detected") or event.get("timestamp") or now_iso(),
            "session": self.session_id,
            "key": key,
            "league": event.get("league") or rec.get("league"),
            "player1": event.get("player1") or rec.get("player1"),
            "team1": rec.get("team1"),
            "player2": event.get("player2") or rec.get("player2"),
            "team2": rec.get("team2"),
            "event_type": etype,
            "field": field,
            "old": old,
            "new": new,
            "score": score,
            "scorer": scorer,
            "status": event.get("status") or rec.get("status"),
            "minute": event.get("minute") if event.get("minute") is not None else rec.get("minute"),
            "uncertainty_seconds": event.get("uncertainty_seconds"),
            "reason": event.get("reason"),
            "pair_uid": event.get("pair_uid") or rec.get("_pair_uid"),
            "match_uid": event.get("match_uid") or rec.get("_match_uid"),
            "master_match_uid": event.get("master_match_uid") or rec.get("_master_match_uid"),
            "source_url": event.get("source_url") or rec.get("_source_url") or self.last_url,
            "source": event.get("source") or rec.get("_source_platform") or "fortuna",
            "source_label": event.get("source_label") or rec.get("_source_label") or rec.get("_source_platform") or "fortuna",
            "parser_family": event.get("parser_family") or rec.get("_parser_family") or "",
            "collector_id": event.get("collector_id") or rec.get("_collector_id") or "",
            "packet_seq": event.get("packet_seq") if event.get("packet_seq") is not None else rec.get("_packet_seq", ""),
        }
        self._append_csv(self.session_dir / "timeline_pelna.csv", self.TIMELINE_FIELDS, row)
        self._append_jsonl(self.session_dir / "timeline_pelna.jsonl", row)
        master_uid = str(row.get("master_match_uid") or rec.get("_master_match_uid") or "").strip()
        match_dir = self.base_dir / "matches" / (master_uid or self._match_folder_name(key, rec))
        self._append_csv(match_dir / "timeline.csv", self.TIMELINE_FIELDS, row)
        self._append_jsonl(match_dir / "timeline.jsonl", row)

    def _push_event(self, event: dict):
        self._append_full_timeline(event)
        self.recent_events.insert(0, event)
        # UI dostaje ograniczone okno, pliki timeline_pelna.* mają pełną historię.
        del self.recent_events[80:]

    def _log_anomalies(self, anomalies: list, payload: dict, *, strict_db: bool = False, pending_side_effects: list | None = None):
        for item in anomalies or []:
            old = item.get("old") or {}
            cand = item.get("candidate") or {}
            reason = str(item.get("reason") or "anomalia LIVE")
            source = self._source_name(payload)
            st = self.source_status.setdefault(source, {})
            st["semantic_anomalies"] = int(st.get("semantic_anomalies") or 0) + 1
            rlow = reason.casefold()
            if "cofnięcie minuty" in rlow:
                anomaly_type = "CLOCK_BACKWARD"
            elif "spadek wyniku" in rlow:
                anomaly_type = "SCORE_BACKWARD"
            elif "score binding shift" in rlow:
                anomaly_type = "SCORE_BINDING"
            elif "confidence=" in rlow or "niska pewność" in rlow:
                anomaly_type = "SCORE_LOW_CONFIDENCE"
            else:
                anomaly_type = "PARSER_ANOMALY"
            fam = st.setdefault("semantic_anomaly_types", {})
            if not isinstance(fam, dict):
                fam = {}
                st["semantic_anomaly_types"] = fam
            fam[anomaly_type] = int(fam.get(anomaly_type) or 0) + 1
            if anomaly_type in {"SCORE_BACKWARD", "SCORE_BINDING"}:
                st["score_regressions"] = int(st.get("score_regressions") or 0) + 1
            raw_key = record_key(cand or old)
            key = raw_key if str(raw_key).startswith(source + "|") else f"{source}|{raw_key}"
            sig = f"{key}|{reason}"
            evidence = ""
            evidence_blob = None
            if sig not in self.evidence_signatures and len(self.evidence_signatures) < self.config.max_raw_evidence:
                self.evidence_signatures.add(sig)
                idx = len(self.evidence_signatures)
                p = self.raw_dir / f"anomalia_{idx:03d}.json"
                evidence_blob = {"reason": reason, "payload": payload, "old": old, "candidate": cand}
                evidence = p.name

            rec = cand or old
            try:
                linked = self.current_by_source.get(source, {}).get(key) or self.current.get(key) or {}
                master_uid = linked.get("_master_match_uid")
                raw_id = linked.get("_raw_snapshot_id")
                if anomaly_type == "CLOCK_BACKWARD":
                    old_value = old.get("minute")
                    new_value = cand.get("minute")
                else:
                    old_value = self._score_text(old)
                    new_value = self._score_text(cand)
                occurrence_ts = str(rec.get("_captured_at") or payload.get("collector_ts") or now_iso())
                row = {
                    "timestamp": occurrence_ts, "session": self.session_id, "key": key,
                    "master_match_uid": master_uid or "",
                    "league": rec.get("league"), "player1": rec.get("player1"), "player2": rec.get("player2"),
                    "reason": reason, "candidate_score": self._score_text(cand), "accepted_score": self._score_text(old),
                    "type": anomaly_type, "old_value": "" if old_value is None else str(old_value),
                    "new_value": "" if new_value is None else str(new_value),
                    "source_url": str(payload.get("url") or self.last_url), "evidence_file": evidence,
                    "source": source,
                    "source_label": str(payload.get("source_label") or source),
                    "parser_family": str(payload.get("parser_family") or payload.get("platform") or "auto"),
                    "collector_id": self._collector_key(payload, source),
                    "packet_seq": payload.get("seq") if payload.get("seq") is not None else "",
                }
                self.db.add_anomaly(master_uid=master_uid, source=source, anomaly_type=anomaly_type, old_value=old_value, new_value=new_value, raw_snapshot_id=raw_id)
            except Exception as db_exc:
                self.last_error = f"sqlite anomaly: {type(db_exc).__name__}: {db_exc}"
                if strict_db:
                    raise RuntimeError(self.last_error) from db_exc
            else:
                action = ("anomaly", row, evidence_blob)
                if pending_side_effects is not None:
                    pending_side_effects.append(action)
                else:
                    self._apply_pending_side_effects([action])

    def _apply_pending_side_effects(self, actions: list):
        for action in actions or []:
            kind = action[0]
            row = action[1]
            if kind == "start":
                self._push_event(row)
            elif kind == "goal":
                self._append_csv(self.session_dir / "gole.csv", self.GOAL_FIELDS, row)
                self._append_jsonl(self.session_dir / "gole.jsonl", row)
                self.goal_count += int(action[2] if len(action) > 2 else 1)
                self._push_event({"type": str(row.get("transition") or "goal"), **row})
            elif kind == "change":
                self._append_change(row)
            elif kind == "anomaly":
                evidence_blob = action[2] if len(action) > 2 else None
                if evidence_blob and row.get("evidence_file"):
                    (self.raw_dir / str(row["evidence_file"])).write_text(
                        json.dumps(evidence_blob, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                # Full occurrence history stays in JSONL; the human-facing CSV is
                # aggregated into semantic families such as SCORE_BACKWARD 5:4→4:6 ×139.
                self._append_jsonl(self.session_dir / "anomalie_raw.jsonl", row)
                agg_key = (
                    str(row.get("master_match_uid") or ""), str(row.get("source") or ""),
                    str(row.get("type") or "PARSER_ANOMALY"), str(row.get("old_value") or ""),
                    str(row.get("new_value") or ""),
                )
                agg = self.anomaly_agg.get(agg_key)
                if agg is None:
                    agg = {
                        "first_seen": row.get("timestamp") or now_iso(),
                        "last_seen": row.get("timestamp") or now_iso(),
                        "session": self.session_id,
                        "master_match_uid": row.get("master_match_uid") or "",
                        "source": row.get("source") or "",
                        "source_label": row.get("source_label") or "",
                        "parser_family": row.get("parser_family") or "",
                        "type": row.get("type") or "PARSER_ANOMALY",
                        "old_value": row.get("old_value") or "",
                        "new_value": row.get("new_value") or "",
                        "occurrences": 0,
                        "example_reason": row.get("reason") or "",
                        "evidence_file": row.get("evidence_file") or "",
                    }
                    self.anomaly_agg[agg_key] = agg
                agg["last_seen"] = row.get("timestamp") or agg["last_seen"]
                agg["occurrences"] = int(agg.get("occurrences") or 0) + 1
                if not agg.get("evidence_file") and row.get("evidence_file"):
                    agg["evidence_file"] = row.get("evidence_file")
                self._rewrite_anomaly_summary()
                self.anomaly_count += 1
                self._push_event({"type": "anomaly", **row})

    def _rewrite_anomaly_summary(self):
        if not self.session_dir:
            return
        p = self.session_dir / "anomalie.csv"
        with p.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=self.ANOM_FIELDS, delimiter=";", extrasaction="ignore")
            w.writeheader()
            for row in sorted(self.anomaly_agg.values(), key=lambda x: (-int(x.get("occurrences") or 0), str(x.get("type") or ""))):
                w.writerow({k: safe(row.get(k)) for k in self.ANOM_FIELDS})

    def _write_snapshot(self, recs: Dict[str, dict]):
        ts = now_iso()
        for rec in recs.values():
            row = self._flat_record(rec, ts)
            self._append_csv(self.session_dir / "snapshoty.csv", self.SNAP_FIELDS, row)
            self._append_jsonl(self.session_dir / "snapshoty.jsonl", row)
        self.saved_snapshot_batches += 1
        self.saved_snapshot_rows += len(recs)
        self._rewrite_current_csv(self.current)

    def _rewrite_current_csv(self, recs: Dict[str, dict]):
        p = self.session_dir / "aktualne_mecze.csv"
        with p.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=self.SNAP_FIELDS, delimiter=";", extrasaction="ignore")
            w.writeheader()
            ts = now_iso()
            for rec in recs.values():
                w.writerow(self._flat_record(rec, ts))

    @staticmethod
    def _process_rss_mb() -> float:
        """Current process RSS without an extra dependency (Windows + Linux fallback)."""
        try:
            if os.name == "nt":
                import ctypes
                from ctypes import wintypes

                class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                    _fields_ = [
                        ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
                    ]
                counters = PROCESS_MEMORY_COUNTERS()
                counters.cb = ctypes.sizeof(counters)
                handle = ctypes.windll.kernel32.GetCurrentProcess()
                ok = ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)
                if ok:
                    return round(float(counters.WorkingSetSize) / (1024.0 * 1024.0), 3)
            statm = Path("/proc/self/statm")
            if statm.exists():
                pages = int(statm.read_text(encoding="ascii").split()[1])
                page_size = int(os.sysconf("SC_PAGE_SIZE"))
                return round((pages * page_size) / (1024.0 * 1024.0), 3)
        except Exception:
            pass
        return 0.0

    def _sample_resources(self, *, force: bool = False) -> dict:
        now = time.time()
        if not force and self._resource_last_sample and now - self._resource_last_wall < self.resource_sample_interval_seconds:
            return dict(self._resource_last_sample)
        proc_now = time.process_time()
        wall_delta = now - self._resource_last_wall if self._resource_last_wall else 0.0
        proc_delta = proc_now - self._resource_last_process
        cpu_count = max(1, int(os.cpu_count() or 1))
        cpu_pct = 0.0 if wall_delta <= 0 else max(0.0, min(100.0, (proc_delta / wall_delta) * 100.0 / cpu_count))
        sqlite_bytes = 0
        try:
            for suffix in ("", "-wal", "-shm"):
                q = Path(str(self.db.path) + suffix)
                if q.exists():
                    sqlite_bytes += int(q.stat().st_size)
        except Exception:
            sqlite_bytes = 0
        queue_depth = 0
        try:
            queue_depth = sum(max(0, int((meta or {}).get("queue_depth") or 0)) for meta in self.source_status.values())
        except Exception:
            queue_depth = 0
        row = {
            "timestamp": now_iso(),
            "process_ram_mb": self._process_rss_mb(),
            "cpu_pct": round(cpu_pct, 3),
            "sqlite_mb": round(sqlite_bytes / (1024.0 * 1024.0), 3),
            "queue_depth": int(queue_depth),
            "matches": int(len(self.current)),
            "raw_packets": int(self.collector_count),
        }
        self._resource_last_wall = now
        self._resource_last_process = proc_now
        self._resource_last_sample = dict(row)
        if self.session_dir:
            path = self.session_dir / "zasoby.csv"
            exists = path.exists()
            with path.open("a", encoding="utf-8-sig", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(row.keys()), delimiter=";")
                if not exists:
                    w.writeheader()
                w.writerow(row)
        return row

    def _write_diag(self, final: bool = False):
        if not self.session_dir:
            return
        current_status = self.status()
        resources = self._sample_resources(force=bool(final))
        diag = {
            "updated_at": now_iso(),
            "running": self.running,
            "session": self.session_id,
            "last_ingest": self.last_ingest_iso,
            "last_url": self.last_url,
            "last_title": self.last_title,
            "ingest_count": self.ingest_count,
            "collector_posts_total": self.collector_count,
            "matches": len(self.current),
            "saved_snapshot_batches": self.saved_snapshot_batches,
            "saved_snapshot_rows_csv": self.saved_snapshot_rows,
            "counters": current_status.get("counters", {}),
            "goal_count": self.goal_count,
            "change_count": self.change_count,
            "anomaly_occurrences": self.anomaly_count,
            "anomaly_families": len(self.anomaly_agg),
            "sources": current_status.get("sources", []),
            "gold_quality": current_status.get("gold_quality", {}),
            "semantic_quality": current_status.get("semantic_quality", {}),
            "packet_duplicates": self.packet_duplicates,
            "packet_gaps": self.packet_gaps,
            "packet_recovered": self.packet_recovered,
            "packet_stale": self.packet_stale,
            "last_error": self.last_error,
            "resources": resources,
        }
        (self.session_dir / "diagnostyka.json").write_text(json.dumps(diag, ensure_ascii=False, indent=2), encoding="utf-8")
        quality = {
            "updated_at": diag["updated_at"],
            "session": self.session_id,
            "final": bool(final),
            "gold_quality": current_status.get("gold_quality", {}),
            "sources": current_status.get("sources", []),
            "counters": current_status.get("counters", {}),
            "browser_note": "GOLD QUALITY ocenia transport/świeżość/kompletność parsera; nie jest oceną prawdziwości danych operatora.",
        }
        (self.session_dir / "GOLD_QUALITY.json").write_text(json.dumps(quality, ensure_ascii=False, indent=2), encoding="utf-8")
        semantic = {
            "updated_at": diag["updated_at"],
            "session": self.session_id,
            "final": bool(final),
            "semantic_quality": current_status.get("semantic_quality", {}),
            "sources": [
                {
                    "source": x.get("source"), "source_label": x.get("source_label"),
                    "semantic_score": x.get("semantic_score"), "semantic_grade": x.get("semantic_grade"),
                    "semantic_acceptance": x.get("semantic_acceptance"),
                    "semantic_pair_ratio": x.get("semantic_pair_ratio"),
                    "semantic_uid_stability": x.get("semantic_uid_stability"),
                    "semantic_score_stability": x.get("semantic_score_stability"),
                    "semantic_score_validity": x.get("semantic_score_validity"),
                    "semantic_sport_validity": x.get("semantic_sport_validity"),
                    "semantic_player_validity": x.get("semantic_player_validity"),
                    "semantic_team_validity": x.get("semantic_team_validity"),
                    "semantic_team_cleanliness": x.get("semantic_team_cleanliness"),
                    "semantic_context_cleanliness": x.get("semantic_context_cleanliness"),
                    "semantic_league_validity": x.get("semantic_league_validity"),
                    "semantic_clock_validity": x.get("semantic_clock_validity"),
                    "semantic_format_validity": x.get("semantic_format_validity"),
                    "semantic_market_validity": x.get("semantic_market_validity"),
                    "semantic_freshness": x.get("semantic_freshness"),
                    "semantic_anomaly_rate": x.get("semantic_anomaly_rate"),
                    "semantic_rejected": x.get("semantic_rejected"),
                    "semantic_reasons": x.get("semantic_reasons") or {},
                } for x in current_status.get("sources", [])
            ],
            "note": "SEMANTIC QUALITY ocenia uczestników, drużyny, sport, ligę/format, score, zegar, rynek, czystość kontekstu, anomalie i świeżość. Nie jest oceną samego połączenia.",
        }
        (self.session_dir / "SEMANTIC_QUALITY.json").write_text(json.dumps(semantic, ensure_ascii=False, indent=2), encoding="utf-8")
        if final:
            lines = [
                f"# GOLD QUALITY — {self.session_id}", "",
                f"Stan końcowy: **{quality['gold_quality'].get('overall_health', 0)}/100**", "",
                "| Źródło | Stan | GOLD | Mecze | Kompletność | Pewność score | Q | Dropped | Gaps | Stale |",
                "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
            ]
            for x in quality["sources"]:
                lines.append(
                    f"| {x.get('source_label') or x.get('source')} | {x.get('connection')} | "
                    f"{x.get('health_grade','-')} {x.get('health_score',0)}/100 | {x.get('matches',0)} | "
                    f"{float(x.get('avg_completeness') or 0)*100:.0f}% | {float(x.get('avg_score_confidence') or 0)*100:.0f}% | "
                    f"{x.get('queue_depth',0)} | {x.get('queue_dropped',0)} | {x.get('seq_gaps',0)} | {x.get('stale_packets',0)} |"
                )
                why = ", ".join(x.get("health_reasons") or [])
                if why:
                    lines.append(f"  - {x.get('source_label') or x.get('source')}: {why}")
            lines += ["", "> GOLD QUALITY to diagnostyka kolektora i parsera, nie ranking bukmacherów ani gwarancja poprawności zewnętrznego feedu."]
            (self.session_dir / "GOLD_QUALITY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
            sem_lines = [
                f"# SEMANTIC QUALITY — {self.session_id}", "",
                f"Stan końcowy: **{semantic['semantic_quality'].get('overall_score', 0)}/100**", "",
                "| Źródło | SEM | Akceptacja | Score present | Sport valid | Score stability | Team clean | Clock | Market | Anom. rate |",
                "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
            ]
            for x in semantic["sources"]:
                sem_lines.append(
                    f"| {x.get('source_label') or x.get('source')} | {x.get('semantic_grade','-')} {x.get('semantic_score',0)}/100 | "
                    f"{float(x.get('semantic_acceptance') or 0)*100:.0f}% | {float(x.get('semantic_player_validity') or 0)*100:.0f}% | "
                    f"{float(x.get('semantic_team_validity') or 0)*100:.0f}% | {float(x.get('semantic_sport_validity') or 0)*100:.0f}% | "
                    f"{float(x.get('semantic_score_validity') or 0)*100:.0f}% | {float(x.get('semantic_score_stability') or 0)*100:.0f}% | "
                    f"{float(x.get('semantic_league_validity') or 0)*100:.0f}% | {float(x.get('semantic_format_validity') or 0)*100:.0f}% | "
                    f"{float(x.get('semantic_clock_validity') or 0)*100:.0f}% | {float(x.get('semantic_market_validity') or 0)*100:.0f}% | "
                    f"{float(x.get('semantic_anomaly_rate') or 0)*100:.1f}% |"
                )
            sem_lines += ["", "> SEMANTIC QUALITY jest oddzielone od GOLD QUALITY. GOLD mierzy transport/świeżość; SEMANTIC mierzy sens danych parsera."]
            (self.session_dir / "SEMANTIC_QUALITY.md").write_text("\n".join(sem_lines) + "\n", encoding="utf-8")

    def status(self) -> dict:
        with self.lock:
            now = time.time()
            age = None
            if self.last_ingest_wall:
                age = max(0.0, now - self.last_ingest_wall)
            matches = []
            for rec in self.current.values():
                db_match = self.db.match_row(rec.get("_master_match_uid")) if rec.get("_master_match_uid") else None
                matches.append({
                    "key": record_key(rec),
                    "source": rec.get("_source_platform") or "fortuna",
                    "source_label": rec.get("_source_label") or rec.get("_source_platform") or "fortuna",
                    "parser_family": rec.get("_parser_family") or "",
                    "source_url": rec.get("_source_url") or "",
                    "league": rec.get("league"), "format": rec.get("format"),
                    "status": rec.get("status"), "half": rec.get("half"), "minute": rec.get("minute"),
                    "team1": rec.get("team1"), "player1": rec.get("player1"),
                    "team2": rec.get("team2"), "player2": rec.get("player2"),
                    "score1": rec.get("score1"), "score2": rec.get("score2"),
                    "line": rec.get("line"), "under_odds": rec.get("under_odds"), "over_odds": rec.get("over_odds"),
                    "quality": rec.get("_score_quality"), "confidence": rec.get("_score_confidence"),
                    "analysis_allowed": bool(rec.get("_analysis_allowed", True)),
                    "pair_uid": rec.get("_pair_uid"), "match_uid": rec.get("_match_uid"),
                    "master_match_uid": rec.get("_master_match_uid"),
                    "data_completeness": rec.get("_data_completeness"),
                    "db_state": (db_match or {}).get("state"),
                    "quality_grade": (db_match or {}).get("quality_grade"),
                    "ready_for_analysis": bool((db_match or {}).get("ready_for_analysis", 0)),
                    "ready_score": bool((db_match or {}).get("ready_score", 0)),
                    "ready_clock": bool((db_match or {}).get("ready_clock", 0)),
                    "ready_market": bool((db_match or {}).get("ready_market", 0)),
                    "ready_live_model": bool((db_match or {}).get("ready_live_model", 0)),
                    "ready_live": bool((db_match or {}).get("ready_live", 0)),
                    "ready_ft": bool((db_match or {}).get("ready_ft", 0)),
                    "master_semantic_score": int((db_match or {}).get("semantic_score", 0) or 0),
                    "final_confirmed": bool((db_match or {}).get("final_confirmed", 0)),
                })

            sources = []
            for source, meta in sorted(self.source_status.items()):
                hb_wall = float(meta.get("last_heartbeat_wall") or 0.0)
                data_wall = float(meta.get("last_data_wall") or 0.0)
                hb_age = max(0.0, now - hb_wall) if hb_wall else None
                data_age = max(0.0, now - data_wall) if data_wall else None
                # Heartbeat pochodzi bezpośrednio z collectora w sterowanej karcie Edge.
                # Nie udajemy osobnego "host watchdog" z dawnego wariantu PC.
                if hb_age is None or hb_age >= 30:
                    conn = "offline"
                elif hb_age >= 10:
                    conn = "delayed"
                else:
                    conn = "online"
                page_alive = conn != "offline"
                collector_alive = conn != "offline"
                source_matches = list(self.current_by_source.get(source, {}).values())
                health = source_health(meta, connection=conn, heartbeat_age=hb_age, data_age=data_age, matches=source_matches)
                sem_meta = dict(meta)
                sem_meta["_semantic_data_age_seconds"] = data_age
                semq = semantic_score(sem_meta, source_matches)
                sources.append({
                    **meta,
                    "source": source,
                    "heartbeat_age_seconds": hb_age,
                    "data_age_seconds": data_age,
                    "last_real_data_age_seconds": data_age,
                    "last_nonempty_data_at": meta.get("last_nonempty") or meta.get("last_data"),
                    "host_watchdog_age_seconds": None,
                    "page_alive": page_alive,
                    "collector_alive": collector_alive,
                    "data_fresh": data_age is not None and data_age < 15.0,
                    "connection": conn,
                    "connected": conn != "offline",
                    "matches": len(source_matches),
                    **health,
                    **semq,
                })

            any_connected = any(x.get("connected") for x in sources)
            db_counts = self.db.counts(self.session_id) if self.session_id else {"db_raw_snapshot_rows": 0, "db_analysis_snapshot_rows": 0}
            active_health = [int(x.get("health_score") or 0) for x in sources if x.get("connected")]
            overall_health = round(sum(active_health) / len(active_health)) if active_health else 0
            return {
                "running": self.running,
                "session": self.session_id,
                "session_dir": str(self.session_dir or ""),
                "database": str(self.db.path),
                "storage_mode": "SQLITE_PRIMARY",
                "last_ingest": self.last_ingest_iso,
                "collector_age_seconds": age,
                "collector_connected": any_connected,
                "source_url": self.last_url,
                "title": self.last_title,
                "sources": sources,
                "gold_quality": {"overall_health": overall_health, "active_sources": len(active_health)},
                "semantic_quality": {
                    "overall_score": round(sum(int(x.get("semantic_score") or 0) for x in sources) / len(sources)) if sources else 0,
                    "sources": len(sources),
                },
                "matches": matches,
                "counters": {
                    "raw_packets": self.collector_count,
                    "accepted_packets": self.ingest_count,
                    "db_raw_snapshot_rows": int(db_counts.get("db_raw_snapshot_rows") or 0),
                    "db_analysis_snapshot_rows": int(db_counts.get("db_analysis_snapshot_rows") or 0),
                    "saved_snapshot_rows_csv": int(self.saved_snapshot_rows),
                    "saved_snapshot_batches": int(self.saved_snapshot_batches),
                    "goals": self.goal_count, "changes": self.change_count,
                    "anomaly_occurrences": self.anomaly_count,
                    "anomaly_families": len(self.anomaly_agg),
                    "duplicates": self.packet_duplicates,
                    "seq_gaps": self.packet_gaps, "recovered": self.packet_recovered,
                    "stale": self.packet_stale,
                },
                "recent_events": list(self.recent_events[:80]),
                "resources": dict(self._resource_last_sample),
                "last_error": self.last_error,
                "log_odds_changes": self.config.log_odds_changes,
            }

    def configure(self, *, snapshot_interval: Optional[float] = None, log_odds_changes: Optional[bool] = None):
        with self.lock:
            if snapshot_interval is not None:
                self.config.snapshot_interval = max(1.0, min(60.0, float(snapshot_interval)))
            if log_odds_changes is not None:
                self.config.log_odds_changes = bool(log_odds_changes)
            return self.status()
