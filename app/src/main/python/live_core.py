import re
from copy import deepcopy


def norm_line(value):
    return re.sub(r"\s+", " ", str(value or "").replace("\u00a0", " ")).strip()


def float_or_none(value):
    if value is None:
        return None
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def pair_key(player1, player2):
    return (norm_line(player1).casefold(), norm_line(player2).casefold())


def score_pair(rec):
    a = rec.get("score1")
    b = rec.get("score2")
    if a is None or b is None:
        return None
    try:
        return int(a), int(b)
    except (TypeError, ValueError):
        return None


def clear_reset_detected(old, new):
    """Conservative reset detector. A score decrease alone is never enough."""
    oh = old.get("half")
    nh = new.get("half")
    om = old.get("minute")
    nm = new.get("minute")

    if oh is not None and nh is not None and nh < oh:
        return True
    if oh == 2 and nh == 1:
        return True

    # Same-half clock moved substantially backwards and the new score is low.
    if (
        oh is not None and nh is not None and oh == nh
        and om is not None and nm is not None and nm + 3 < om
    ):
        p = score_pair(new)
        if p and sum(p) <= 3:
            return True

    return False


def parse_metadata_records(raw_text):
    """
    Parse league/status/team pair/O-U metadata from rendered page text.

    Deliberately DOES NOT parse score from loose numeric lines. On Fortuna the
    score is visually aligned with each team row, while other numeric values
    (minutes, odds, market values) can sit nearby in DOM order. Guessing a score
    from the first two numbers after team2 caused false values such as 10:12.
    """
    raw_lines = [norm_line(x) for x in (raw_text or "").splitlines()]
    raw_lines = [x for x in raw_lines if x]

    noise_exact = {
        "zaloguj", "zaloguj się", "rejestracja", "start", "live", "sport",
        "kupony", "promocje", "menu", "płatności", "statystyki", "wyniki",
        "archiwum promocji", "mobile", "produkty", "stacjonarne punkty",
        "motyw", "ciemny", "zakłady bukmacherskie", "zakłady live",
        "wirtualne sporty", "bet on games", "solo", "kupon jest pusty",
        "pokaż więcej kursów",
    }

    def is_noise(line):
        low = line.casefold()
        if low in noise_exact:
            return True
        if "zakaz uczestnictwa w grach hazardowych" in low:
            return True
        if "sprawdź naszą ofertę i dodaj zdarzenie" in low:
            return True
        return False

    lines = [x for x in raw_lines if not is_noise(x)]

    league_re = re.compile(
        r"(esports?\s*battle|esportsbattle|e-?soccer|esoccer|efootball|volta|"
        r"gt\s*league|gt\s*-|h2h\s*-|gg\s*league|battle\s*-|"
        r"cyber\s+live\s+arena|eal\s*-|interactive\s+world\s+cup)",
        re.I,
    )
    format_re = re.compile(
        r"(?:\((?P<paren>[234]\s*[x×]\s*\d+)\s*min\.?\)|"
        r"mecze\s+rozgrywane\s+w\s+formacie\s*(?P<plain>[234]\s*[x×]\s*\d+)\s*min(?:ut(?:y|\.)?)?)",
        re.I,
    )
    status_re = re.compile(
        r"^(?:(?P<half>[12])\.\s*poł(?:\.|owa)?"
        r"(?:(?:\s*-\s*(?P<min>\d+)\s*m)|"
        r"(?:\s*(?P<smin>\d+)(?:\+(?P<added>\d+))?\s*['’′])|"
        r"\s*<\s*1\s*m)?|"
        r"przerwa(?:\s*(?P<hmin>\d+)(?:\+(?P<hadded>\d+))?\s*['’′])?)$",
        re.I,
    )
    team_re = re.compile(r"^(?P<team>.+?)\s*\((?P<player>[^()]{1,60})\)\s*$")
    market_re = re.compile(
        r"^(?P<side>mniej|więcej|wiecej|powyżej|powyzej|poniżej|ponizej|over|under)\s+(?P<line>\d+(?:[.,]\d+)?)$",
        re.I,
    )
    odds_re = re.compile(r"^\d{1,3}(?:[.,]\d{1,3})$")

    def is_league(line):
        low = norm_line(line).casefold()
        if low in {"e-piłka nożna", "e piłka nożna", "efootball", "e-football"}:
            return False
        return bool(league_re.search(line))

    def find_next_team(start_idx, max_ahead=6):
        for k in range(start_idx, min(len(lines), start_idx + max_ahead + 1)):
            m = team_re.match(lines[k])
            if m:
                return k, m
            if is_league(lines[k]) or status_re.match(lines[k]):
                if k != start_idx:
                    break
        return None, None

    current_league = ""
    current_format = ""
    current_status = ""
    matches = []
    i = 0

    while i < len(lines):
        line = lines[i]

        if is_league(line):
            current_league = line
            fm = format_re.search(line)
            token = (fm.group("paren") or fm.group("plain")) if fm else ""
            current_format = re.sub(r"\s+", "", token).replace("×", "x") if token else ""
            current_status = ""
            i += 1
            continue

        fm = format_re.search(line)
        if fm and current_league:
            token = fm.group("paren") or fm.group("plain") or ""
            current_format = re.sub(r"\s+", "", token).replace("×", "x")
            i += 1
            continue

        sm = status_re.match(line)
        if sm:
            current_status = line
            i += 1
            continue

        tm1 = team_re.match(line)
        if not tm1:
            i += 1
            continue

        j, tm2 = find_next_team(i + 1, 6)
        if tm2 is None:
            i += 1
            continue

        if not current_league or not is_league(current_league):
            i = j + 1
            continue

        team1 = norm_line(tm1.group("team"))
        player1 = norm_line(tm1.group("player"))
        team2 = norm_line(tm2.group("team"))
        player2 = norm_line(tm2.group("player"))

        under_line = over_line = None
        under_odds = over_odds = None

        # Search only the MAIN match total market. Current Superbet places
        # "Liczba goli drużyny" and "Asian Total Goals" immediately below it;
        # those must never overwrite the full-match O/U line.
        k = j + 1
        market_end = min(len(lines), j + 40)
        main_total_seen = False
        stop_market_re = re.compile(
            r"^(?:liczba\s+goli\s+drużyny|asian\s+total\s+goals|parzysta\s*/?\s*nieparzysta|"
            r"podwójna\s+szansa|dokładny\s+wynik|handicap|następny\s+gol|\d+\.\s*gol\b)", re.I
        )

        def absorb_market_segment(segment, forced_side=None):
            nonlocal under_line, over_line, under_odds, over_odds
            low = segment.casefold()
            side = forced_side
            if side is None:
                if re.search(r"\b(mniej|poniżej|ponizej|under)\b", low):
                    side = "under"
                elif re.search(r"\b(więcej|wiecej|powyżej|powyzej|over)\b", low):
                    side = "over"
            nums = [float_or_none(x) for x in re.findall(r"\d+(?:[.,]\d+)?", segment)]
            nums = [x for x in nums if x is not None]
            if not side or not nums:
                return False
            line_val = nums[0]
            odds_val = None
            if len(nums) >= 3:
                odds_val = nums[-1]
            elif len(nums) == 2 and abs(nums[1] - nums[0]) > 1e-9:
                odds_val = nums[1]
            if odds_val is not None and not (1.001 <= odds_val <= 100):
                odds_val = None
            if side in {"mniej", "poniżej", "ponizej", "under"}:
                under_line, under_odds = line_val, odds_val
            else:
                over_line, over_odds = line_val, odds_val
            return True

        while k < market_end:
            candidate = lines[k]
            low_candidate = candidate.casefold()
            if k > j + 1:
                if is_league(candidate):
                    break
                if status_re.match(candidate) and candidate != current_status:
                    break
                if team_re.match(candidate) and k > j + 2:
                    break

            if re.search(r"\bliczba\s+goli\b", low_candidate) and not re.search(r"liczba\s+goli\s+drużyny", low_candidate):
                main_total_seen = True
            if stop_market_re.search(candidate):
                if main_total_seen or re.search(r"asian\s+total\s+goals|liczba\s+goli\s+drużyny", low_candidate):
                    break

            mm = market_re.match(candidate)
            # Legacy/simple layouts may omit the heading. A bare O/U directly below
            # the teams is accepted, but team totals ("strzeli") and Asian totals are not.
            if not main_total_seen and (mm or re.search(r"\b(mniej|poniżej|ponizej|under|więcej|wiecej|powyżej|powyzej|over)\b", low_candidate)):
                if "strzeli" not in low_candidate and "asian" not in low_candidate:
                    main_total_seen = True
            if not main_total_seen:
                k += 1
                continue

            # A compact LIVE card can contain both sides in one rendered line.
            side_hits = list(re.finditer(r"\b(poniżej|ponizej|mniej|under|powyżej|powyzej|więcej|wiecej|over)\b", candidate, re.I))
            if len(side_hits) >= 2:
                for idx, hit in enumerate(side_hits):
                    seg = candidate[hit.start(): side_hits[idx + 1].start() if idx + 1 < len(side_hits) else len(candidate)]
                    side_name = "over" if re.search(r"powyżej|powyzej|więcej|wiecej|over", hit.group(1), re.I) else "under"
                    absorb_market_segment(seg, side_name)
            else:
                side = None
                line_val = odds_val = None
                if mm:
                    side = mm.group("side").casefold()
                    line_val = float_or_none(mm.group("line"))
                else:
                    low = candidate.casefold()
                    if re.search(r"\b(mniej|poniżej|ponizej|under)\b", low):
                        side = "under"
                    elif re.search(r"\b(więcej|wiecej|powyżej|powyzej|over)\b", low):
                        side = "over"
                    nums = [float_or_none(x) for x in re.findall(r"\d+(?:[.,]\d+)?", candidate)]
                    nums = [x for x in nums if x is not None]
                    if side and nums:
                        line_val = nums[0]
                        if len(nums) >= 3:
                            odds_val = nums[-1]
                        elif len(nums) == 2 and abs(nums[1] - nums[0]) > 1e-9:
                            odds_val = nums[1]
                if side and odds_val is None:
                    for q in range(k + 1, min(k + 5, market_end)):
                        y = lines[q]
                        if odds_re.match(y):
                            odds_val = float_or_none(y)
                            break
                        if market_re.match(y) or team_re.match(y) or is_league(y) or stop_market_re.search(y):
                            break
                if side:
                    if odds_val is not None and not (1.001 <= odds_val <= 100):
                        odds_val = None
                    if side in {"mniej", "poniżej", "ponizej", "under"}:
                        under_line, under_odds = line_val, odds_val
                    else:
                        over_line, over_odds = line_val, odds_val

            if under_line is not None and over_line is not None and under_odds is not None and over_odds is not None:
                break
            k += 1

        market_line = under_line if under_line is not None else over_line
        if under_line is not None and over_line is not None:
            if abs(under_line - over_line) > 1e-9:
                market_line = under_line

        fmt = current_format
        if not fmt:
            fm = format_re.search(current_league)
            if fm:
                token = fm.group("paren") or fm.group("plain") or ""
                fmt = re.sub(r"\s+", "", token).replace("×", "x")

        half = minute = None
        status_match = status_re.match(current_status) if current_status else None
        if status_match:
            if status_match.group("half"):
                half = int(status_match.group("half"))
                if status_match.group("min"):
                    minute = int(status_match.group("min"))
                elif status_match.group("smin"):
                    minute = int(status_match.group("smin")) + int(status_match.group("added") or 0)
                elif "<" in current_status:
                    minute = 0
            else:
                half = 1
                if status_match.group("hmin"):
                    minute = int(status_match.group("hmin")) + int(status_match.group("hadded") or 0)

        key = f"{current_league.casefold()}|{player1.casefold()}|{player2.casefold()}"
        record = {
            "key": key,
            "league": current_league,
            "format": fmt,
            "status": current_status,
            "half": half,
            "minute": minute,
            "team1": team1,
            "player1": player1,
            "team2": team2,
            "player2": player2,
            "score1": None,
            "score2": None,
            "total_goals": None,
            "line": market_line,
            "under_odds": under_odds,
            "over_odds": over_odds,
            "_source": "metadata_text",
        }

        existing = next((x for x in matches if x["key"] == key), None)
        if existing is None:
            matches.append(record)
        else:
            def quality(r):
                return sum(
                    r.get(field) is not None and r.get(field) != ""
                    for field in ("status", "line", "under_odds", "over_odds")
                )
            if quality(record) >= quality(existing):
                matches[matches.index(existing)] = record

        i = j + 1

    return matches


def _layout_quality(rec):
    q = rec.get("_score_confidence")
    try:
        q = float(q)
    except (TypeError, ValueError):
        q = 0.0
    score_ok = score_pair(rec) is not None
    return (1 if score_ok else 0, q, 1 if rec.get("status") else 0)


def merge_layout_scores(metadata_records, layout_records):
    """Merge geometry-derived scores into text-derived metadata records."""
    metadata_records = [dict(r) for r in (metadata_records or [])]
    layout_records = [dict(r) for r in (layout_records or [])]

    layout_by_pair = {}
    for r in layout_records:
        p1 = r.get("player1")
        p2 = r.get("player2")
        if not p1 or not p2:
            continue
        k = pair_key(p1, p2)
        old = layout_by_pair.get(k)
        if old is None or _layout_quality(r) > _layout_quality(old):
            layout_by_pair[k] = r

    used_layout = set()
    out = []

    for rec in metadata_records:
        k = pair_key(rec.get("player1"), rec.get("player2"))
        lay = layout_by_pair.get(k)
        reverse = False
        if lay is None:
            rk = (k[1], k[0])
            lay = layout_by_pair.get(rk)
            reverse = lay is not None

        if lay is not None:
            used_layout.add(id(lay))
            p = score_pair(lay)
            if p is not None:
                a, b = p
                if reverse:
                    a, b = b, a
                rec["score1"] = a
                rec["score2"] = b
                rec["total_goals"] = a + b
                rec["_score_source"] = "layout_geometry"
                rec["_score_confidence"] = lay.get("_score_confidence", 0.0)
                rec["_layout_debug"] = lay.get("_layout_debug", "")
                rec["_source"] = "metadata+layout"

            # Preserve adapter identity and clock semantics through the merge.
            # These fields decide whether source-specific clock/market data is safe
            # to admit into the analytical SQLite layer.
            for internal_field in (
                "_layout_adapter", "_clock_mode", "_score_column_x",
                "_score_binding", "_score_binding_x", "_score_binding_suspect", "_score_candidate1",
                "_score_candidate2", "_cross_source_score_confirmed", "_active_card_bound",
                "_market_binding_suspect"
            ):
                if lay.get(internal_field) not in (None, ""):
                    rec[internal_field] = lay.get(internal_field)

            # Layout status is useful when text ordering missed it.
            if not rec.get("status") and lay.get("status"):
                rec["status"] = lay.get("status")
                rec["half"] = lay.get("half")
                rec["minute"] = lay.get("minute")
            if not rec.get("league") and lay.get("league"):
                rec["league"] = lay.get("league")
            # Adaptery innych serwisów (np. Betcris) potrafią dostarczyć
            # również metadane rynku bezpośrednio z geometrii DOM.
            # Fortuna pozostaje bez zmian, bo jej layout rekordy tych pól nie mają.
            for field in ("format", "clock_second_in_period", "match_second", "elapsed_seconds", "line", "under_odds", "over_odds"):
                if rec.get(field) in (None, "") and lay.get(field) not in (None, ""):
                    rec[field] = lay.get(field)

        out.append(rec)

    # Keep layout-only pairs rather than returning zero matches if metadata text
    # changes. Score still goes through the same temporal confirmation state.
    known_pairs = {pair_key(r.get("player1"), r.get("player2")) for r in out}
    for lay in layout_records:
        k = pair_key(lay.get("player1"), lay.get("player2"))
        if not all(k) or k in known_pairs:
            continue
        p = score_pair(lay)
        if p is None:
            if lay.get("_score_candidate1") is None or lay.get("_score_candidate2") is None:
                continue
            a = b = None
        else:
            a, b = p
        league = norm_line(lay.get("league")) or "eSoccer — liga nierozpoznana"
        key = f"{league.casefold()}|{k[0]}|{k[1]}"
        out.append({
            "key": key,
            "league": league,
            "format": lay.get("format", ""),
            "status": lay.get("status", ""),
            "half": lay.get("half"),
            "minute": lay.get("minute"),
            "clock_second_in_period": lay.get("clock_second_in_period"),
            "match_second": lay.get("match_second"),
            "elapsed_seconds": lay.get("elapsed_seconds"),
            "team1": norm_line(lay.get("team1")),
            "player1": norm_line(lay.get("player1")),
            "team2": norm_line(lay.get("team2")),
            "player2": norm_line(lay.get("player2")),
            "score1": a,
            "score2": b,
            "total_goals": (a + b) if a is not None and b is not None else None,
            "line": lay.get("line"),
            "under_odds": lay.get("under_odds"),
            "over_odds": lay.get("over_odds"),
            "_score_source": "layout_geometry",
            "_score_confidence": lay.get("_score_confidence", 0.0),
            "_layout_debug": lay.get("_layout_debug", ""),
            "_layout_adapter": lay.get("_layout_adapter", ""),
            "_clock_mode": lay.get("_clock_mode", ""),
            "_score_column_x": lay.get("_score_column_x"),
            "_score_binding": lay.get("_score_binding", ""),
            "_score_binding_x": lay.get("_score_binding_x"),
            "_score_binding_suspect": lay.get("_score_binding_suspect", False),
            "_score_candidate1": lay.get("_score_candidate1"),
            "_score_candidate2": lay.get("_score_candidate2"),
            "_cross_source_score_confirmed": lay.get("_cross_source_score_confirmed", False),
            "_active_card_bound": lay.get("_active_card_bound", False),
            "_market_binding_suspect": lay.get("_market_binding_suspect", False),
            "_source": "layout_only",
        })

    # One record per key; prefer richer records.
    deduped = {}
    for rec in out:
        key = rec.get("key")
        if not key:
            continue
        def q(r):
            return sum(
                r.get(f) not in (None, "")
                for f in (
                    "league", "status", "score1", "score2", "line",
                    "under_odds", "over_odds",
                )
            ) + (2 if r.get("_score_source") == "layout_geometry" else 0)
        if key not in deduped or q(rec) > q(deduped[key]):
            deduped[key] = rec

    return list(deduped.values())


class CanonicalLiveState:
    """
    Shared, conservative source of truth for both the change logger and goal logger.

    Scores are accepted only from geometry-derived team rows and require temporal
    confirmation. A direct +1 change requires two observations. Any jump larger than +1 is
    a catch-up gap and requires three observations; it is never treated as one goal. A missing score never erases an already accepted score.
    """

    def __init__(
        self,
        min_score_confidence=0.82,
        confirmations=2,
        catchup_confirmations=3,
        min_confirmation_gap=0.35,
    ):
        self.min_score_confidence = float(min_score_confidence)
        self.confirmations = int(confirmations)
        self.catchup_confirmations = int(catchup_confirmations)
        self.min_confirmation_gap = float(min_confirmation_gap)
        self.records = {}
        self.pending_scores = {}

    def reset(self):
        self.records.clear()
        self.pending_scores.clear()

    def _observe_pending(self, key, pair, required, now, window_start=None):
        p = self.pending_scores.get(key)
        if p is None or tuple(p.get("pair", ())) != tuple(pair):
            p = {
                "pair": tuple(pair),
                "count": 1,
                "first": float(now),
                "last": float(now),
                "window_start": float(now if window_start is None else window_start),
                "required": int(required),
            }
            self.pending_scores[key] = p
            return int(required) <= 1, deepcopy(p)

        # Do not count two consumers reading the same DOM sample milliseconds apart.
        if float(now) - float(p.get("last", 0.0)) >= self.min_confirmation_gap:
            p["count"] = int(p.get("count", 1)) + 1
            p["last"] = float(now)
        p["required"] = max(int(p.get("required", required)), int(required))
        confirmed = int(p["count"]) >= int(p["required"])
        return confirmed, deepcopy(p)

    def _hold_old_score(self, stable, old):
        if old is not None and score_pair(old) is not None:
            stable["score1"] = old.get("score1")
            stable["score2"] = old.get("score2")
            stable["total_goals"] = old.get("total_goals")
            # Zachowaj pochodzenie zaakceptowanego wyniku; kandydat odrzucony
            # nie może przepisać źródła/confidence starego pewnego score.
            stable["_score_source"] = old.get("_score_source", "")
            stable["_score_confidence"] = old.get("_score_confidence", "")
            stable["_score_column_x"] = old.get("_score_column_x")
            stable["_score_binding"] = old.get("_score_binding")
            if old.get("_layout_debug"):
                stable["_layout_debug"] = old.get("_layout_debug")
        else:
            stable["score1"] = None
            stable["score2"] = None
            stable["total_goals"] = None

    def reconcile(self, raw_records, now):
        current = []
        anomalies = []

        for raw in raw_records or []:
            key = raw.get("key")
            if not key:
                continue

            candidate = dict(raw)
            old = self.records.get(key)
            stable = dict(candidate)
            previous_sample_mono = (old or {}).get("_last_sample_mono")

            # Carry forward metadata that can temporarily disappear during rerenders.
            if old is not None:
                for field in (
                    "league", "format", "status", "half", "minute", "clock_second_in_period", "match_second", "elapsed_seconds",
                    "team1", "player1", "team2", "player2",
                    "line", "under_odds", "over_odds",
                ):
                    if stable.get(field) in (None, "") and old.get(field) not in (None, ""):
                        stable[field] = old.get(field)

                # Same-half displayed minute may not move backwards. Keep old clock on
                # a one-frame DOM mismatch instead of creating a false minute boundary.
                oh, nh = old.get("half"), candidate.get("half")
                om, nm = old.get("minute"), candidate.get("minute")
                if (
                    oh is not None and nh is not None and oh == nh
                    and om is not None and nm is not None and nm < om
                    and not clear_reset_detected(old, candidate)
                ):
                    anomalies.append({
                        "reason": f"cofnięcie minuty {om} → {nm} bez resetu",
                        "old": old,
                        "candidate": candidate,
                    })
                    stable["status"] = old.get("status")
                    stable["half"] = old.get("half")
                    stable["minute"] = old.get("minute")

            cand_pair = score_pair(candidate)
            old_pair = score_pair(old or {})
            confidence = candidate.get("_score_confidence", 0.0)
            try:
                confidence = float(confidence)
            except (TypeError, ValueError):
                confidence = 0.0

            score_source = str(candidate.get("_score_source") or "")
            # Geometry is preferred. The strict Betcris single-match text fallback is
            # also allowed, but still needs the same two consecutive confirmations.
            source_ok = score_source in {"layout_geometry", "text_match_fallback", "cross_source_confirmed_geometry"}
            confidence_ok = confidence >= self.min_score_confidence

            stable["_score_transition"] = "none"
            stable["_score_confirmations"] = 0
            stable["_score_event_low_mono"] = None
            stable["_score_event_high_mono"] = None
            stable["_score_confirmed_mono"] = None
            stable["_analysis_allowed"] = True

            # Superbet score binding: the accepted score column for one player pair
            # must not jump to a neighboring score column during a DOM rerender.
            # A shifted binding is retained only as raw evidence, never as analysis.
            cross_source_confirmed = bool(candidate.get("_cross_source_score_confirmed"))
            binding_shift = bool(candidate.get("_score_binding_suspect"))
            if old is not None and str(candidate.get("_layout_adapter") or "").startswith("superbet"):
                try:
                    old_x = float(old.get("_score_binding_x") if old.get("_score_binding_x") is not None else old.get("_score_column_x"))
                    new_x = float(candidate.get("_score_binding_x") if candidate.get("_score_binding_x") is not None else candidate.get("_score_column_x"))
                    binding_shift = binding_shift or abs(new_x - old_x) > 80.0
                except (TypeError, ValueError):
                    pass

            if binding_shift and cand_pair is not None and not cross_source_confirmed:
                self._hold_old_score(stable, old)
                stable["_score_quality"] = "ODRZUCONY — SCORE BINDING"
                stable["_analysis_allowed"] = False
                self.pending_scores.pop(key, None)
                anomalies.append({
                    "reason": (
                        f"score binding shift {old.get('_score_binding_x', old.get('_score_column_x'))} → "
                        f"{candidate.get('_score_binding_x', candidate.get('_score_column_x'))} dla tej samej pary"
                    ),
                    "old": old,
                    "candidate": candidate,
                })
            elif cand_pair is None:
                self._hold_old_score(stable, old)
                stable["_score_quality"] = "HOLD / BRAK ODCZYTU"
                stable["_analysis_allowed"] = False
            elif not source_ok or not confidence_ok:
                self._hold_old_score(stable, old)
                stable["_score_quality"] = "ODRZUCONY — NISKA PEWNOŚĆ"
                stable["_analysis_allowed"] = False
                anomalies.append({
                    "reason": (
                        f"wynik {cand_pair[0]}:{cand_pair[1]} bez pewnego "
                        f"powiązania z wierszami drużyn (source={score_source or 'brak'}, confidence={confidence:.2f})"
                    ),
                    "old": old,
                    "candidate": candidate,
                })
            elif old_pair is not None and cand_pair == old_pair:
                self.pending_scores.pop(key, None)
                stable["score1"], stable["score2"] = cand_pair
                stable["total_goals"] = sum(cand_pair)
                stable["_score_quality"] = "POTWIERDZONY"
            else:
                reset = old is not None and clear_reset_detected(old, candidate)
                if old_pair is not None:
                    d1 = cand_pair[0] - old_pair[0]
                    d2 = cand_pair[1] - old_pair[1]
                else:
                    d1 = d2 = None

                if old_pair is not None and not reset and (d1 < 0 or d2 < 0):
                    self._hold_old_score(stable, old)
                    stable["_score_quality"] = "ODRZUCONY — SPADEK"
                    stable["_analysis_allowed"] = False
                    self.pending_scores.pop(key, None)
                    anomalies.append({
                        "reason": (
                            f"spadek wyniku {old_pair[0]}:{old_pair[1]} → "
                            f"{cand_pair[0]}:{cand_pair[1]} bez resetu"
                        ),
                        "old": old,
                        "candidate": candidate,
                    })
                else:
                    if old_pair is None:
                        transition = "baseline"
                        required = self.confirmations
                    elif reset:
                        transition = "reset"
                        required = self.confirmations
                    else:
                        delta = (d1 or 0) + (d2 or 0)
                        transition = "goal" if delta == 1 else "catchup"
                        required = self.confirmations if delta == 1 else self.catchup_confirmations

                    if cross_source_confirmed:
                        required = 1
                    confirmed, pending = self._observe_pending(
                        key, cand_pair, required, now,
                        window_start=previous_sample_mono,
                    )
                    stable["_score_confirmations"] = pending.get("count", 1)

                    if confirmed:
                        self.pending_scores.pop(key, None)
                        stable["score1"], stable["score2"] = cand_pair
                        stable["total_goals"] = sum(cand_pair)
                        stable["_score_transition"] = transition
                        stable["_score_event_low_mono"] = pending.get("window_start")
                        stable["_score_event_high_mono"] = pending.get("first")
                        stable["_score_confirmed_mono"] = float(now)
                        if cross_source_confirmed:
                            stable["_score_quality"] = "POTWIERDZONY CROSS-SOURCE"
                        else:
                            stable["_score_quality"] = (
                                "POTWIERDZONY"
                                if transition in ("baseline", "goal")
                                else "POTWIERDZONY CATCH-UP"
                            )
                    else:
                        self._hold_old_score(stable, old)
                        stable["_score_transition"] = "pending"
                        stable["_analysis_allowed"] = False
                        stable["_score_quality"] = (
                            f"OCZEKUJE {pending.get('count',1)}/{pending.get('required',required)}"
                        )

            # Accepted state is the single source of truth for both consumers.
            stable["_last_sample_mono"] = float(now)
            self.records[key] = dict(stable)
            current.append(dict(stable))

        return current, anomalies
