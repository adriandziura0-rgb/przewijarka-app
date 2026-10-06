from __future__ import annotations

import re
import unicodedata

from live_database import market_sanity


def _norm(value) -> str:
    s = unicodedata.normalize("NFKD", str(value or ""))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"\s+", " ", s.casefold()).strip()
    return s


# UI/status words that must never become a player/login. Keep this list strict:
# rejecting a bad participant is safer than creating a false MASTER_MATCH_UID.
_BAD_PARTICIPANT = re.compile(
    r"(?:^|\b)(?:przerwa|break|pauza|zako[nń]czony|zako[nń]czone|finished|full\s*time|informacje|info|edytuj\s+zak(?:ł|l)ad|"
    r"tw[oó]j\s+kupon|kupon\s+jest\s+pusty|dla\s+medi[oó]w|regulamin(?:y)?|"
    r"odpowiedzialna\s+gra|legalny\s+polski\s+bukmacher|zaloguj|login|cash\s*out|"
    r"1\.?\s*po[lł](?:owa|\.)?|2\.?\s*po[lł](?:owa|\.)?|half|period|quarter|kwarta|"
    r"nie\s*rozpocz[eę]to|live|in[- ]?play|zak[lł]ady|bukmacher|hazard|"
    r"powy[zż]ej|poni[zż]ej|over|under|total|handicap|moneyline|kurs|odds)(?:\b|$)",
    re.I,
)

_STATUSISH = re.compile(
    r"^\s*(?:przerwa|break|[12]\.?\s*po[lł](?:owa|\.)?|\d{1,3}\s*['m]|\d{1,2}:\d{2}|"
    r"nie\s*rozpocz[eę]to)\b",
    re.I,
)

# Sport/category labels that can be glued to a team when a broad DOM row is read.
_SPORT_PREFIX = (
    r"badminton|tenis\s+sto[lł]owy|table\s+tennis|hokej\s+na\s+trawie|field\s+hockey|e-?koszyk[oó]wka|e-?hokej(?:\s+na\s+lodzie)?|"
    r"koszyk[oó]wka|hokej(?:\s+na\s+lodzie)?|tenis|siatk[oó]wka|pi[lł]ka\s+r[eę]czna|"
    r"baseball|rugby|cricket|darts|snooker|mma|boks|e-?basketball|e-?ice\s+hockey"
)
_TEAM_PREFIX_NOISE = re.compile(rf"^\s*(?:(?:{_SPORT_PREFIX})\s+){{1,4}}", re.I)
_TEAM_DUP_PREFIX = re.compile(r"^\s*([A-Za-zÀ-ž-]{3,30}(?:\s+[A-Za-zÀ-ž-]{3,30})?)\s+\1\s+", re.I)

_PAGE_CHROME = re.compile(
    r"(?:tw[oó]j\s+kupon|kupon\s+jest\s+pusty|dla\s+medi[oó]w|edytuj\s+zak(?:ł|l)ad|"
    r"regulamin(?:y)?|odpowiedzialna\s+gra|legalny\s+polski\s+bukmacher|"
    r"zaloguj|login|polityka\s+prywatno[sś]ci|cookies?|kontakt|pomoc|cash\s*out)",
    re.I,
)

_ESCOCCER_MARKER = re.compile(
    r"(?:e-?football|efootball|e-?soccer|esoccer|esports?\s*battle|esportsbattle|"
    r"h2h|gg\s*league|gt\s*league|volta|interactive\s+world\s+cup|cyber\s+live\s+arena|eal\s*-)",
    re.I,
)
_ORDINARY_FOOTBALL_URL = re.compile(r"/(?:soccer|football)/(?:world|poland|europe|international|club|league)(?:/|$)", re.I)
_FORMAT_RE = re.compile(r"(?:\b[234]\s*[x×]\s*\d{1,2}\b|\b(?:6|7|8|9|10|11|12)\s*(?:m|min)\b|volta|h2h|gt\s*league)", re.I)


def participant_ok(value) -> bool:
    s = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(s) < 2 or len(s) > 72:
        return False
    if not re.search(r"[A-Za-zÀ-ž]", s):
        return False
    if _BAD_PARTICIPANT.search(s) or _STATUSISH.search(s) or _PAGE_CHROME.search(s):
        return False
    if len(re.findall(r"\d", s)) > 5:
        return False
    if "http://" in s.casefold() or "https://" in s.casefold():
        return False
    return True


def clean_team_name(value) -> tuple[str, bool]:
    """Remove category labels accidentally glued to a team, without inventing a team."""
    original = re.sub(r"\s+", " ", str(value or "")).strip()
    s = original
    for _ in range(4):
        n = _TEAM_DUP_PREFIX.sub("", s).strip()
        if n == s:
            break
        s = n
    for _ in range(3):
        n = _TEAM_PREFIX_NOISE.sub("", s).strip(" -–—·")
        if n == s:
            break
        s = n
    s = re.sub(r"\s+", " ", s).strip()
    if not s or len(s) < 2:
        return original, False
    return s, s != original


def team_valid(value) -> bool:
    s = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(s) < 2 or len(s) > 96:
        return False
    if _PAGE_CHROME.search(s) or _BAD_PARTICIPANT.search(s):
        return False
    if _TEAM_PREFIX_NOISE.search(s) or _TEAM_DUP_PREFIX.search(s):
        return False
    return bool(re.search(r"[A-Za-zÀ-ž]", s))


def context_field_valid(value, *, kind: str) -> bool:
    s = re.sub(r"\s+", " ", str(value or "")).strip()
    if not s:
        return False
    if len(s) > (120 if kind == "league" else 80):
        return False
    if _PAGE_CHROME.search(s):
        return False
    if kind == "format":
        return bool(_FORMAT_RE.search(s))
    return bool(_ESCOCCER_MARKER.search(s) or re.search(r"\b(?:battle|world\s+cup|cyber|eal)\b", s, re.I))


def _sanitize_context(rec: dict, parser_family: str) -> dict:
    rec = dict(rec)
    team_cleanups = 0
    for fld in ("team1", "team2"):
        cleaned, changed = clean_team_name(rec.get(fld))
        if changed:
            rec[fld] = cleaned
            team_cleanups += 1
    if team_cleanups:
        rec["_team_cleaned"] = True
        rec["_team_cleanups_count"] = team_cleanups

    # Betcris/Betsport can capture broad page chrome into league/format. Never pass
    # those strings through to MASTER identity. Blank uncertain context instead.
    if parser_family in {"betcris", "betcris_pl"}:
        if rec.get("league") and not context_field_valid(rec.get("league"), kind="league"):
            rec["_league_rejected"] = True
            rec["league"] = ""
        if rec.get("format") and not context_field_valid(rec.get("format"), kind="format"):
            rec["_format_rejected"] = True
            rec["format"] = ""
    return rec


def esoccer_record_valid(rec: dict, parser_family: str = "auto", source_url: str = "") -> tuple[bool, str]:
    parser_family = str(parser_family or "auto").casefold()
    if parser_family not in {"betcris", "betcris_pl"}:
        return True, ""
    url = str(source_url or "")
    joined = " ".join(str(rec.get(k) or "") for k in ("league", "format", "status", "team1", "team2", "player1", "player2"))
    if _ORDINARY_FOOTBALL_URL.search(url) and not _ESCOCCER_MARKER.search(url):
        return False, "not_esoccer_url"
    if _ESCOCCER_MARKER.search(url) or _ESCOCCER_MARKER.search(joined):
        return True, ""
    p1, p2 = _norm(rec.get("player1")), _norm(rec.get("player2"))
    t1, t2 = _norm(rec.get("team1")), _norm(rec.get("team2"))
    if p1 and p2 and t1 and t2 and p1 != t1 and p2 != t2:
        return True, ""
    return False, "not_esoccer"


def stable_pair_key(player1, player2, team1=None, team2=None) -> str:
    a = _norm(player1) or _norm(team1)
    b = _norm(player2) or _norm(team2)
    return f"{a}|{b}"


def cross_source_pair_key(player1, player2, team1=None, team2=None) -> str:
    vals = [_norm(player1) or _norm(team1), _norm(player2) or _norm(team2)]
    vals.sort()
    return "|".join(vals)


def semantic_filter(records: list[dict], parser_family: str = "auto", source_url: str = "") -> tuple[list[dict], dict]:
    accepted: list[dict] = []
    rejected = 0
    cleaned_teams = 0
    bad_context = 0
    reasons: dict[str, int] = {}

    def reject(reason: str):
        nonlocal rejected
        rejected += 1
        reasons[reason] = reasons.get(reason, 0) + 1

    for source_rec in records or []:
        if not isinstance(source_rec, dict):
            reject("not_object")
            continue
        before = dict(source_rec)
        rec = _sanitize_context(source_rec, parser_family)
        cleaned_teams += int(rec.get("_team_cleanups_count") or 0)
        if rec.get("_league_rejected") or rec.get("_format_rejected"):
            bad_context += 1

        p1, p2 = rec.get("player1"), rec.get("player2")
        if not participant_ok(p1) or not participant_ok(p2):
            reject("bad_participant")
            continue
        if _norm(p1) == _norm(p2):
            reject("same_participant")
            continue
        if not team_valid(rec.get("team1")) or not team_valid(rec.get("team2")):
            reject("bad_team")
            continue

        sport_ok, sport_reason = esoccer_record_valid(rec, parser_family, source_url)
        rec["_sport_valid"] = bool(sport_ok)
        if not sport_ok:
            reject(sport_reason or "not_esoccer")
            continue

        if parser_family in {"betcris_pl", "betcris"}:
            line = rec.get("line")
            try:
                if line not in (None, "") and not (0.5 <= float(line) <= 20.5):
                    rec["line"] = rec["under_odds"] = rec["over_odds"] = None
            except (TypeError, ValueError):
                rec["line"] = rec["under_odds"] = rec["over_odds"] = None
            for fld in ("under_odds", "over_odds"):
                try:
                    v = rec.get(fld)
                    if v not in (None, "") and not (1.01 <= float(v) <= 20.0):
                        rec[fld] = None
                except (TypeError, ValueError):
                    rec[fld] = None

        rec["key"] = stable_pair_key(rec.get("player1"), rec.get("player2"), rec.get("team1"), rec.get("team2"))
        rec["_semantic_pair_key"] = cross_source_pair_key(rec.get("player1"), rec.get("player2"), rec.get("team1"), rec.get("team2"))
        rec["_player_valid"] = True
        rec["_team_valid"] = True
        rec["_league_valid"] = context_field_valid(rec.get("league"), kind="league")
        rec["_format_valid"] = context_field_valid(rec.get("format"), kind="format")
        rec["_semantic_changed"] = rec != before
        accepted.append(rec)

    total = len(records or [])
    quality = (len(accepted) / total) if total else 0.0
    return accepted, {
        "observed": total,
        "accepted": len(accepted),
        "rejected": rejected,
        "acceptance": round(quality, 3),
        "team_cleanups": cleaned_teams,
        "bad_context": bad_context,
        "reasons": reasons,
    }


def _ratio(matches: list[dict], fn) -> float:
    if not matches:
        return 0.0
    return sum(1 for m in matches if fn(m)) / len(matches)


def semantic_score(meta: dict, matches: list[dict]) -> dict:
    observed = max(0, int(meta.get("semantic_observed") or 0))
    accepted = max(0, int(meta.get("semantic_accepted") or 0))
    rejected = max(0, int(meta.get("semantic_rejected") or 0))
    score_regressions = max(0, int(meta.get("score_regressions") or 0))
    cleaned_teams = max(0, int(meta.get("team_cleanups") or 0))
    bad_context = max(0, int(meta.get("bad_context") or 0))
    anomaly_types = meta.get("semantic_anomaly_types") or {}
    if isinstance(anomaly_types, dict):
        anomaly_families = len([k for k, v in anomaly_types.items() if int(v or 0) > 0])
        anomaly_occurrences = sum(int(v or 0) for v in anomaly_types.values())
    else:
        anomaly_families = max(0, int(meta.get("semantic_anomalies") or 0))
        anomaly_occurrences = anomaly_families

    if observed <= 0 and not matches:
        return {
            "semantic_score": 0, "semantic_grade": "E", "semantic_acceptance": 0.0,
            "semantic_player_validity": 0.0, "semantic_team_validity": 0.0,
            "semantic_team_cleanliness": 0.0, "semantic_context_cleanliness": 0.0,
            "semantic_pair_ratio": 0.0, "semantic_uid_stability": 0.0,
            "semantic_score_stability": 0.0, "semantic_score_validity": 0.0,
            "semantic_sport_validity": 0.0, "semantic_league_validity": 0.0,
            "semantic_format_validity": 0.0, "semantic_clock_validity": 0.0,
            "semantic_market_validity": 0.0, "semantic_freshness": 0.0,
            "semantic_anomaly_rate": 1.0, "semantic_anomaly_families": 0,
            "semantic_rejected": rejected, "semantic_team_cleanups": cleaned_teams,
            "semantic_bad_context": bad_context,
        }

    acceptance = accepted / observed if observed else 0.0
    player_valid = _ratio(matches, lambda m: participant_ok(m.get("player1")) and participant_ok(m.get("player2")))
    team_validity = _ratio(matches, lambda m: team_valid(m.get("team1")) and team_valid(m.get("team2")))
    team_cleanliness = max(0.0, 1.0 - (cleaned_teams / max(1, accepted * 2)))
    context_cleanliness = max(0.0, 1.0 - (bad_context / max(1, accepted * 2)))
    pair_ratio = _ratio(matches, lambda m: bool(m.get("_semantic_pair_key")))
    uid_count = len({m.get("_match_uid") for m in matches if m.get("_match_uid")})
    logical_pairs = len({m.get("_semantic_pair_key") for m in matches if m.get("_semantic_pair_key")})
    uid_stability = 1.0 if logical_pairs and uid_count <= logical_pairs else (logical_pairs / uid_count if uid_count else 0.0)
    score_stability = max(0.0, 1.0 - (score_regressions / max(1, accepted)))
    score_valid = _ratio(matches, lambda m: m.get("score1") is not None and m.get("score2") is not None and bool(m.get("_analysis_allowed", True)))
    sport_valid = _ratio(matches, lambda m: bool(m.get("_sport_valid", True)))
    league_valid = _ratio(matches, lambda m: context_field_valid(m.get("league"), kind="league"))
    format_valid = _ratio(matches, lambda m: context_field_valid(m.get("format"), kind="format"))
    clock_valid = _ratio(matches, lambda m: m.get("minute") not in (None, "") or m.get("half") not in (None, "") or bool(m.get("status")))
    market_valid = _ratio(matches, lambda m: market_sanity(m)[0])

    data_age = meta.get("_semantic_data_age_seconds")
    try:
        data_age = float(data_age) if data_age is not None else None
    except Exception:
        data_age = None
    if data_age is None:
        freshness = 0.0
    elif data_age < 15:
        freshness = 1.0
    elif data_age < 45:
        freshness = 0.6
    elif data_age < 180:
        freshness = 0.25
    else:
        freshness = 0.0

    # Repeated CLOCK_BACKWARD 6->1 is one semantic family with occurrences, not 31
    # independent defect classes. Occurrence rate is retained separately for audit.
    anomaly_rate = anomaly_occurrences / max(1, accepted + rejected)
    anomaly_family_quality = max(0.0, 1.0 - min(1.0, anomaly_families / 6.0))

    components = {
        "acceptance": acceptance,
        "player": player_valid,
        "team": team_validity,
        "team_cleanliness": team_cleanliness,
        "uid": uid_stability,
        "score_stability": score_stability,
        "score_validity": score_valid,
        "sport": sport_valid,
        "league": league_valid,
        "format": format_valid,
        "context_cleanliness": context_cleanliness,
        "clock": clock_valid,
        "market": market_valid,
        "anomaly": anomaly_family_quality,
        "freshness": freshness,
    }
    weights = {
        "acceptance": .05, "player": .10, "team": .06, "team_cleanliness": .06,
        "uid": .08, "score_stability": .12, "score_validity": .16, "sport": .10,
        "league": .03, "format": .04, "context_cleanliness": .04, "clock": .06,
        "market": .05, "anomaly": .02, "freshness": .03,
    }
    score = round(100 * sum(components[k] * weights[k] for k in weights))
    score = max(0, min(100, score))

    # Hard caps make 100/A impossible when core semantics are wrong.
    if player_valid < 1.0 or team_validity < 1.0:
        score = min(score, 79)
    if sport_valid < 0.95:
        score = min(score, 49)
    if score_valid < 0.50:
        score = min(score, 49)
    if market_valid < 0.25:
        score = min(score, 84)
    if league_valid < 0.50 or format_valid < 0.50:
        score = min(score, 84)
    if team_cleanliness < 1.0 or context_cleanliness < 1.0:
        score = min(score, 89)
    if acceptance < 0.50:
        score = min(score, 59)
    if freshness == 0.0:
        score = min(score, 69)

    grade = "A" if score >= 90 else "B" if score >= 80 else "C" if score >= 65 else "D" if score >= 50 else "E"
    return {
        "semantic_score": score,
        "semantic_grade": grade,
        "semantic_acceptance": round(acceptance, 3),
        "semantic_player_validity": round(player_valid, 3),
        "semantic_team_validity": round(team_validity, 3),
        "semantic_team_cleanliness": round(team_cleanliness, 3),
        "semantic_context_cleanliness": round(context_cleanliness, 3),
        "semantic_pair_ratio": round(pair_ratio, 3),
        "semantic_uid_stability": round(uid_stability, 3),
        "semantic_score_stability": round(score_stability, 3),
        "semantic_score_validity": round(score_valid, 3),
        "semantic_sport_validity": round(sport_valid, 3),
        "semantic_league_validity": round(league_valid, 3),
        "semantic_format_validity": round(format_valid, 3),
        "semantic_clock_validity": round(clock_valid, 3),
        "semantic_market_validity": round(market_valid, 3),
        "semantic_freshness": round(freshness, 3),
        "semantic_anomaly_rate": round(anomaly_rate, 4),
        "semantic_anomaly_families": anomaly_families,
        "semantic_rejected": rejected,
        "semantic_team_cleanups": cleaned_teams,
        "semantic_bad_context": bad_context,
    }
