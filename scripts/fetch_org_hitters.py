"""Org-wide Yankees hitters tracker. Reads cached MLB + MiLB stats files,
fetches last-10-day stats per hitter (byDateRange), pulls org transactions,
derives level history. Writes data/org-hitters.json and data/org-transactions.json.
"""
from __future__ import annotations

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib_scrape_log import log_scrape

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA = REPO_ROOT / "data"
HITTERS_PATH = DATA / "org-hitters.json"
TX_PATH = DATA / "org-transactions.json"
LU_PATH = DATA / "last-updated.json"
MLB_CACHE = DATA / "stats-mlb.json"
MILB_CACHE = DATA / "stats-milb.json"

BASE = "https://statsapi.mlb.com/api/v1"
PARENT_TEAM_ID = 147
USER_AGENT = "YankNewsBot/1.0 (+https://github.com/cwds145/Yank-News)"
TIMEOUT = 25
WORKERS = 8
SUBMIT_DELAY_S = 0.1
WINDOW_DAYS = 10
TX_WINDOW_DAYS = 30

PITCHER_POS = {"P", "SP", "RP", "CP", "LHP", "RHP"}
TWP = "TWP"
LEVEL_RANK = {"MLB": 0, "AAA": 1, "AA": 2, "High-A": 3, "Low-A": 4}
# Every level is queried for every org player so stats follow them across
# promotions/demotions; byDateRange/season at sportIds where they never played
# returns empty splits, which aggregate cleanly to nothing.
ALL_SPORT_LEVELS = [(1, "MLB"), (11, "AAA"), (12, "AA"), (13, "High-A"), (14, "Low-A")]

# Whitelist of MLB transaction type codes that represent actual organizational
# level moves. Everything else (notably "NUM" / Number Change) is dropped from
# level-history derivation.
LEVEL_MOVE_TYPES = {
    "CU",   # Recalled
    "OPT",  # Optioned
    "ASG",  # Assigned (broad — see spring-camp carve-out below)
    "TR",   # Traded
    "CLW",  # Claimed off Waivers
    "DFA",  # Designated for Assignment
    "REL",  # Released
    "RTN",  # Returned
    "SFA",  # Signed as Free Agent
    "SC",   # Status Change
}
# Excluded from user-facing data/org-transactions.json (file is source of truth;
# UI just renders what's in the file).
NOISE_TX_TYPES = {"NUM"}
# ASG spring-camp carve-out: ASG entries with toTeam=MLB between Feb 1 and the
# regular-season start are spring-training assignments, not real callups.
DEFAULT_SEASON_START_MMDD = "03-27"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_season() -> int:
    o = os.environ.get("SEASON_OVERRIDE")
    if o:
        try:
            return int(o)
        except ValueError:
            pass
    return datetime.now(timezone.utc).year


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


def fetch_json(session: requests.Session, url: str) -> tuple[int, dict | None]:
    try:
        r = session.get(url, timeout=TIMEOUT)
        if r.status_code == 200:
            try:
                return r.status_code, r.json()
            except ValueError:
                return r.status_code, None
        return r.status_code, None
    except requests.RequestException:
        return 0, None


def load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def update_last_updated(stamp: str) -> None:
    lu = {"news": None, "stats_safe": None, "stats_scraped": None}
    if LU_PATH.exists():
        try:
            with open(LU_PATH, "r", encoding="utf-8") as f:
                ex = json.load(f)
            if isinstance(ex, dict):
                for k in ("news", "stats_safe", "stats_scraped"):
                    if k in ex:
                        lu[k] = ex[k]
        except (json.JSONDecodeError, OSError):
            pass
    lu["stats_safe"] = stamp
    atomic_write(LU_PATH, lu)


def to_num(v: Any) -> float | None:
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def to_int(v: Any) -> int | None:
    if v in (None, ""):
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        try:
            return int(float(v))
        except (ValueError, TypeError):
            return None


def fmt_rate(x: float | None) -> str | None:
    if x is None:
        return None
    s = f"{x:.3f}"
    if 0 <= x < 1 and s.startswith("0."):
        s = s[1:]
    return s


def aggregate_blocks(blocks: list[dict | None]) -> dict | None:
    """Sum raw counting stats across per-level blocks; recompute AVG/OBP/SLG/OPS
    from totals (exact, no weighting). Each block must have keys: ab, h, bb, so,
    hbp, sf, tb. Returns the frontend-compatible block shape (ab, k, avg, ops,
    avg_num, ops_num) or None if no input has any AB.
    """
    valid = [b for b in blocks if b]
    if not valid:
        return None
    ab = sum(to_int(b.get("ab")) or 0 for b in valid)
    h = sum(to_int(b.get("h")) or 0 for b in valid)
    bb = sum(to_int(b.get("bb")) or 0 for b in valid)
    so = sum(to_int(b.get("so")) or 0 for b in valid)
    hbp = sum(to_int(b.get("hbp")) or 0 for b in valid)
    sf = sum(to_int(b.get("sf")) or 0 for b in valid)
    tb = sum(to_int(b.get("tb")) or 0 for b in valid)
    if ab == 0 and bb == 0:
        return None
    avg = (h / ab) if ab > 0 else None
    obp_den = ab + bb + hbp + sf
    obp = ((h + bb + hbp) / obp_den) if obp_den > 0 else None
    slg = (tb / ab) if ab > 0 else None
    ops = (obp + slg) if (obp is not None and slg is not None) else None
    return {
        "ab": ab,
        "k": so,
        "avg": fmt_rate(avg),
        "ops": fmt_rate(ops),
        "avg_num": avg,
        "ops_num": ops,
    }


def is_hitter(position: str | None, group: str | None) -> bool:
    pos = (position or "").upper()
    if pos == TWP:
        return group == "hitting"
    return pos not in PITCHER_POS


def build_org_players(mlb: dict, milb: dict) -> tuple[list[int], dict[int, str], dict[int, str], dict[int, str]]:
    """Walk cached MLB + MiLB rosters. Returns:
      - player_ids: ordered list of unique hitter ids
      - primary_level: {pid: highest level by LEVEL_RANK}
      - primary_position: {pid: position string from primary level's roster}
      - name: {pid: full name}
    Stats themselves are fetched per-player-per-level later; this just enumerates
    who's in the org and which level/position to display.
    """
    player_ids: list[int] = []
    primary_level: dict[int, str] = {}
    primary_position: dict[int, str] = {}
    name: dict[int, str] = {}

    mlb_player_stats = (mlb or {}).get("player_stats") or {}
    for r in (mlb or {}).get("roster", []):
        pid = r.get("id")
        if pid is None:
            continue
        ps = mlb_player_stats.get(str(pid)) or {}
        if not is_hitter(r.get("position"), ps.get("group")):
            continue
        if pid not in primary_level:
            player_ids.append(pid)
            primary_level[pid] = "MLB"
            primary_position[pid] = r.get("position") or ""
            name[pid] = r.get("name") or ""

    affiliates = sorted(
        (milb or {}).get("affiliates") or [],
        key=lambda a: LEVEL_RANK.get(a.get("level"), 99),
    )
    for aff in affiliates:
        level = aff.get("level")
        ps_dict = aff.get("player_stats") or {}
        for r in aff.get("roster", []) or []:
            pid = r.get("id")
            if pid is None:
                continue
            ps = ps_dict.get(str(pid)) or {}
            if not is_hitter(r.get("position"), ps.get("group")):
                continue
            if pid not in primary_level:
                player_ids.append(pid)
                primary_level[pid] = level
                primary_position[pid] = r.get("position") or ""
                name[pid] = r.get("name") or ""

    return player_ids, primary_level, primary_position, name


def _parse_split_block(stat: dict) -> dict:
    return {
        "ab": to_int(stat.get("atBats")),
        "h": to_int(stat.get("hits")),
        "bb": to_int(stat.get("baseOnBalls")),
        "so": to_int(stat.get("strikeOuts")),
        "hbp": to_int(stat.get("hitByPitch")),
        "sf": to_int(stat.get("sacFlies")),
        "tb": to_int(stat.get("totalBases")),
        "avg": stat.get("avg"),
        "obp": stat.get("obp"),
        "slg": stat.get("slg"),
        "ops": stat.get("ops"),
    }


def fetch_player_stats(session: requests.Session, player_ids: list[int],
                         season: int, start: str, end: str
                         ) -> dict[tuple[int, int], dict[str, dict | None]]:
    """For every player × every org level (sport_id), pull season + byDateRange
    in one API call. Returns {(pid, sport_id): {"season": block_or_None,
    "last_10": block_or_None}}. Empty splits → block None (player didn't play
    at that level). This is what lets a recently-promoted hitter keep his
    Somerset/SWB stats once he's on the MLB roster.
    """
    targets: list[tuple[int, int, str]] = [
        (pid, sid, lvl) for pid in player_ids for sid, lvl in ALL_SPORT_LEVELS
    ]
    out: dict[tuple[int, int], dict[str, dict | None]] = {}
    by_level_count: dict[str, int] = {}
    by_level_ok: dict[str, int] = {}
    for _, _, lvl in targets:
        by_level_count[lvl] = by_level_count.get(lvl, 0) + 1
        by_level_ok.setdefault(lvl, 0)

    def task(pid: int, sport_id: int, lvl: str
             ) -> tuple[int, int, str, int, dict[str, dict | None] | None]:
        url = (
            f"{BASE}/people/{pid}/stats?stats=season,byDateRange&group=hitting"
            f"&startDate={start}&endDate={end}&season={season}&sportId={sport_id}"
        )
        status, data = fetch_json(session, url)
        if status != 200 or not data:
            return pid, sport_id, lvl, status, None
        result: dict[str, dict | None] = {"season": None, "last_10": None}
        for stat_set in (data.get("stats") or []):
            type_name = ((stat_set.get("type") or {}).get("displayName") or "").lower()
            splits = stat_set.get("splits") or []
            if not splits:
                continue
            block = _parse_split_block(splits[0].get("stat") or {})
            if "datarange" in type_name.replace(" ", "") or "daterange" in type_name.replace(" ", ""):
                result["last_10"] = block
            elif type_name.startswith("season"):
                result["season"] = block
        return pid, sport_id, lvl, status, result

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = []
        for pid, sid, lvl in targets:
            futures.append(ex.submit(task, pid, sid, lvl))
            time.sleep(SUBMIT_DELAY_S)
        for fut in as_completed(futures):
            try:
                pid, sport_id, lvl, status, result = fut.result()
            except Exception:
                continue
            out[(pid, sport_id)] = result or {"season": None, "last_10": None}
            if status == 200:
                by_level_ok[lvl] = by_level_ok.get(lvl, 0) + 1

    for lvl, count in by_level_count.items():
        ok = by_level_ok.get(lvl, 0)
        log_scrape(
            f"MLB season+byDateRange: {lvl}",
            f"{BASE}/people/{{id}}/stats?stats=season,byDateRange&sportId={{sid}}",
            200 if ok else 0, ok,
            note=f"ok={ok}/{count} window={start}..{end}",
        )
    return out


def fetch_transactions(session: requests.Session, milb: dict) -> list[dict[str, Any]]:
    today = datetime.now(timezone.utc).date()
    start = (today - timedelta(days=TX_WINDOW_DAYS)).isoformat()
    end = today.isoformat()

    parent_url = f"{BASE}/transactions?teamId={PARENT_TEAM_ID}&startDate={start}&endDate={end}"
    status, data = fetch_json(session, parent_url)
    items = (data or {}).get("transactions") or []
    log_scrape("MLB Transactions org", parent_url, status, len(items),
               note=f"window {start}..{end}")

    distinct_to = {((t.get("toTeam") or {}).get("id")) for t in items}
    distinct_to.discard(None)

    if distinct_to.issubset({PARENT_TEAM_ID}):
        merged: dict[Any, dict] = {t.get("id"): t for t in items if t.get("id")}
        affiliate_ids = [
            a.get("id") for a in (milb.get("affiliates") or []) if a.get("id")
        ]
        for aid in affiliate_ids:
            url = f"{BASE}/transactions?teamId={aid}&startDate={start}&endDate={end}"
            s, d = fetch_json(session, url)
            aff_items = (d or {}).get("transactions") or []
            log_scrape(f"MLB Transactions team {aid}", url, s, len(aff_items),
                       note="affiliate fallback")
            for t in aff_items:
                tid = t.get("id")
                if tid:
                    merged[tid] = t
        items = list(merged.values())

    return items


def team_label(team_obj: dict, team_to_level: dict[int, str]) -> str | None:
    tid = team_obj.get("id")
    name = team_obj.get("name") or ""
    if not name:
        return None
    if tid == PARENT_TEAM_ID:
        return name
    level = team_to_level.get(tid)
    return f"{level} {name}" if level else name


def normalize_tx(t: dict, team_to_level: dict[int, str]) -> dict[str, Any]:
    person = t.get("person") or {}
    from_team = t.get("fromTeam") or {}
    to_team = t.get("toTeam") or {}
    raw_date = t.get("date") or ""
    date = raw_date[:10] if raw_date else ""
    return {
        "id": t.get("id"),
        "date": date,
        "type_code": t.get("typeCode"),
        "type_desc": t.get("typeDesc"),
        "player_id": person.get("id"),
        "player": person.get("fullName"),
        "from_team_id": from_team.get("id"),
        "to_team_id": to_team.get("id"),
        "from": team_label(from_team, team_to_level),
        "to": team_label(to_team, team_to_level),
        "description": t.get("description") or "",
    }


def derive_levels(player_id: int, current_level: str,
                   transactions: list[dict],
                   team_to_level: dict[int, str]) -> tuple[str | None, str | None, str | None]:
    """Walk player's transactions chronologically, return (current_since, prior_level, prior_since)."""
    history: list[tuple[str, str]] = []
    player_tx = []
    for t in transactions:
        if (t.get("person") or {}).get("id") != player_id:
            continue
        tid = (t.get("toTeam") or {}).get("id")
        level = team_to_level.get(tid)
        if not level:
            continue
        date = (t.get("date") or "")[:10]
        if not date:
            continue
        player_tx.append((date, level))
    player_tx.sort()

    for date, level in player_tx:
        if not history or history[-1][1] != level:
            history.append((date, level))

    if not history:
        return None, None, None

    anchor = None
    for i in range(len(history) - 1, -1, -1):
        if history[i][1] == current_level:
            anchor = i
            break
    if anchor is None:
        anchor = len(history) - 1

    current_since = history[anchor][0]
    if anchor > 0:
        prior_since, prior_level = history[anchor - 1][0], history[anchor - 1][1]
    else:
        prior_level, prior_since = None, None

    return current_since, prior_level, prior_since


def fallback_personid(session: requests.Session, player_id: int,
                       season: int) -> list[dict]:
    today = datetime.now(timezone.utc).date()
    url = (f"{BASE}/transactions?personId={player_id}"
           f"&startDate={season}-01-01&endDate={today.isoformat()}")
    _, data = fetch_json(session, url)
    return (data or {}).get("transactions") or []


def fetch_season_start(session: requests.Session, season: int) -> str:
    """Return the regular-season start date as 'YYYY-MM-DD' (March 27 fallback)."""
    url = f"{BASE}/seasons?sportId=1&season={season}"
    _, data = fetch_json(session, url)
    seasons = (data or {}).get("seasons") or []
    if seasons:
        d = seasons[0].get("regularSeasonStartDate")
        if d:
            return d[:10]
    return f"{season}-{DEFAULT_SEASON_START_MMDD}"


def is_level_move(tx: dict, season_start_date: str) -> bool:
    """Whitelist by type_code + drop spring-training MLB-camp assignments.

    Carve-out applies to both ASG and SC entries with toTeam=MLB between
    Feb 1 and opening day. The spec called out ASG only; SC is included
    because the API uses SC for the same "join MLB camp" pattern in
    spring training, and leaving it would still pollute prior_level.
    """
    code = (tx.get("typeCode") or "").upper()
    if code not in LEVEL_MOVE_TYPES:
        return False
    if code in ("ASG", "SC"):
        to_id = (tx.get("toTeam") or {}).get("id")
        date = (tx.get("date") or "")[:10]
        season_year = season_start_date[:4]
        spring_start = f"{season_year}-02-01"
        if (to_id == PARENT_TEAM_ID and date
                and spring_start <= date < season_start_date):
            return False
    return True


def filter_level_tx(transactions: list[dict], season_start_date: str) -> list[dict]:
    return [t for t in transactions if is_level_move(t, season_start_date)]


def main() -> int:
    season = get_season()
    today = datetime.now(timezone.utc).date()
    start = (today - timedelta(days=WINDOW_DAYS)).isoformat()
    end = today.isoformat()

    mlb = load_json(MLB_CACHE)
    milb = load_json(MILB_CACHE)
    if not mlb or not milb:
        log_scrape("OrgHitters", "<no-cache>", 0, 0, note="missing_cache_files")
        return 0

    team_to_level: dict[int, str] = {PARENT_TEAM_ID: "MLB"}
    for aff in (milb.get("affiliates") or []):
        if aff.get("id") and aff.get("level"):
            team_to_level[aff["id"]] = aff["level"]

    player_ids, primary_level, primary_position, name_by_id = build_org_players(mlb, milb)
    if not player_ids:
        log_scrape("OrgHitters", "<no-players>", 0, 0, note="no_hitters_resolved")
        return 0

    session = make_session()

    stats_map = fetch_player_stats(session, player_ids, season, start, end)
    raw_tx = fetch_transactions(session, milb)
    season_start_date = fetch_season_start(session, season)
    level_tx = filter_level_tx(raw_tx, season_start_date)

    hitters_out: list[dict[str, Any]] = []
    fallback_used = 0
    for pid in player_ids:
        primary_lvl = primary_level[pid]
        current_since, prior_level, prior_since = derive_levels(
            pid, primary_lvl, level_tx, team_to_level
        )
        if not prior_level:
            extra_tx = fallback_personid(session, pid, season)
            time.sleep(SUBMIT_DELAY_S)
            fallback_used += 1
            extra_filtered = filter_level_tx(extra_tx, season_start_date)
            cs2, pl2, ps2 = derive_levels(pid, primary_lvl, extra_filtered, team_to_level)
            if current_since is None:
                current_since = cs2
            if prior_level is None:
                prior_level = pl2
                prior_since = ps2

        season_blocks: list[dict | None] = []
        last_10_blocks: list[dict | None] = []
        last_10_levels: list[str] = []
        season_levels: list[str] = []
        for sport_id, lvl in ALL_SPORT_LEVELS:
            entry = stats_map.get((pid, sport_id)) or {}
            sb = entry.get("season")
            lb = entry.get("last_10")
            if sb and (to_int(sb.get("ab")) or 0) > 0:
                season_blocks.append(sb)
                season_levels.append(lvl)
            if lb and (to_int(lb.get("ab")) or 0) > 0:
                last_10_blocks.append(lb)
                last_10_levels.append(lvl)

        hitters_out.append({
            "id": pid,
            "name": name_by_id.get(pid) or "",
            "position": primary_position.get(pid) or "",
            "level": primary_lvl,
            "level_since": current_since,
            "prior_level": prior_level,
            "prior_level_since": prior_since,
            "season": aggregate_blocks(season_blocks),
            "last_10": aggregate_blocks(last_10_blocks),
            "season_levels": season_levels,
            "last_10_levels": last_10_levels,
        })

    log_scrape("MLB personId fallback", "<bulk>", 200, fallback_used,
               note="players needing prior_level fallback")

    hitters_out.sort(key=lambda h: (h.get("name") or "").lower())

    payload = {
        "updated": now_iso(),
        "season": season,
        "window_days": WINDOW_DAYS,
        "window_start": start,
        "window_end": end,
        "hitters": hitters_out,
    }
    atomic_write(HITTERS_PATH, payload)

    seen_tx: set[Any] = set()
    tx_normalized: list[dict[str, Any]] = []
    for t in raw_tx:
        code = (t.get("typeCode") or "").upper()
        if code in NOISE_TX_TYPES:
            continue
        tid = t.get("id")
        if tid is None or tid in seen_tx:
            continue
        seen_tx.add(tid)
        norm = normalize_tx(t, team_to_level)
        if not norm["date"]:
            continue
        tx_normalized.append(norm)
    tx_normalized.sort(key=lambda t: t["date"], reverse=True)

    tx_payload = {"updated": now_iso(), "items": tx_normalized}
    atomic_write(TX_PATH, tx_payload)

    update_last_updated(payload["updated"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
