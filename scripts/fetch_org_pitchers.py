"""Org-wide Yankees pitchers tracker. Reads cached MLB + MiLB rosters,
fetches season pitching stats per (player, sport_id) via MLB Stats API,
aggregates raw counts across every level a pitcher has been at, and
writes data/org-pitchers.json. Mirrors fetch_org_hitters.py but for the
pitching group.
"""
from __future__ import annotations

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib_scrape_log import log_scrape
# Reuse helpers + level-history derivation from the hitters script. Both files
# share the season-cache + transaction model; pitcher-specific logic lives here.
from fetch_org_hitters import (
    PARENT_TEAM_ID, BASE, USER_AGENT, TIMEOUT, WORKERS, SUBMIT_DELAY_S,
    LEVEL_RANK, ALL_SPORT_LEVELS,
    now_iso, get_season, make_session, fetch_json, load_json, atomic_write,
    to_int, to_num,
    fetch_transactions, fetch_season_start, filter_level_tx, derive_levels,
    fallback_personid,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA = REPO_ROOT / "data"
PITCHERS_PATH = DATA / "org-pitchers.json"
MLB_CACHE = DATA / "stats-mlb.json"
MILB_CACHE = DATA / "stats-milb.json"

PITCHER_POS = {"P", "SP", "RP", "CP", "LHP", "RHP"}
TWP = "TWP"


def is_pitcher(position: str | None, group: str | None) -> bool:
    pos = (position or "").upper()
    if pos == TWP:
        return group == "pitching"
    return pos in PITCHER_POS


def build_org_pitchers(mlb: dict, milb: dict
                        ) -> tuple[list[int], dict[int, str], dict[int, str], dict[int, str]]:
    """Walk MLB + MiLB rosters; return (player_ids, primary_level, primary_position, name)."""
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
        if not is_pitcher(r.get("position"), ps.get("group")):
            continue
        if pid not in primary_level:
            player_ids.append(pid)
            primary_level[pid] = "MLB"
            primary_position[pid] = r.get("position") or "P"
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
            if not is_pitcher(r.get("position"), ps.get("group")):
                continue
            if pid not in primary_level:
                player_ids.append(pid)
                primary_level[pid] = level
                primary_position[pid] = r.get("position") or "P"
                name[pid] = r.get("name") or ""

    return player_ids, primary_level, primary_position, name


def _ip_to_outs(ip: Any) -> int:
    """'19.1' → 58 outs (19*3 + 1). '19.2' → 59. '19.0' or '19' → 57."""
    if ip in (None, ""):
        return 0
    s = str(ip)
    if "." not in s:
        try:
            return int(float(s)) * 3
        except (ValueError, TypeError):
            return 0
    whole, _, frac = s.partition(".")
    try:
        w = int(whole)
    except ValueError:
        return 0
    f = 0
    if frac:
        try:
            f = int(frac[0])  # only first decimal digit; "0.2" → 2 outs
        except ValueError:
            f = 0
    return w * 3 + f


def _outs_to_ip(outs: int) -> str:
    return f"{outs // 3}.{outs % 3}"


def _parse_pitching_block(stat: dict) -> dict:
    return {
        "w": to_int(stat.get("wins")) or 0,
        "l": to_int(stat.get("losses")) or 0,
        "g": to_int(stat.get("gamesPlayed")) or 0,
        "gs": to_int(stat.get("gamesStarted")) or 0,
        "cg": to_int(stat.get("completeGames")) or 0,
        "sho": to_int(stat.get("shutouts")) or 0,
        "sv": to_int(stat.get("saves")) or 0,
        "svo": to_int(stat.get("saveOpportunities")) or 0,
        "outs": _ip_to_outs(stat.get("inningsPitched")),
        "h": to_int(stat.get("hits")) or 0,
        "r": to_int(stat.get("runs")) or 0,
        "er": to_int(stat.get("earnedRuns")) or 0,
        "hr": to_int(stat.get("homeRuns")) or 0,
        "hb": to_int(stat.get("hitByPitch")) or 0,
        "bb": to_int(stat.get("baseOnBalls")) or 0,
        "so": to_int(stat.get("strikeOuts")) or 0,
        "ab": to_int(stat.get("atBats")) or 0,
    }


def aggregate_pitching(blocks: list[dict | None]) -> dict | None:
    """Sum raw counts across per-level blocks; recompute ERA/WHIP/BAA from totals.
    Returns the frontend-compatible block (w, l, era, ..., whip, avg, ...).
    """
    valid = [b for b in blocks if b]
    if not valid:
        return None
    keys = ["w","l","g","gs","cg","sho","sv","svo","outs","h","r","er","hr","hb","bb","so","ab"]
    tot = {k: sum(b.get(k) or 0 for b in valid) for k in keys}
    if tot["outs"] == 0 and tot["g"] == 0:
        return None
    ip_str = _outs_to_ip(tot["outs"])
    era = (tot["er"] * 27 / tot["outs"]) if tot["outs"] > 0 else None
    whip = ((tot["bb"] + tot["h"]) * 3 / tot["outs"]) if tot["outs"] > 0 else None
    baa = (tot["h"] / tot["ab"]) if tot["ab"] > 0 else None
    return {
        "w": tot["w"], "l": tot["l"], "g": tot["g"], "gs": tot["gs"],
        "cg": tot["cg"], "sho": tot["sho"], "sv": tot["sv"], "svo": tot["svo"],
        "ip": ip_str, "ip_outs": tot["outs"],
        "h": tot["h"], "r": tot["r"], "er": tot["er"],
        "hr": tot["hr"], "hb": tot["hb"], "bb": tot["bb"], "so": tot["so"],
        "era": (f"{era:.2f}" if era is not None else None),
        "era_num": era,
        "whip": (f"{whip:.2f}" if whip is not None else None),
        "whip_num": whip,
        "avg": (f"{baa:.3f}".lstrip("0") if baa is not None else None),
        "avg_num": baa,
    }


def fetch_pitcher_stats(session: requests.Session, player_ids: list[int],
                         season: int) -> dict[tuple[int, int], dict | None]:
    """For every (pitcher × sport_id), fetch season pitching stats.
    Returns {(pid, sport_id): pitching_block_or_None}.
    """
    targets: list[tuple[int, int, str]] = [
        (pid, sid, lvl) for pid in player_ids for sid, lvl in ALL_SPORT_LEVELS
    ]
    out: dict[tuple[int, int], dict | None] = {}
    by_level_count: dict[str, int] = {}
    by_level_ok: dict[str, int] = {}
    for _, _, lvl in targets:
        by_level_count[lvl] = by_level_count.get(lvl, 0) + 1
        by_level_ok.setdefault(lvl, 0)

    def task(pid: int, sport_id: int, lvl: str
             ) -> tuple[int, int, str, int, dict | None]:
        url = (
            f"{BASE}/people/{pid}/stats?stats=season&group=pitching"
            f"&season={season}&sportId={sport_id}"
        )
        status, data = fetch_json(session, url)
        if status != 200 or not data:
            return pid, sport_id, lvl, status, None
        for stat_set in (data.get("stats") or []):
            type_name = ((stat_set.get("type") or {}).get("displayName") or "").lower()
            if not type_name.startswith("season"):
                continue
            splits = stat_set.get("splits") or []
            if not splits:
                continue
            return pid, sport_id, lvl, status, _parse_pitching_block(splits[0].get("stat") or {})
        return pid, sport_id, lvl, status, None

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = []
        for pid, sid, lvl in targets:
            futures.append(ex.submit(task, pid, sid, lvl))
            time.sleep(SUBMIT_DELAY_S)
        for fut in as_completed(futures):
            try:
                pid, sport_id, lvl, status, block = fut.result()
            except Exception:
                continue
            out[(pid, sport_id)] = block
            if status == 200:
                by_level_ok[lvl] = by_level_ok.get(lvl, 0) + 1

    for lvl, count in by_level_count.items():
        ok = by_level_ok.get(lvl, 0)
        log_scrape(
            f"MLB season pitching: {lvl}",
            f"{BASE}/people/{{id}}/stats?stats=season&group=pitching&sportId={{sid}}",
            200 if ok else 0, ok,
            note=f"ok={ok}/{count}",
        )
    return out


def main() -> int:
    season = get_season()

    mlb = load_json(MLB_CACHE)
    milb = load_json(MILB_CACHE)
    if not mlb or not milb:
        log_scrape("OrgPitchers", "<no-cache>", 0, 0, note="missing_cache_files")
        return 0

    team_to_level: dict[int, str] = {PARENT_TEAM_ID: "MLB"}
    for aff in (milb.get("affiliates") or []):
        if aff.get("id") and aff.get("level"):
            team_to_level[aff["id"]] = aff["level"]

    player_ids, primary_level, primary_position, name_by_id = build_org_pitchers(mlb, milb)
    if not player_ids:
        log_scrape("OrgPitchers", "<no-players>", 0, 0, note="no_pitchers_resolved")
        return 0

    session = make_session()

    stats_map = fetch_pitcher_stats(session, player_ids, season)
    raw_tx = fetch_transactions(session, milb)
    season_start_date = fetch_season_start(session, season)
    level_tx = filter_level_tx(raw_tx, season_start_date)

    pitchers_out: list[dict[str, Any]] = []
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
        season_levels: list[str] = []
        for sport_id, lvl in ALL_SPORT_LEVELS:
            block = stats_map.get((pid, sport_id))
            if block and (block.get("g") or 0) > 0:
                season_blocks.append(block)
                season_levels.append(lvl)

        pitchers_out.append({
            "id": pid,
            "name": name_by_id.get(pid) or "",
            "position": primary_position.get(pid) or "P",
            "level": primary_lvl,
            "level_since": current_since,
            "prior_level": prior_level,
            "prior_level_since": prior_since,
            "season": aggregate_pitching(season_blocks),
            "season_levels": season_levels,
        })

    log_scrape("MLB personId fallback (pitchers)", "<bulk>", 200, fallback_used,
               note="pitchers needing prior_level fallback")

    pitchers_out.sort(key=lambda p: (p.get("name") or "").lower())

    payload = {
        "updated": now_iso(),
        "season": season,
        "pitchers": pitchers_out,
    }
    atomic_write(PITCHERS_PATH, payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
