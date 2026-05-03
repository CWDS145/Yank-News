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


def is_hitter(position: str | None, group: str | None) -> bool:
    pos = (position or "").upper()
    if pos == TWP:
        return group == "hitting"
    return pos not in PITCHER_POS


def build_org_players(mlb: dict, milb: dict) -> list[dict[str, Any]]:
    """Walk cached MLB + MiLB rosters, dedupe by player id (MLB > AAA > AA > High-A > Low-A)."""
    players: list[dict[str, Any]] = []
    seen: set[int] = set()

    mlb_player_stats = (mlb or {}).get("player_stats") or {}
    for r in (mlb or {}).get("roster", []):
        pid = r.get("id")
        if pid is None or pid in seen:
            continue
        ps = mlb_player_stats.get(str(pid)) or {}
        if not is_hitter(r.get("position"), ps.get("group")):
            continue
        stats = ps.get("stats") if ps.get("group") == "hitting" else None
        players.append({
            "id": pid, "name": r.get("name"),
            "position": r.get("position"),
            "level": "MLB", "team_id": PARENT_TEAM_ID, "sport_id": 1,
            "season_stats": stats,
        })
        seen.add(pid)

    affiliates = sorted(
        (milb or {}).get("affiliates") or [],
        key=lambda a: LEVEL_RANK.get(a.get("level"), 99),
    )
    for aff in affiliates:
        level = aff.get("level")
        team_id = aff.get("id")
        sport_id = aff.get("sport_id")
        ps_dict = aff.get("player_stats") or {}
        for r in aff.get("roster", []) or []:
            pid = r.get("id")
            if pid is None or pid in seen:
                continue
            ps = ps_dict.get(str(pid)) or {}
            if not is_hitter(r.get("position"), ps.get("group")):
                continue
            stats = ps.get("stats") if ps.get("group") == "hitting" else None
            players.append({
                "id": pid, "name": r.get("name"),
                "position": r.get("position"),
                "level": level, "team_id": team_id, "sport_id": sport_id,
                "season_stats": stats,
            })
            seen.add(pid)

    return players


def fetch_last_10(session: requests.Session, players: list[dict],
                   season: int, start: str, end: str) -> dict[int, dict | None]:
    """Pull byDateRange hitting stats. Returns {player_id: stats_or_None}.
    Logs once per level with aggregated success counts.
    """
    out: dict[int, dict | None] = {}
    by_level_count: dict[str, int] = {}
    by_level_ok: dict[str, int] = {}
    for p in players:
        by_level_count[p["level"]] = by_level_count.get(p["level"], 0) + 1
        by_level_ok.setdefault(p["level"], 0)

    def task(p: dict[str, Any]) -> tuple[int, str, int, dict | None]:
        url = (
            f"{BASE}/people/{p['id']}/stats?stats=byDateRange&group=hitting"
            f"&startDate={start}&endDate={end}&season={season}&sportId={p['sport_id']}"
        )
        status, data = fetch_json(session, url)
        if status != 200 or not data:
            return p["id"], p["level"], status, None
        splits = ((data.get("stats") or [{}])[0].get("splits") or [])
        if not splits:
            return p["id"], p["level"], status, {"ab": 0, "k": 0, "avg": None, "ops": None}
        s = splits[0].get("stat") or {}
        return p["id"], p["level"], status, {
            "ab": s.get("atBats"),
            "k": s.get("strikeOuts"),
            "avg": s.get("avg"),
            "ops": s.get("ops"),
        }

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = []
        for p in players:
            futures.append(ex.submit(task, p))
            time.sleep(SUBMIT_DELAY_S)
        for fut in as_completed(futures):
            try:
                pid, level, status, stats = fut.result()
            except Exception:
                continue
            out[pid] = stats
            if stats is not None and status == 200:
                by_level_ok[level] = by_level_ok.get(level, 0) + 1

    for lvl, count in by_level_count.items():
        ok = by_level_ok.get(lvl, 0)
        log_scrape(
            f"MLB byDateRange: {lvl}",
            f"{BASE}/people/{{id}}/stats?stats=byDateRange&group=hitting&sportId={{sid}}",
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

    org_players = build_org_players(mlb, milb)
    if not org_players:
        log_scrape("OrgHitters", "<no-players>", 0, 0, note="no_hitters_resolved")
        return 0

    session = make_session()

    last10_map = fetch_last_10(session, org_players, season, start, end)
    raw_tx = fetch_transactions(session, milb)

    hitters_out: list[dict[str, Any]] = []
    fallback_used = 0
    for p in org_players:
        current_since, prior_level, prior_since = derive_levels(
            p["id"], p["level"], raw_tx, team_to_level
        )
        if not prior_level:
            extra_tx = fallback_personid(session, p["id"], season)
            time.sleep(SUBMIT_DELAY_S)
            fallback_used += 1
            cs2, pl2, ps2 = derive_levels(p["id"], p["level"], extra_tx, team_to_level)
            if current_since is None:
                current_since = cs2
            if prior_level is None:
                prior_level = pl2
                prior_since = ps2

        season_s = p.get("season_stats") or {}
        s_avg = season_s.get("AVG") or season_s.get("avg")
        s_ops = season_s.get("OPS") or season_s.get("ops")
        s_ab = season_s.get("AB") or season_s.get("atBats")
        s_so = season_s.get("SO") or season_s.get("strikeOuts")
        season_block = {
            "ab": s_ab, "k": s_so,
            "avg": s_avg, "ops": s_ops,
            "avg_num": to_num(s_avg), "ops_num": to_num(s_ops),
        } if season_s else None

        l10 = last10_map.get(p["id"])
        last_10_block = None
        if l10 is not None:
            last_10_block = {
                "ab": l10.get("ab"), "k": l10.get("k"),
                "avg": l10.get("avg"), "ops": l10.get("ops"),
                "avg_num": to_num(l10.get("avg")),
                "ops_num": to_num(l10.get("ops")),
            }

        hitters_out.append({
            "id": p["id"],
            "name": p["name"],
            "position": p["position"],
            "level": p["level"],
            "level_since": current_since,
            "prior_level": prior_level,
            "prior_level_since": prior_since,
            "season": season_block,
            "last_10": last_10_block,
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
