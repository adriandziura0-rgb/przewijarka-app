from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import sqlite3
import threading
import time
import unicodedata
from datetime import datetime
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

DB_SCHEMA_VERSION = 4


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def norm_name(value) -> str:
    s = unicodedata.normalize("NFKD", str(value or ""))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^a-z0-9]+", " ", s.casefold()).strip()
    return re.sub(r"\s+", " ", s)


def platform_from_record(rec: dict) -> str:
    text = " ".join(str(rec.get(k) or "") for k in ("league", "format", "_source_label", "_parser_family")).casefold()
    # Najpierw rodziny bardziej szczegółowe. Nazwa Superbet "Battle - Volta ..."
    # nie może zostać wrzucona do zwykłego BATTLE. Platforma jest częścią
    # tożsamości MASTER_MATCH_UID, więc UNKNOWN nie może też łączyć różnych lig.
    if "volta" in text:
        return "VOLTA"
    if re.search(r"(?:h2h|head\s*to\s*head)", text):
        return "H2H"
    if re.search(r"(?:\bgt\b|gt\s*league|esoccer\s*gt)", text):
        return "GT"
    if re.search(r"interactive\s+world\s+cup", text):
        return "IWC"
    if re.search(r"cyber\s+live\s+arena|(?:^|\s)cyber(?:\s|$)", text):
        return "CYBER"
    if re.search(r"(?:^|\s)eal\s*-", text):
        return "EAL"
    if re.search(r"(?:esports?\s*battle|esportsbattle|turkiye\s*super\s*lig|(?:^|\s)battle\s*-)", text):
        return "BATTLE"
    return "UNKNOWN"


def platform_prefix(platform: str) -> str:
    return {
        "BATTLE": "BAT", "GT": "GT", "VOLTA": "VOL", "H2H": "H2H",
        "IWC": "IWC", "CYBER": "CYB", "EAL": "EAL",
    }.get(str(platform or "").upper(), "UNK")


def format_seconds(fmt: str, league: str = "") -> Optional[int]:
    text = f"{fmt or ''} {league or ''}".casefold().replace("×", "x")
    m = re.search(r"2\s*x\s*(\d{1,2})", text)
    if m:
        return 2 * int(m.group(1)) * 60
    m = re.search(r"\b(\d{1,2})\s*(?:m|min|minutes?)\b", text)
    if m:
        return int(m.group(1)) * 60
    if "volta" in text:
        return 6 * 60
    if "h2h" in text:
        return 8 * 60
    if re.search(r"\bgt\b", text):
        return 12 * 60
    # Tylko klasyczne ESportsBattle/Turkiye ma bezpieczny fallback 2x4.
    # Superbet używa również nazw "Battle - ..." dla 2x6, więc genericzny
    # "battle = 8 min" dawałby fałszywy procent czasu, gdy format zniknie z DOM.
    if re.search(r"(?:esports?\s*battle|esportsbattle|turkiye\s*super\s*lig)", text):
        return 8 * 60
    return None


def best_match_second(rec: dict) -> tuple[Optional[int], float]:
    # Prefer a future parser-provided precise clock when available.
    for key in ("match_second", "elapsed_seconds"):
        try:
            v = rec.get(key)
            if v not in (None, ""):
                return max(0, int(float(v))), 1.0
        except Exception:
            pass
    minute = rec.get("minute")
    half = rec.get("half")
    try:
        minute = int(minute) if minute not in (None, "") else None
    except Exception:
        minute = None
    try:
        half = int(half) if half not in (None, "") else None
    except Exception:
        half = None
    total = format_seconds(str(rec.get("format") or ""), str(rec.get("league") or ""))
    clock_mode = str(rec.get("_clock_mode") or "")
    # Superbet pokazuje minutę jako absolutną minutę całego krótkiego meczu:
    # np. 2.Połowa6' w 2x4 oznacza 6/8, a nie 4 min + 6 min drugiej połowy.
    # Doliczony czas pierwszej połowy nie może wypchnąć postępu ponad 50%.
    if clock_mode == "absolute_match_minute" and minute is not None:
        sec = max(0, minute * 60)
        if total:
            if half == 1:
                sec = min(sec, total // 2)
            else:
                sec = min(sec, total)
        return sec, 0.95
    # Betsport/Betcris.PL presents eFootball on a soccer-like 0..90 minute clock
    # even when the real simulation lasts 6/8/10/12 minutes. Convert that
    # display clock to actual elapsed seconds instead of treating 72' as 72 real
    # minutes and clipping every second-half record to 100%.
    if clock_mode == "football_90" and minute is not None and total:
        display_minute = max(0.0, min(90.0, float(minute)))
        return int(round(total * display_minute / 90.0)), 0.95
    period_second = rec.get("clock_second_in_period")
    if period_second in (None, ""):
        cm = re.search(r"\b(\d{1,2}):(\d{2})\b", str(rec.get("status") or ""))
        if cm:
            period_second = int(cm.group(1)) * 60 + int(cm.group(2))
    try:
        if period_second not in (None, ""):
            sec = max(0, int(float(period_second)))
            if half == 2 and total:
                sec += total // 2
            if total:
                sec = min(sec, total)
            return sec, 0.9
    except Exception:
        pass
    if minute is None:
        return None, 0.0
    if half in (1, 2) and total:
        per_half = total // 2
        sec = minute * 60 + (per_half if half == 2 else 0)
        return min(sec, total), 0.65
    sec = minute * 60
    if total:
        sec = min(sec, total)
    return sec, 0.5


def progress_pct(rec: dict, match_second: Optional[int]) -> Optional[float]:
    total = format_seconds(str(rec.get("format") or ""), str(rec.get("league") or ""))
    if total and match_second is not None:
        return round(max(0.0, min(100.0, 100.0 * float(match_second) / total)), 2)
    return None


def market_state(rec: dict) -> str:
    s = str(rec.get("status") or "").casefold()
    if re.search(r"(?:suspend|wstrzym|zamkni|closed)", s):
        return "SUSPENDED" if "suspend" in s or "wstrzym" in s else "CLOSED"
    if rec.get("line") not in (None, "") or rec.get("under_odds") not in (None, "") or rec.get("over_odds") not in (None, ""):
        return "OPEN"
    return "UNKNOWN"


def db_state(rec: dict) -> str:
    s = str(rec.get("status") or "").casefold()
    if re.search(r"(?:finished|final|zako[nń]cz|koniec|full\s*time|\bft\b)", s):
        return "FINISHED"
    if rec.get("half") == 2 or re.search(r"(?:2\.\s*po[lł]|second\s*half|2nd\s*half)", s):
        return "2P"
    if re.search(r"(?:half\s*time|halftime|przerwa|\bht\b)", s):
        return "HT"
    return "ACTIVE"


def market_sanity(rec: dict) -> tuple[bool, str]:
    """Conservative validation for a full-match O/U market before model use.

    Raw values are still stored even when this gate fails.  The gate only decides
    whether ``ready_market`` / ``ready_live_model`` may become true.
    """
    try:
        line = float(rec.get("line"))
        over = float(rec.get("over_odds"))
        under = float(rec.get("under_odds"))
    except (TypeError, ValueError):
        return False, "missing_market"
    if not (0.0 <= line <= 40.0):
        return False, "line_range"
    if not (1.001 <= over <= 50.0 and 1.001 <= under <= 50.0):
        return False, "odds_range"
    total = rec.get("total_goals")
    if total is None and rec.get("score1") is not None and rec.get("score2") is not None:
        try:
            total = int(rec.get("score1")) + int(rec.get("score2"))
        except (TypeError, ValueError):
            total = None
    if total is not None and line + 1e-9 < float(total):
        return False, "line_below_score"
    implied = (1.0 / over) + (1.0 / under)
    if not (0.85 <= implied <= 1.35):
        return False, "odds_pair_incoherent"
    parser = str(rec.get("_parser_family") or "").casefold()
    # Fortuna's main total in the observed eSoccer feed is a half-goal line.
    # Integer values (notably 18) came from a neighbouring DOM number.
    if parser == "fortuna" and abs((line % 1.0) - 0.5) > 1e-6:
        return False, "fortuna_not_half_line"
    # Betsport/Betcris eFootball can expose Asian quarter lines; arbitrary decimals
    # such as 4.6831 are odds leaking into the line field and are never valid lines.
    if parser in {"betcris_pl", "betcris"} and abs(line * 4.0 - round(line * 4.0)) > 1e-6:
        return False, "betcris_not_quarter_line"
    if rec.get("_market_binding_suspect"):
        return False, "market_binding_suspect"
    if parser == "betcris_pl" and not bool(rec.get("_active_card_bound", False)):
        return False, "betcris_card_unbound"
    return True, "ok"


def readiness_flags(rec: dict, final_confirmed: bool = False) -> dict[str, int]:
    """Independent readiness gates for different downstream analyses.

    A record may be perfectly usable for score/goal analysis while lacking a market
    or a trustworthy clock.  Keeping separate flags prevents one broad
    ``analysis_allowed`` bit from silently promoting incomplete data into a model
    that needs more fields.
    """
    pair_ok = bool(rec.get("player1") and rec.get("player2"))
    score_ok = rec.get("score1") is not None and rec.get("score2") is not None
    match_second, _ = best_match_second(rec)
    clock_ok = match_second is not None
    market_ok, _market_reason = market_sanity(rec)
    format_ok = format_seconds(str(rec.get("format") or ""), str(rec.get("league") or "")) is not None
    ready_score = int(pair_ok and score_ok)
    ready_clock = int(bool(ready_score and clock_ok))
    ready_market = int(bool(ready_score and market_ok))
    ready_live_model = int(bool(ready_score and clock_ok and market_ok and format_ok))
    ready_ft = int(bool(final_confirmed and ready_score))
    return {
        "ready_score": ready_score,
        "ready_clock": ready_clock,
        "ready_market": ready_market,
        "ready_live_model": ready_live_model,
        "ready_ft": ready_ft,
    }


def analysis_grade(rec: dict, final_confirmed: bool = False) -> tuple[str, int, int, int]:
    flags = readiness_flags(rec, final_confirmed)
    ready_live = flags["ready_live_model"]
    ready_ft = flags["ready_ft"]
    ready = int(bool(flags["ready_score"] or ready_ft))
    grade = (
        "A_FULL" if ready_live and ready_ft
        else "B_LIVE" if ready_live
        else "C_FRAGMENT" if flags["ready_score"]
        else "REJECTED"
    )
    return grade, ready, ready_live, ready_ft


def raw_hash(rec: dict, source: str, source_match_uid: str) -> str:
    vals = [source, source_match_uid]
    for k in ("half", "minute", "match_second", "elapsed_seconds", "clock_second_in_period", "status", "score1", "score2", "line", "under_odds", "over_odds"):
        vals.append(rec.get(k))
    return hashlib.sha256(json.dumps(vals, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


class LiveDatabase:
    """Persistent SQLite source of truth for LIVE eSoccer.

    CSV/JSON files remain exports only. All master/source/snapshot/event identity is
    committed here first. SQLite WAL allows long-running collection without locking
    readers that inspect/export data concurrently.
    """

    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir)
        self.db_dir = self.base_dir / "database"
        self.db_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.db_dir / "przewijak.sqlite3"
        self.matches_dir = self.base_dir / "matches"
        self.matches_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self._transaction_depth = 0
        self.conn = sqlite3.connect(self.path, timeout=30.0, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self._init_schema()

    def _commit(self):
        if self._transaction_depth == 0:
            self.conn.commit()

    @contextmanager
    def transaction(self):
        """One SQLite transaction spanning a complete accepted collector packet.

        Database helper methods use _commit(), which becomes a no-op while this
        context is active. Nested calls therefore remain atomic without changing
        their public API. Any exception rolls every SQLite change from the packet
        back, so a collector retry cannot leave half-written snapshots/events.
        """
        with self.lock:
            outer = self._transaction_depth == 0
            if outer:
                self.conn.execute("BEGIN IMMEDIATE")
            self._transaction_depth += 1
            try:
                yield
            except Exception:
                self._transaction_depth -= 1
                if outer:
                    self.conn.rollback()
                raise
            else:
                self._transaction_depth -= 1
                if outer:
                    try:
                        self.conn.commit()
                    except Exception:
                        self.conn.rollback()
                        raise

    def close(self):
        with self.lock:
            try:
                if self._transaction_depth:
                    self.conn.rollback()
                    self._transaction_depth = 0
                else:
                    self.conn.commit()
                self.conn.close()
            except Exception:
                pass

    def _init_schema(self):
        schema = r'''
        CREATE TABLE IF NOT EXISTS meta (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS matches (
          master_match_uid TEXT PRIMARY KEY,
          platform TEXT NOT NULL DEFAULT 'UNKNOWN',
          competition TEXT,
          format TEXT,
          player1 TEXT,
          player2 TEXT,
          player1_norm TEXT,
          player2_norm TEXT,
          team1 TEXT,
          team2 TEXT,
          first_seen_at TEXT NOT NULL,
          last_seen_at TEXT NOT NULL,
          state TEXT NOT NULL DEFAULT 'ACTIVE',
          ht_score1 INTEGER,
          ht_score2 INTEGER,
          final_score1 INTEGER,
          final_score2 INTEGER,
          final_confirmed INTEGER NOT NULL DEFAULT 0,
          final_source TEXT,
          finish_reason TEXT,
          quality_grade TEXT NOT NULL DEFAULT 'C_FRAGMENT',
          analysis_quality INTEGER NOT NULL DEFAULT 0,
          semantic_score INTEGER NOT NULL DEFAULT 0,
          ready_for_analysis INTEGER NOT NULL DEFAULT 0,
          ready_score INTEGER NOT NULL DEFAULT 0,
          ready_clock INTEGER NOT NULL DEFAULT 0,
          ready_market INTEGER NOT NULL DEFAULT 0,
          ready_live_model INTEGER NOT NULL DEFAULT 0,
          ready_live INTEGER NOT NULL DEFAULT 0,
          ready_ft INTEGER NOT NULL DEFAULT 0,
          last_score1 INTEGER,
          last_score2 INTEGER,
          last_match_second INTEGER,
          last_progress_pct REAL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS source_matches (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          master_match_uid TEXT NOT NULL REFERENCES matches(master_match_uid) ON DELETE CASCADE,
          source TEXT NOT NULL,
          source_match_uid TEXT NOT NULL,
          source_url TEXT,
          player1_raw TEXT,
          player2_raw TEXT,
          orientation INTEGER NOT NULL DEFAULT 1,
          first_seen_at TEXT NOT NULL,
          last_seen_at TEXT NOT NULL,
          heartbeat_at TEXT,
          last_nonempty_data_at TEXT,
          parser_version TEXT,
          transport_quality INTEGER,
          semantic_quality INTEGER,
          active INTEGER NOT NULL DEFAULT 1,
          last_score1 INTEGER,
          last_score2 INTEGER,
          last_match_second INTEGER,
          explicit_finished INTEGER NOT NULL DEFAULT 0,
          UNIQUE(source, source_match_uid)
        );
        CREATE TABLE IF NOT EXISTS raw_snapshots (
          raw_snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
          session_id TEXT,
          master_match_uid TEXT NOT NULL REFERENCES matches(master_match_uid) ON DELETE CASCADE,
          source TEXT NOT NULL,
          source_match_uid TEXT NOT NULL,
          captured_at TEXT NOT NULL,
          captured_at_ms INTEGER NOT NULL,
          period TEXT,
          match_second INTEGER,
          display_minute INTEGER,
          progress_pct REAL,
          score1 INTEGER,
          score2 INTEGER,
          total_goals INTEGER,
          goal_difference INTEGER,
          leader TEXT,
          line REAL,
          under_odds REAL,
          over_odds REAL,
          market_state TEXT,
          status_raw TEXT,
          score_confidence REAL,
          clock_confidence REAL,
          line_confidence REAL,
          semantic_quality INTEGER,
          raw_hash TEXT NOT NULL,
          parser_version TEXT,
          source_url TEXT
        );
        CREATE TABLE IF NOT EXISTS snapshots (
          snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
          raw_snapshot_id INTEGER REFERENCES raw_snapshots(raw_snapshot_id) ON DELETE SET NULL,
          session_id TEXT,
          master_match_uid TEXT NOT NULL REFERENCES matches(master_match_uid) ON DELETE CASCADE,
          platform TEXT,
          source TEXT NOT NULL,
          source_match_uid TEXT NOT NULL,
          captured_at TEXT NOT NULL,
          captured_at_ms INTEGER NOT NULL,
          period TEXT,
          match_second INTEGER,
          display_minute INTEGER,
          progress_pct REAL,
          score1 INTEGER,
          score2 INTEGER,
          total_goals INTEGER,
          goal_difference INTEGER,
          leader TEXT,
          line REAL,
          under_odds REAL,
          over_odds REAL,
          market_state TEXT,
          status_raw TEXT,
          score_confidence REAL,
          clock_confidence REAL,
          line_confidence REAL,
          semantic_quality INTEGER,
          ready_score INTEGER NOT NULL DEFAULT 0,
          ready_clock INTEGER NOT NULL DEFAULT 0,
          ready_market INTEGER NOT NULL DEFAULT 0,
          ready_live_model INTEGER NOT NULL DEFAULT 0,
          ready_ft INTEGER NOT NULL DEFAULT 0,
          raw_hash TEXT NOT NULL,
          parser_version TEXT,
          line_minus_goals REAL,
          goals_needed_over INTEGER,
          score_diff INTEGER,
          sec_to_next_goal REAL,
          goals_next_30s INTEGER,
          goals_next_60s INTEGER,
          goals_next_90s INTEGER,
          goals_remaining_ft INTEGER,
          over_hit_ft INTEGER,
          under_hit_ft INTEGER
        );
        CREATE TABLE IF NOT EXISTS events (
          event_id INTEGER PRIMARY KEY AUTOINCREMENT,
          master_match_uid TEXT NOT NULL REFERENCES matches(master_match_uid) ON DELETE CASCADE,
          event_at TEXT NOT NULL,
          match_second INTEGER,
          event_type TEXT NOT NULL,
          source TEXT,
          old_value TEXT,
          new_value TEXT,
          score1 INTEGER,
          score2 INTEGER,
          confidence REAL,
          raw_snapshot_id INTEGER REFERENCES raw_snapshots(raw_snapshot_id) ON DELETE SET NULL
        );
        CREATE TABLE IF NOT EXISTS goals (
          goal_id INTEGER PRIMARY KEY AUTOINCREMENT,
          master_match_uid TEXT NOT NULL REFERENCES matches(master_match_uid) ON DELETE CASCADE,
          goal_no INTEGER NOT NULL,
          goal_at TEXT NOT NULL,
          match_second INTEGER,
          period TEXT,
          score_before1 INTEGER,
          score_before2 INTEGER,
          score_after1 INTEGER,
          score_after2 INTEGER,
          scorer_side TEXT,
          detected_source TEXT,
          detected_directly INTEGER NOT NULL DEFAULT 0,
          event_kind TEXT NOT NULL DEFAULT 'GOAL',
          goals_delta INTEGER NOT NULL DEFAULT 1,
          uncertainty_ms INTEGER,
          line_before REAL,
          over_before REAL,
          under_before REAL,
          line_after REAL,
          line_reaction_ms INTEGER,
          raw_snapshot_id INTEGER REFERENCES raw_snapshots(raw_snapshot_id) ON DELETE SET NULL,
          UNIQUE(master_match_uid, goal_no)
        );
        CREATE TABLE IF NOT EXISTS anomalies (
          anomaly_id INTEGER PRIMARY KEY AUTOINCREMENT,
          master_match_uid TEXT REFERENCES matches(master_match_uid) ON DELETE CASCADE,
          source TEXT,
          type TEXT NOT NULL,
          old_value TEXT,
          new_value TEXT,
          first_seen_at TEXT NOT NULL,
          last_seen_at TEXT NOT NULL,
          occurrences INTEGER NOT NULL DEFAULT 1,
          resolved INTEGER NOT NULL DEFAULT 0,
          raw_snapshot_id INTEGER REFERENCES raw_snapshots(raw_snapshot_id) ON DELETE SET NULL,
          UNIQUE(master_match_uid, source, type, old_value, new_value, resolved)
        );
        CREATE INDEX IF NOT EXISTS idx_snap_match_time ON snapshots(master_match_uid, captured_at);
        CREATE INDEX IF NOT EXISTS idx_snap_platform_time ON snapshots(platform, captured_at);
        CREATE INDEX IF NOT EXISTS idx_events_match_time ON events(master_match_uid, event_at);
        CREATE INDEX IF NOT EXISTS idx_source_uid ON source_matches(source, source_match_uid);
        CREATE INDEX IF NOT EXISTS idx_matches_players ON matches(player1_norm, player2_norm);
        CREATE INDEX IF NOT EXISTS idx_matches_ready ON matches(ready_for_analysis);
        CREATE INDEX IF NOT EXISTS idx_raw_match_time ON raw_snapshots(master_match_uid, captured_at);
        CREATE INDEX IF NOT EXISTS idx_goals_match_time ON goals(master_match_uid, goal_at);
        CREATE INDEX IF NOT EXISTS idx_matches_state_time ON matches(state, last_seen_at);
        CREATE VIEW IF NOT EXISTS analysis_snapshots AS SELECT * FROM snapshots;
        '''
        with self.lock:
            self.conn.executescript(schema)
            # MASTER ids are stored directly in matches; no sequence allocator is used.
            self.conn.execute("DROP TABLE IF EXISTS uid_sequence")
            # In-place schema migration for users keeping an existing output database.
            goal_cols = {str(r[1]) for r in self.conn.execute("PRAGMA table_info(goals)").fetchall()}
            if "event_kind" not in goal_cols:
                self.conn.execute("ALTER TABLE goals ADD COLUMN event_kind TEXT NOT NULL DEFAULT 'GOAL'")
            if "goals_delta" not in goal_cols:
                self.conn.execute("ALTER TABLE goals ADD COLUMN goals_delta INTEGER NOT NULL DEFAULT 1")

            match_cols = {str(r[1]) for r in self.conn.execute("PRAGMA table_info(matches)").fetchall()}
            for name, ddl in (
                ("semantic_score", "INTEGER NOT NULL DEFAULT 0"),
                ("ready_score", "INTEGER NOT NULL DEFAULT 0"),
                ("ready_clock", "INTEGER NOT NULL DEFAULT 0"),
                ("ready_market", "INTEGER NOT NULL DEFAULT 0"),
                ("ready_live_model", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if name not in match_cols:
                    self.conn.execute(f"ALTER TABLE matches ADD COLUMN {name} {ddl}")

            snap_cols = {str(r[1]) for r in self.conn.execute("PRAGMA table_info(snapshots)").fetchall()}
            for name in ("ready_score", "ready_clock", "ready_market", "ready_live_model", "ready_ft"):
                if name not in snap_cols:
                    self.conn.execute(f"ALTER TABLE snapshots ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0")

            self.conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version',?)", (str(DB_SCHEMA_VERSION),))
            self._commit()

    @staticmethod
    def stable_master_uid(platform: str, session_id: str, source_match_uid: str, ts: Optional[str] = None) -> str:
        """Deterministic master id for the first source that creates a fixture.

        A failed SQLite packet is retried with the same source_match_uid.  The old
        sequence allocator could therefore produce a different MASTER_MATCH_UID after
        rollback/retry.  A deterministic seed makes identity survive DB_RETRY while
        cross-source sources can still attach to the same existing master in
        resolve_master().
        """
        ts = ts or now_iso()
        date = re.sub(r"[^0-9]", "", ts[:10])[:8]
        seed = f"{session_id}|{source_match_uid}"
        digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12].upper()
        return f"{platform_prefix(platform)}_{date}_{digest}"

    @staticmethod
    def _master_oriented_score(match_row, rec: dict) -> tuple[Optional[int], Optional[int], int]:
        a = norm_name(rec.get("player1") or rec.get("team1"))
        b = norm_name(rec.get("player2") or rec.get("team2"))
        p1, p2 = norm_name(match_row["player1"]), norm_name(match_row["player2"])
        s1, s2 = rec.get("score1"), rec.get("score2")
        orientation = 1
        if a == p2 and b == p1:
            orientation = -1
            s1, s2 = s2, s1
        return s1, s2, orientation

    def resolve_master(self, *, session_id: str, source: str, source_match_uid: str, rec: dict,
                       source_url: str = "", parser_version: str = "", semantic_quality: int = 100,
                       transport_quality: Optional[int] = None, force_new: bool = False) -> tuple[str, int]:
        """Resolve one source fixture to an immutable MASTER_MATCH_UID.

        The unordered login pair is the primary identity.  Cross-source attachment is
        then gated by recency, platform/format compatibility, score direction and
        normalized match clock.  ``force_new`` means *this source* started a new
        generation; it no longer disables cross-source matching completely.  Instead
        it prevents re-attaching to a master that already contains an older generation
        of the same source while still allowing the new source generation to join a
        live master created by another bookmaker.
        """
        ts = now_iso()
        p1 = str(rec.get("player1") or rec.get("team1") or "")
        p2 = str(rec.get("player2") or rec.get("team2") or "")
        n1, n2 = norm_name(p1), norm_name(p2)
        platform = platform_from_record(rec)
        fmt = str(rec.get("format") or "")
        competition = str(rec.get("league") or "")
        match_second, _ = best_match_second(rec)
        score1, score2 = rec.get("score1"), rec.get("score2")

        with self.lock:
            row = self.conn.execute(
                "SELECT master_match_uid,orientation FROM source_matches WHERE source=? AND source_match_uid=?",
                (source, source_match_uid),
            ).fetchone()
            if row:
                master_uid = str(row["master_match_uid"])
                self.conn.execute(
                    "UPDATE source_matches SET last_seen_at=?,last_nonempty_data_at=?,source_url=?,parser_version=?,semantic_quality=?,transport_quality=?,active=1 WHERE source=? AND source_match_uid=?",
                    (ts, ts, source_url, parser_version, semantic_quality, transport_quality, source, source_match_uid),
                )
                self.conn.execute(
                    """UPDATE matches SET
                       platform=CASE WHEN platform='UNKNOWN' AND ?!='UNKNOWN' THEN ? ELSE platform END,
                       competition=CASE WHEN COALESCE(competition,'')='' AND ?!='' THEN ? ELSE competition END,
                       format=CASE WHEN COALESCE(format,'')='' AND ?!='' THEN ? ELSE format END,
                       team1=CASE WHEN COALESCE(team1,'')='' AND ?!='' THEN ? ELSE team1 END,
                       team2=CASE WHEN COALESCE(team2,'')='' AND ?!='' THEN ? ELSE team2 END
                       WHERE master_match_uid=?""",
                    (platform, platform, competition, competition, fmt, fmt,
                     str(rec.get("team1") or ""), str(rec.get("team1") or ""),
                     str(rec.get("team2") or ""), str(rec.get("team2") or ""), master_uid),
                )
                self._commit()
                return master_uid, int(row["orientation"] or 1)

            candidate = None
            orientation = 1
            candidate_score = -10_000.0
            if n1 and n2:
                pair = sorted((n1, n2))
                rows = self.conn.execute(
                    """SELECT * FROM matches
                       WHERE ((player1_norm=? AND player2_norm=?) OR (player1_norm=? AND player2_norm=?))
                         AND state IN ('ACTIVE','HT','2P','DISAPPEARED','DISAPPEARED_UNCONFIRMED')
                       ORDER BY last_seen_at DESC LIMIT 16""",
                    (pair[0], pair[1], pair[1], pair[0]),
                ).fetchall()
                now_ms = int(time.time() * 1000)
                for m in rows:
                    master_uid0 = str(m["master_match_uid"])
                    recent = self.conn.execute(
                        "SELECT captured_at_ms FROM raw_snapshots WHERE master_match_uid=? ORDER BY raw_snapshot_id DESC LIMIT 1",
                        (master_uid0,),
                    ).fetchone()
                    age_ms = now_ms - int(recent[0]) if recent else 10**9
                    if age_ms > 300_000:
                        continue

                    same_source_history = bool(self.conn.execute(
                        "SELECT 1 FROM source_matches WHERE master_match_uid=? AND source=? LIMIT 1",
                        (master_uid0, source),
                    ).fetchone())
                    active_other = int(self.conn.execute(
                        "SELECT COUNT(*) FROM source_matches WHERE master_match_uid=? AND source<>? AND active=1",
                        (master_uid0, source),
                    ).fetchone()[0] or 0)

                    existing_platform = str(m["platform"] or "UNKNOWN")
                    parser_family = str(rec.get("_parser_family") or "").casefold()
                    trusted_unknown_esoccer = (
                        platform == "UNKNOWN"
                        and parser_family in {"betcris", "betcris_pl"}
                        and bool(rec.get("_sport_valid"))
                    )
                    platform_mismatch = False
                    if platform == "UNKNOWN":
                        if existing_platform != "UNKNOWN" and not trusted_unknown_esoccer:
                            platform_mismatch = bool(
                                competition and m["competition"]
                                and norm_name(competition) != norm_name(m["competition"])
                            )
                    elif existing_platform not in ("UNKNOWN", platform):
                        platform_mismatch = True

                    format_mismatch = bool(fmt and m["format"] and norm_name(fmt) != norm_name(m["format"]))

                    ms1, ms2, ori = self._master_oriented_score(m, rec)
                    if ms1 == 0 and ms2 == 0 and ((m["last_score1"] or 0) + (m["last_score2"] or 0) > 0):
                        continue

                    # Clock evidence is normalized to real match seconds before this
                    # comparison, including Betsport's football-like 0..90 display.
                    clock_diff = None
                    if match_second is not None and m["last_match_second"] is not None:
                        clock_diff = abs(int(match_second) - int(m["last_match_second"]))
                        if clock_diff > 150:
                            continue

                    score_evidence = 0.0
                    if ms1 is not None and ms2 is not None and m["last_score1"] is not None and m["last_score2"] is not None:
                        a1, a2 = int(ms1), int(ms2)
                        b1, b2 = int(m["last_score1"]), int(m["last_score2"])
                        ta, tb = a1 + a2, b1 + b2
                        if ta == tb and (a1, a2) != (b1, b2):
                            continue
                        if ta < tb:
                            if tb - ta > 2 or a1 > b1 or a2 > b2:
                                continue
                        elif ta > tb:
                            if ta - tb > 2 or a1 < b1 or a2 < b2:
                                continue
                        if (a1, a2) == (b1, b2):
                            score_evidence = 5.0
                        elif abs(ta - tb) == 1:
                            score_evidence = 3.0
                        else:
                            score_evidence = 1.0

                    generation_reason = str(rec.get("_generation_reason") or "")
                    strong_new_match = bool(rec.get("_strong_new_match"))
                    exact_score = score_evidence >= 5.0
                    strong_cross = bool(
                        active_other and age_ms <= 120_000 and score_evidence >= 3.0
                        and (clock_diff is None or clock_diff <= 90)
                    )
                    # First packets from a second bookmaker often arrive before its clock,
                    # format or score is parsed. If another source is actively tracking the
                    # exact login pair RIGHT NOW, pair-only attachment for a short window is
                    # safer than permanently creating a duplicate master.
                    pair_only_cross = bool(
                        active_other and age_ms <= 20_000 and not strong_new_match
                        and (clock_diff is None or clock_diff <= 90)
                    )

                    # A new source generation is not automatically a new real match. A gap
                    # or DOM rerender may restart the source UID while another bookmaker is
                    # still following the same fixture. Reattach only on strong independent
                    # evidence; a real score/clock reset remains a hard split unless the
                    # other live source already agrees with the new state.
                    if force_new and same_source_history:
                        if strong_new_match:
                            if not strong_cross:
                                continue
                        elif not (strong_cross or pair_only_cross):
                            continue

                    if platform_mismatch and not strong_cross:
                        continue
                    if format_mismatch and not strong_cross:
                        continue

                    # Refuse weak pair-only matching unless this is the short live overlap
                    # window above. Exact login pair alone is otherwise insufficient when a
                    # five-player rotation repeats the same pairing later.
                    if score_evidence <= 0 and clock_diff is None and not pair_only_cross:
                        continue

                    q = 0.0
                    q += 4.0 if age_ms <= 30_000 else 3.0 if age_ms <= 90_000 else 2.0 if age_ms <= 180_000 else 1.0
                    q += score_evidence
                    if clock_diff is not None:
                        q += 4.0 if clock_diff <= 30 else 3.0 if clock_diff <= 60 else 1.0
                    if existing_platform == platform and platform != "UNKNOWN":
                        q += 2.0
                    elif existing_platform == "UNKNOWN" or platform == "UNKNOWN":
                        q += 1.0
                    if fmt and m["format"] and norm_name(fmt) == norm_name(m["format"]):
                        q += 2.0
                    if active_other:
                        q += 2.0
                    if strong_cross:
                        q += 4.0
                    elif pair_only_cross:
                        q += 2.0
                    if format_mismatch:
                        q -= 1.0
                    if platform_mismatch:
                        q -= 1.0

                    if q > candidate_score:
                        candidate_score = q
                        candidate, orientation = m, ori

            if candidate is None:
                master_uid = self.stable_master_uid(platform, session_id, source_match_uid, ts)
                grade, ready, ready_live, ready_ft = analysis_grade(rec, False)
                flags = readiness_flags(rec, False)
                self.conn.execute(
                    """INSERT OR IGNORE INTO matches(master_match_uid,platform,competition,format,player1,player2,player1_norm,player2_norm,team1,team2,
                       first_seen_at,last_seen_at,state,quality_grade,ready_for_analysis,ready_score,ready_clock,ready_market,ready_live_model,ready_live,ready_ft,
                       last_score1,last_score2,last_match_second,last_progress_pct,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (master_uid, platform, competition, fmt, p1, p2, n1, n2, rec.get("team1"), rec.get("team2"),
                     ts, ts, db_state(rec), grade, ready, flags["ready_score"], flags["ready_clock"], flags["ready_market"],
                     flags["ready_live_model"], ready_live, ready_ft, score1, score2, match_second,
                     progress_pct(rec, match_second), ts, ts),
                )
                orientation = 1
            else:
                master_uid = str(candidate["master_match_uid"])
                self.conn.execute(
                    """UPDATE matches SET
                       platform=CASE WHEN platform='UNKNOWN' AND ?!='UNKNOWN' THEN ? ELSE platform END,
                       competition=CASE WHEN COALESCE(competition,'')='' AND ?!='' THEN ? ELSE competition END,
                       format=CASE WHEN COALESCE(format,'')='' AND ?!='' THEN ? ELSE format END,
                       team1=CASE WHEN COALESCE(team1,'')='' AND ?!='' THEN ? ELSE team1 END,
                       team2=CASE WHEN COALESCE(team2,'')='' AND ?!='' THEN ? ELSE team2 END
                       WHERE master_match_uid=?""",
                    (platform, platform, competition, competition, fmt, fmt,
                     str(rec.get("team1") or ""), str(rec.get("team1") or ""),
                     str(rec.get("team2") or ""), str(rec.get("team2") or ""), master_uid),
                )

            self.conn.execute(
                """INSERT INTO source_matches(master_match_uid,source,source_match_uid,source_url,player1_raw,player2_raw,orientation,first_seen_at,last_seen_at,heartbeat_at,last_nonempty_data_at,parser_version,transport_quality,semantic_quality,active)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                (master_uid, source, source_match_uid, source_url, p1, p2, orientation, ts, ts, ts, ts,
                 parser_version, transport_quality, semantic_quality),
            )
            self._commit()
            return master_uid, orientation

    def update_source_heartbeat(self, source: str, heartbeat_at: Optional[str] = None):
        heartbeat_at = heartbeat_at or now_iso()
        with self.lock:
            self.conn.execute("UPDATE source_matches SET heartbeat_at=? WHERE source=? AND active=1", (heartbeat_at, source))
            self._commit()

    def set_source_inactive(self, source: str, source_match_uids: set[str]):
        with self.lock:
            if source_match_uids:
                qs = ",".join("?" for _ in source_match_uids)
                self.conn.execute(f"UPDATE source_matches SET active=0 WHERE source=? AND source_match_uid NOT IN ({qs})", (source, *source_match_uids))
            else:
                self.conn.execute("UPDATE source_matches SET active=0 WHERE source=?", (source,))
            self._commit()

    def mark_disappeared(self, master_uid: str, source: str, reason: str = "disappeared_unconfirmed"):
        if not master_uid:
            return
        with self.lock:
            # Source disappearance never confirms FT. Keep a confirmed match finished.
            self.conn.execute(
                """UPDATE matches SET
                   state=CASE WHEN final_confirmed=1 THEN 'FINISHED_CONFIRMED' ELSE 'DISAPPEARED_UNCONFIRMED' END,
                   finish_reason=CASE WHEN final_confirmed=1 THEN finish_reason ELSE ? END, updated_at=?
                   WHERE master_match_uid=?""",
                (reason, now_iso(), master_uid),
            )
            self.conn.execute("UPDATE source_matches SET active=0 WHERE master_match_uid=?", (master_uid,))
            self._commit()

    def _snapshot_values(self, rec: dict, source: str, source_match_uid: str, semantic_quality: int, parser_version: str, source_url: str):
        ts = str(rec.get('_captured_at') or now_iso())
        try:
            captured_ms = int(rec.get('_captured_at_ms') or 0)
        except Exception:
            captured_ms = 0
        if captured_ms <= 0:
            captured_ms = int(time.time() * 1000)
        sec, clock_conf = best_match_second(rec)
        prog = progress_pct(rec, sec)
        s1, s2 = rec.get("score1"), rec.get("score2")
        total = (int(s1) + int(s2)) if s1 is not None and s2 is not None else None
        diff = abs(int(s1) - int(s2)) if s1 is not None and s2 is not None else None
        leader = "DRAW" if s1 is not None and s2 is not None and int(s1) == int(s2) else "P1" if s1 is not None and s2 is not None and int(s1) > int(s2) else "P2" if s1 is not None and s2 is not None else None
        half = rec.get("half")
        period = "1P" if half == 1 else "2P" if half == 2 else "HT" if re.search(r"(?:half\s*time|halftime|przerwa|\bht\b)", str(rec.get("status") or ""), re.I) else None
        try:
            score_conf = float(rec.get("_score_confidence") or 0.0)
        except Exception:
            score_conf = 0.0
        line_conf = 1.0 if rec.get("line") is not None and rec.get("over_odds") is not None and rec.get("under_odds") is not None else 0.6 if rec.get("line") is not None else 0.0
        rh = raw_hash(rec, source, source_match_uid)
        return dict(ts=ts, captured_ms=captured_ms, sec=sec, clock_conf=clock_conf, prog=prog, s1=s1, s2=s2,
                    total=total, diff=diff, leader=leader, period=period, score_conf=score_conf, line_conf=line_conf,
                    semantic_quality=int(semantic_quality or 0), raw_hash=rh, parser_version=parser_version, source_url=source_url)

    def _aggregate_master_semantic(self, master_uid: str) -> int:
        rows = self.conn.execute(
            "SELECT semantic_quality,transport_quality FROM source_matches WHERE master_match_uid=?",
            (master_uid,),
        ).fetchall()
        vals = []
        for row in rows:
            try:
                q = max(0.0, min(100.0, float(row["semantic_quality"] or 0)))
            except Exception:
                continue
            try:
                tq = max(20.0, min(100.0, float(row["transport_quality"] or 100)))
            except Exception:
                tq = 100.0
            vals.append((q, tq))
        if not vals:
            return 0
        weighted = sum(q * w for q, w in vals) / sum(w for _, w in vals)
        weakest = min(q for q, _ in vals)
        # A weak source must be visible in the master score, but one imperfect feed
        # should not erase high-quality corroboration from another bookmaker.
        return int(round(max(0.0, min(100.0, 0.70 * weighted + 0.30 * weakest))))

    def record_snapshot(self, *, session_id: str, master_uid: str, source: str, source_match_uid: str, rec: dict,
                        semantic_quality: int = 100, parser_version: str = "", source_url: str = "",
                        transport_quality: Optional[int] = None, trusted_clock: bool = True, trusted_market: bool = True,
                        raw_rec: Optional[dict] = None) -> tuple[int, Optional[int]]:
        raw_rec = dict(raw_rec or rec)
        v = self._snapshot_values(raw_rec, source, source_match_uid, semantic_quality, parser_version, source_url)
        analysis_rec = dict(rec)
        analysis_allowed = bool(rec.get("_analysis_allowed", True))
        if not trusted_clock:
            for _clock_field in ("status", "half", "minute", "clock_second_in_period", "match_second", "elapsed_seconds"):
                analysis_rec[_clock_field] = None
        if not trusted_market:
            analysis_rec["line"] = analysis_rec["under_odds"] = analysis_rec["over_odds"] = None
        av = self._snapshot_values(analysis_rec, source, source_match_uid, semantic_quality, parser_version, source_url)

        with self.lock:
            cur = self.conn.execute(
                """INSERT INTO raw_snapshots(session_id,master_match_uid,source,source_match_uid,captured_at,captured_at_ms,period,match_second,display_minute,progress_pct,score1,score2,total_goals,goal_difference,leader,line,under_odds,over_odds,market_state,status_raw,score_confidence,clock_confidence,line_confidence,semantic_quality,raw_hash,parser_version,source_url)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (session_id, master_uid, source, source_match_uid, v["ts"], v["captured_ms"], v["period"], v["sec"], raw_rec.get("minute"), v["prog"], v["s1"], v["s2"], v["total"], v["diff"], v["leader"], raw_rec.get("line"), raw_rec.get("under_odds"), raw_rec.get("over_odds"), market_state(raw_rec), raw_rec.get("status"), v["score_conf"], v["clock_conf"], v["line_conf"], v["semantic_quality"], v["raw_hash"], v["parser_version"], source_url),
            )
            raw_id = int(cur.lastrowid)

            orientation_row = self.conn.execute(
                "SELECT orientation FROM source_matches WHERE source=? AND source_match_uid=?",
                (source, source_match_uid),
            ).fetchone()
            orientation = int(orientation_row[0]) if orientation_row else 1
            as1, as2 = av["s1"], av["s2"]
            if orientation == -1:
                as1, as2 = as2, as1
            aleader = (
                "DRAW" if as1 is not None and as2 is not None and int(as1) == int(as2)
                else "P1" if as1 is not None and as2 is not None and int(as1) > int(as2)
                else "P2" if as1 is not None and as2 is not None
                else None
            )

            # A rendered "Finished" label alone is not enough. The engine sets
            # _final_confirmed only after the trusted-score consistency gate.
            final_now = bool(analysis_allowed and rec.get("_final_confirmed", False))
            flags = readiness_flags(analysis_rec, final_now)
            if not analysis_allowed:
                flags = {k: 0 for k in ("ready_score", "ready_clock", "ready_market", "ready_live_model", "ready_ft")}
            grade, ready, ready_live, ready_ft = analysis_grade(analysis_rec, final_now)
            if not analysis_allowed:
                grade, ready, ready_live, ready_ft = "REJECTED", 0, 0, 0
            state = "FINISHED_CONFIRMED" if final_now else db_state(analysis_rec)

            prev = self.conn.execute(
                "SELECT raw_hash FROM snapshots WHERE master_match_uid=? AND source=? ORDER BY snapshot_id DESC LIMIT 1",
                (master_uid, source),
            ).fetchone()
            snap_id = None
            if analysis_allowed and (not prev or str(prev["raw_hash"]) != av["raw_hash"]):
                line_minus = (float(analysis_rec.get("line")) - av["total"]) if analysis_rec.get("line") is not None and av["total"] is not None else None
                needed = max(0, math.floor(float(analysis_rec.get("line"))) + 1 - av["total"]) if analysis_rec.get("line") is not None and av["total"] is not None else None
                cur2 = self.conn.execute(
                    """INSERT INTO snapshots(raw_snapshot_id,session_id,master_match_uid,platform,source,source_match_uid,captured_at,captured_at_ms,period,match_second,display_minute,progress_pct,score1,score2,total_goals,goal_difference,leader,line,under_odds,over_odds,market_state,status_raw,score_confidence,clock_confidence,line_confidence,semantic_quality,ready_score,ready_clock,ready_market,ready_live_model,ready_ft,raw_hash,parser_version,line_minus_goals,goals_needed_over,score_diff)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (raw_id, session_id, master_uid, platform_from_record(rec), source, source_match_uid, av["ts"], av["captured_ms"], av["period"], av["sec"], analysis_rec.get("minute"), av["prog"], as1, as2, av["total"], av["diff"], aleader, analysis_rec.get("line"), analysis_rec.get("under_odds"), analysis_rec.get("over_odds"), market_state(analysis_rec), analysis_rec.get("status"), av["score_conf"], av["clock_conf"], av["line_conf"], av["semantic_quality"], flags["ready_score"], flags["ready_clock"], flags["ready_market"], flags["ready_live_model"], flags["ready_ft"], av["raw_hash"], av["parser_version"], line_minus, needed, av["diff"]),
                )
                snap_id = int(cur2.lastrowid)

            ms1, ms2 = as1, as2
            if not analysis_allowed:
                ms1 = ms2 = None
            analysis_q = int(round(100 * (
                0.35 * min(1.0, max(0.0, av["score_conf"]))
                + 0.25 * min(1.0, max(0.0, av["clock_conf"]))
                + 0.25 * min(1.0, max(0.0, av["line_conf"]))
                + 0.15 * min(1.0, max(0.0, av["semantic_quality"] / 100.0))
            ))) if analysis_allowed else 0

            # MASTER score is monotonic. A later stale/source-misaligned snapshot
            # may not move a confirmed live score backwards.
            score_update1 = score_update2 = None
            if ms1 is not None and ms2 is not None:
                prev_master = self.conn.execute(
                    "SELECT last_score1,last_score2 FROM matches WHERE master_match_uid=?",
                    (master_uid,),
                ).fetchone()
                if prev_master is None or prev_master["last_score1"] is None or prev_master["last_score2"] is None:
                    score_update1, score_update2 = int(ms1), int(ms2)
                else:
                    p1, p2 = int(prev_master["last_score1"]), int(prev_master["last_score2"])
                    n1, n2 = int(ms1), int(ms2)
                    if n1 >= p1 and n2 >= p2:
                        score_update1, score_update2 = n1, n2

            self.conn.execute(
                """UPDATE matches SET
                   last_seen_at=?,
                   state=CASE WHEN final_confirmed=1 THEN 'FINISHED_CONFIRMED'
                              WHEN state IN ('DISAPPEARED','DISAPPEARED_UNCONFIRMED') THEN ?
                              WHEN state='2P' AND ? IN ('ACTIVE','HT') THEN state
                              WHEN state='HT' AND ?='ACTIVE' THEN state
                              ELSE ? END,
                   quality_grade=CASE
                       WHEN quality_grade='A_FULL' OR ?='A_FULL' THEN 'A_FULL'
                       WHEN quality_grade='B_LIVE' OR ?='B_LIVE' THEN 'B_LIVE'
                       WHEN quality_grade='C_FRAGMENT' OR ?='C_FRAGMENT' THEN 'C_FRAGMENT'
                       ELSE ? END,
                   analysis_quality=MAX(analysis_quality,?),
                   ready_for_analysis=MAX(ready_for_analysis,?),
                   ready_score=MAX(ready_score,?),
                   ready_clock=MAX(ready_clock,?),
                   ready_market=MAX(ready_market,?),
                   ready_live_model=MAX(ready_live_model,?),
                   ready_live=MAX(ready_live,?),
                   ready_ft=MAX(ready_ft,?),
                   last_score1=COALESCE(?,last_score1),last_score2=COALESCE(?,last_score2),
                   last_match_second=COALESCE(?,last_match_second),last_progress_pct=COALESCE(?,last_progress_pct),updated_at=?
                   WHERE master_match_uid=?""",
                (av["ts"], state, state, state, state, grade, grade, grade, grade, analysis_q, ready,
                 flags["ready_score"], flags["ready_clock"], flags["ready_market"], flags["ready_live_model"],
                 ready_live, ready_ft, score_update1, score_update2, av["sec"], av["prog"], av["ts"], master_uid),
            )

            status_text = str(rec.get("status") or "").casefold()
            if ms1 is not None and ms2 is not None and re.search(r"(?:half\s*time|halftime|przerwa|\bht\b)", status_text):
                self.conn.execute(
                    "UPDATE matches SET ht_score1=COALESCE(ht_score1,?),ht_score2=COALESCE(ht_score2,?) WHERE master_match_uid=?",
                    (ms1, ms2, master_uid),
                )

            self.conn.execute(
                """UPDATE source_matches SET last_seen_at=?,heartbeat_at=?,last_nonempty_data_at=?,parser_version=?,transport_quality=?,semantic_quality=?,active=1,last_score1=?,last_score2=?,last_match_second=? WHERE source=? AND source_match_uid=?""",
                (v["ts"], v["ts"], v["ts"], parser_version, transport_quality, v["semantic_quality"], av["s1"], av["s2"], av["sec"], source, source_match_uid),
            )
            master_semantic = self._aggregate_master_semantic(master_uid)
            self.conn.execute(
                "UPDATE matches SET semantic_score=?,updated_at=? WHERE master_match_uid=?",
                (master_semantic, av["ts"], master_uid),
            )
            self._commit()
            return raw_id, snap_id

    def add_event(self, *, master_uid: str, event_type: str, source: str, rec: dict, old_value=None, new_value=None,
                  confidence: Optional[float] = None, raw_snapshot_id: Optional[int] = None, event_at: Optional[str] = None):
        if not master_uid:
            return
        event_at = event_at or now_iso()
        sec, _ = best_match_second(rec)
        etype = str(event_type).upper()
        with self.lock:
            if etype in {"HT", "2P", "FT"}:
                exists = self.conn.execute(
                    "SELECT 1 FROM events WHERE master_match_uid=? AND event_type=? LIMIT 1",
                    (master_uid, etype),
                ).fetchone()
                if exists:
                    return
            mrow = self.conn.execute("SELECT * FROM matches WHERE master_match_uid=?", (master_uid,)).fetchone()
            es1, es2 = rec.get("score1"), rec.get("score2")
            if mrow:
                es1, es2, _ = self._master_oriented_score(mrow, rec)
            self.conn.execute(
                "INSERT INTO events(master_match_uid,event_at,match_second,event_type,source,old_value,new_value,score1,score2,confidence,raw_snapshot_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (master_uid, event_at, sec, etype, source, None if old_value is None else str(old_value), None if new_value is None else str(new_value), es1, es2, confidence, raw_snapshot_id),
            )
            # First market update after a goal is the bookmaker reaction.
            if etype == "LINE" and new_value not in (None, ""):
                g = self.conn.execute(
                    "SELECT goal_id,goal_at FROM goals WHERE master_match_uid=? AND line_after IS NULL ORDER BY goal_id DESC LIMIT 1",
                    (master_uid,),
                ).fetchone()
                if g:
                    try:
                        goal_ms = int(datetime.fromisoformat(str(g["goal_at"])).timestamp() * 1000)
                        event_ms = int(datetime.fromisoformat(event_at).timestamp() * 1000)
                        reaction = max(0, event_ms - goal_ms)
                    except Exception:
                        reaction = None
                    self.conn.execute("UPDATE goals SET line_after=?,line_reaction_ms=? WHERE goal_id=?", (new_value, reaction, g["goal_id"]))
            self._commit()

    def add_goal(self, *, master_uid: str, source: str, old_rec: dict, new_rec: dict, uncertainty_seconds: Optional[float], raw_snapshot_id: Optional[int]):
        """Persist one factual score transition at MASTER level.

        delta == 1 is an exact GOAL event. delta > 1 is one CATCHUP event; it is
        never expanded into invented goal moments. UNIQUE(master_match_uid, goal_no)
        plus overlap checks deduplicate confirmations from other bookmakers.
        """
        old_s1, old_s2 = old_rec.get("score1"), old_rec.get("score2")
        new_s1, new_s2 = new_rec.get("score1"), new_rec.get("score2")
        if not master_uid or None in (old_s1, old_s2, new_s1, new_s2):
            return 0
        with self.lock:
            source_row = self.conn.execute(
                "SELECT orientation FROM source_matches WHERE source=? AND master_match_uid=? ORDER BY id DESC LIMIT 1",
                (source, master_uid),
            ).fetchone()
            ori = int(source_row["orientation"] or 1) if source_row else 1
            if ori == -1:
                old_s1, old_s2, new_s1, new_s2 = old_s2, old_s1, new_s2, new_s1
            old_s1, old_s2, new_s1, new_s2 = map(int, (old_s1, old_s2, new_s1, new_s2))
            old_total, new_total = old_s1 + old_s2, new_s1 + new_s2
            delta = new_total - old_total
            if delta <= 0:
                return 0

            # Never overlap an already stored exact/catchup event covering this score range.
            overlap = self.conn.execute(
                """SELECT goal_id,event_kind,goal_no,score_before1,score_before2,score_after1,score_after2
                   FROM goals WHERE master_match_uid=? AND goal_no>? AND goal_no<=? ORDER BY goal_id LIMIT 1""",
                (master_uid, old_total, new_total),
            ).fetchone()
            if overlap:
                return 0

            sec, _ = best_match_second(new_rec)
            period = "1P" if new_rec.get("half") == 1 else "2P" if new_rec.get("half") == 2 else None
            goal_at = str(new_rec.get('_captured_at') or now_iso())
            uncertainty_ms = int(float(uncertainty_seconds or 0) * 1000)
            d1, d2 = new_s1 - old_s1, new_s2 - old_s2
            event_kind = "GOAL" if delta == 1 else "CATCHUP"
            direct = int(delta == 1)
            scorer = None
            if delta == 1:
                if d1 == 1 and d2 == 0:
                    scorer = "P1"
                elif d2 == 1 and d1 == 0:
                    scorer = "P2"

            line_before = old_rec.get("line")
            line_after = new_rec.get("line") if new_rec.get("line") != old_rec.get("line") else None
            reaction_ms = 0 if line_after is not None and delta == 1 else None
            cur = self.conn.execute(
                """INSERT OR IGNORE INTO goals(
                       master_match_uid,goal_no,goal_at,match_second,period,
                       score_before1,score_before2,score_after1,score_after2,scorer_side,
                       detected_source,detected_directly,event_kind,goals_delta,uncertainty_ms,
                       line_before,over_before,under_before,line_after,line_reaction_ms,raw_snapshot_id
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (master_uid, new_total, goal_at, sec, period, old_s1, old_s2, new_s1, new_s2,
                 scorer, source, direct, event_kind, delta, uncertainty_ms, line_before,
                 old_rec.get("over_odds"), old_rec.get("under_odds"), line_after, reaction_ms, raw_snapshot_id),
            )
            inserted = max(0, int(cur.rowcount or 0))

            # Exact future-goal labels use only direct +1 events. Catch-up gaps are
            # retained for audit but never assigned fabricated exact goal moments.
            goals = self.conn.execute(
                "SELECT goal_id,goal_at FROM goals WHERE master_match_uid=? AND event_kind='GOAL' ORDER BY goal_id",
                (master_uid,),
            ).fetchall()
            snap_rows = self.conn.execute(
                "SELECT snapshot_id,captured_at_ms FROM snapshots WHERE master_match_uid=?",
                (master_uid,),
            ).fetchall()
            goal_times = []
            for g in goals:
                try:
                    goal_times.append(int(datetime.fromisoformat(str(g["goal_at"])).timestamp() * 1000))
                except Exception:
                    pass
            for snap in snap_rows:
                t = int(snap["captured_at_ms"])
                fut = [gt for gt in goal_times if gt > t]
                sec_next = round((fut[0] - t) / 1000.0, 3) if fut else None
                self.conn.execute(
                    "UPDATE snapshots SET sec_to_next_goal=?,goals_next_30s=?,goals_next_60s=?,goals_next_90s=? WHERE snapshot_id=?",
                    (sec_next, sum(gt <= t + 30000 for gt in fut), sum(gt <= t + 60000 for gt in fut),
                     sum(gt <= t + 90000 for gt in fut), snap["snapshot_id"]),
                )
            self._commit()
            return inserted

    def add_anomaly(self, *, master_uid: Optional[str], source: str, anomaly_type: str, old_value=None, new_value=None, raw_snapshot_id: Optional[int] = None):
        ts = now_iso()
        old_t = "" if old_value is None else str(old_value)
        new_t = "" if new_value is None else str(new_value)
        with self.lock:
            row = self.conn.execute(
                "SELECT anomaly_id,occurrences FROM anomalies WHERE master_match_uid IS ? AND source=? AND type=? AND old_value=? AND new_value=? AND resolved=0",
                (master_uid, source, anomaly_type, old_t, new_t),
            ).fetchone()
            if row:
                self.conn.execute("UPDATE anomalies SET last_seen_at=?,occurrences=occurrences+1,raw_snapshot_id=COALESCE(?,raw_snapshot_id) WHERE anomaly_id=?", (ts, raw_snapshot_id, row["anomaly_id"]))
            else:
                self.conn.execute(
                    "INSERT INTO anomalies(master_match_uid,source,type,old_value,new_value,first_seen_at,last_seen_at,occurrences,resolved,raw_snapshot_id) VALUES(?,?,?,?,?,?,?,1,0,?)",
                    (master_uid, source, anomaly_type, old_t, new_t, ts, ts, raw_snapshot_id),
                )
            self._commit()

    def trusted_master_score(self, master_uid: str) -> tuple[Optional[int], Optional[int]]:
        """Return the last monotonic analysis score in MASTER orientation."""
        with self.lock:
            row = self.conn.execute(
                "SELECT last_score1,last_score2 FROM matches WHERE master_match_uid=?",
                (master_uid,),
            ).fetchone()
            if not row or row["last_score1"] is None or row["last_score2"] is None:
                return None, None
            return int(row["last_score1"]), int(row["last_score2"])


    def confirm_final(self, *, master_uid: str, score1: int, score2: int, source: str, reason: str):
        ts = now_iso()
        with self.lock:
            self.conn.execute(
                """UPDATE matches SET state='FINISHED_CONFIRMED',final_score1=?,final_score2=?,final_confirmed=1,final_source=?,finish_reason=?,quality_grade=CASE WHEN ready_live_model=1 THEN 'A_FULL' ELSE quality_grade END,ready_score=1,ready_ft=1,ready_for_analysis=1,updated_at=? WHERE master_match_uid=?""",
                (score1, score2, source, reason, ts, master_uid),
            )
            final_total = int(score1) + int(score2)
            rows = self.conn.execute("SELECT snapshot_id,total_goals,line FROM snapshots WHERE master_match_uid=?", (master_uid,)).fetchall()
            for r in rows:
                remaining = final_total - int(r["total_goals"]) if r["total_goals"] is not None else None
                over = under = None
                if r["line"] is not None:
                    over = int(final_total > float(r["line"]))
                    under = int(final_total < float(r["line"]))
                self.conn.execute("UPDATE snapshots SET goals_remaining_ft=?,over_hit_ft=?,under_hit_ft=? WHERE snapshot_id=?", (remaining, over, under, r["snapshot_id"]))
            self._commit()

    def counts(self, session_id: str | None = None) -> dict:
        """Physical SQLite row counts with names that describe the storage layer."""
        with self.lock:
            if session_id:
                raw = self.conn.execute("SELECT COUNT(*) FROM raw_snapshots WHERE session_id=?", (session_id,)).fetchone()[0]
                analysis = self.conn.execute("SELECT COUNT(*) FROM snapshots WHERE session_id=?", (session_id,)).fetchone()[0]
            else:
                raw = self.conn.execute("SELECT COUNT(*) FROM raw_snapshots").fetchone()[0]
                analysis = self.conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
            return {"db_raw_snapshot_rows": int(raw or 0), "db_analysis_snapshot_rows": int(analysis or 0)}

    def match_row(self, master_uid: str):
        with self.lock:
            row = self.conn.execute("SELECT * FROM matches WHERE master_match_uid=?", (master_uid,)).fetchone()
            return dict(row) if row else None

    def oriented_score(self, master_uid: str, rec: dict) -> tuple[Optional[int], Optional[int]]:
        with self.lock:
            row = self.conn.execute("SELECT * FROM matches WHERE master_match_uid=?", (master_uid,)).fetchone()
            if not row:
                return rec.get("score1"), rec.get("score2")
            s1, s2, _ = self._master_oriented_score(row, rec)
            return s1, s2

    def export_match_files(self, master_uid: str):
        """Regenerate small human-readable exports under matches/<MASTER_MATCH_UID>/.

        The database remains authoritative. Files are disposable views for inspection.
        """
        if not master_uid:
            return
        out = self.matches_dir / master_uid
        out.mkdir(parents=True, exist_ok=True)
        with self.lock:
            match = self.conn.execute("SELECT * FROM matches WHERE master_match_uid=?", (master_uid,)).fetchone()
            if not match:
                return
            sources = self.conn.execute("SELECT * FROM source_matches WHERE master_match_uid=? ORDER BY id", (master_uid,)).fetchall()
            timeline = self.conn.execute("SELECT * FROM events WHERE master_match_uid=? ORDER BY event_id", (master_uid,)).fetchall()
            goals = self.conn.execute("SELECT * FROM goals WHERE master_match_uid=? ORDER BY goal_no", (master_uid,)).fetchall()
        (out / "summary.json").write_text(json.dumps(dict(match), ensure_ascii=False, indent=2), encoding="utf-8")
        (out / "sources.json").write_text(json.dumps([dict(x) for x in sources], ensure_ascii=False, indent=2), encoding="utf-8")
        self._write_csv(out / "timeline.csv", [dict(x) for x in timeline])
        self._write_csv(out / "goals.csv", [dict(x) for x in goals])

    def export_global_views(self):
        exports = self.base_dir / "exports"
        exports.mkdir(parents=True, exist_ok=True)
        with self.lock:
            data = {
                "MECZE.csv": self.conn.execute("SELECT * FROM matches ORDER BY first_seen_at, master_match_uid").fetchall(),
                "ZRODLA.csv": self.conn.execute("SELECT * FROM source_matches ORDER BY id").fetchall(),
                "GOLE.csv": self.conn.execute("SELECT * FROM goals ORDER BY goal_id").fetchall(),
                "ANALIZA.csv": self.conn.execute("SELECT * FROM snapshots ORDER BY snapshot_id").fetchall(),
                "ANOMALIE.csv": self.conn.execute("SELECT * FROM anomalies ORDER BY anomaly_id").fetchall(),
            }
        for name, rows in data.items():
            self._write_csv(exports / name, [dict(x) for x in rows])

    @staticmethod
    def _write_csv(path: Path, rows: list[dict]):
        if not rows:
            if not path.exists():
                path.write_text("", encoding="utf-8-sig")
            return
        fields = list(rows[0].keys())
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields, delimiter=";", extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
