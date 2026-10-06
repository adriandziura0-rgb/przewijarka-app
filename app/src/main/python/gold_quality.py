from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Iterable


def _norm(value) -> str:
    s = unicodedata.normalize("NFKD", str(value or ""))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^a-z0-9]+", " ", s.casefold()).strip()
    return re.sub(r"\s+", " ", s)


def pair_uid(league, player1, player2) -> str:
    """Cross-source PAIR identity. League is intentionally ignored.

    Different bookmakers often spell or decorate league labels differently. The pair
    id is therefore based only on normalized participants; a separate MASTER_MATCH_UID
    distinguishes repeated matches of the same pair within a session.
    """
    players = sorted([_norm(player1), _norm(player2)])
    raw = "|".join(players)
    return "pair_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def source_match_uid(session_id, source, raw_key, generation: int = 1) -> str:
    raw = f"{session_id}|{_norm(source)}|{_norm(raw_key)}|g{int(generation)}"
    return "match_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def record_completeness(rec: dict, collect: dict | None = None) -> float:
    collect = collect or {}
    groups: list[float] = []
    if collect.get("score", True):
        groups.append(1.0 if rec.get("score1") is not None and rec.get("score2") is not None else 0.0)
    if collect.get("clock", True):
        have = sum(rec.get(k) not in (None, "") for k in ("status", "half", "minute"))
        groups.append(have / 3.0)
    if collect.get("market", True):
        have = sum(rec.get(k) not in (None, "") for k in ("line", "under_odds", "over_odds"))
        groups.append(have / 3.0)
    groups.append(1.0 if rec.get("player1") and rec.get("player2") else 0.0)
    return round(sum(groups) / max(1, len(groups)), 3)


def average(values: Iterable[float]) -> float:
    vals = [float(x) for x in values]
    return sum(vals) / len(vals) if vals else 0.0


def source_health(meta: dict, *, connection: str, heartbeat_age, data_age, matches: list[dict]) -> dict:
    """Operational quality score (0-100), not a claim about bookmaker accuracy.

    It combines collector liveness, data freshness, transport reliability and
    current parser completeness/confidence. The components are exposed so the UI
    and audit can explain WHY a source is degraded.
    """
    reasons: list[str] = []

    if connection == "online":
        liveness = 30.0
    elif connection == "delayed":
        liveness = 18.0
        reasons.append("heartbeat opóźniony")
    else:
        liveness = 0.0
        reasons.append("brak heartbeat")

    if data_age is None:
        freshness = 0.0
        reasons.append("brak danych")
    elif data_age < 5:
        freshness = 20.0
    elif data_age < 15:
        freshness = 14.0
        reasons.append("dane >5 s")
    elif data_age < 45:
        freshness = 7.0
        reasons.append("dane mocno opóźnione")
    else:
        freshness = 0.0
        reasons.append("dane stare")

    queue = max(0, int(meta.get("queue_depth") or 0))
    dropped = max(0, int(meta.get("queue_dropped") or 0))
    gaps = max(0, int(meta.get("seq_gaps") or 0))
    stale = max(0, int(meta.get("stale_packets") or 0))
    dup = max(0, int(meta.get("duplicates") or 0))
    transport = 20.0
    transport -= min(8.0, queue * 0.8)
    transport -= min(6.0, dropped * 1.5)
    transport -= min(4.0, gaps * 0.5)
    transport -= min(3.0, stale * 0.5)
    transport -= min(2.0, dup * 0.1)
    transport = max(0.0, transport)
    if queue >= 5: reasons.append(f"kolejka Q{queue}")
    if dropped: reasons.append(f"utracone {dropped}")
    if gaps: reasons.append(f"luki seq {gaps}")
    if stale: reasons.append(f"stare pakiety {stale}")

    completenesses = [float(m.get("_data_completeness") or 0.0) for m in matches]
    confidences = []
    for m in matches:
        try:
            if m.get("score1") is not None and m.get("score2") is not None:
                confidences.append(float(m.get("_score_confidence") or 0.0))
        except (TypeError, ValueError):
            pass
    avg_complete = average(completenesses)
    avg_conf = average(confidences)
    if matches:
        parser_quality = 30.0 * (0.55 * avg_complete + 0.45 * avg_conf)
        if avg_complete < .7: reasons.append(f"kompletność {avg_complete*100:.0f}%")
        if confidences and avg_conf < .82: reasons.append(f"pewność wyniku {avg_conf*100:.0f}%")
    else:
        parser_quality = 12.0 if str(meta.get("data_state") or "") in {"EMPTY", "HOLD_ZERO"} else 0.0
        if str(meta.get("data_state") or "") == "HOLD_ZERO": reasons.append("HOLD_ZERO")

    if meta.get("last_error"):
        parser_quality = max(0.0, parser_quality - 8.0)
        reasons.append("błąd kolektora")

    score = int(round(max(0.0, min(100.0, liveness + freshness + transport + parser_quality))))
    if score >= 90: grade = "A"
    elif score >= 75: grade = "B"
    elif score >= 55: grade = "C"
    elif score >= 35: grade = "D"
    else: grade = "E"
    return {
        "health_score": score,
        "health_grade": grade,
        "health_reasons": reasons[:5],
        "health_components": {
            "liveness": round(liveness, 1),
            "freshness": round(freshness, 1),
            "transport": round(transport, 1),
            "parser_quality": round(parser_quality, 1),
        },
        "avg_completeness": round(avg_complete, 3),
        "avg_score_confidence": round(avg_conf, 3),
    }
