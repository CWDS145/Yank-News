"""Fetch Yankees MiLB affiliate stats from MLB Stats API. Public, no auth.

Replaces the dropped FG MiLB scraper (FanGraphs no longer publishes per-affiliate
/teams/<slug> pages). Lives on the safe-tier 30-min cadence.
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

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_PATH = REPO_ROOT / "data" / "stats-milb.json"
LAST_UPDATED_PATH = REPO_ROOT / "data" / "last-updated.json"

BASE = "https://statsapi.mlb.com/api/v1"
PARENT_TEAM_ID = 147
USER_AGENT = "YankNewsBot/1.0 (+https://github.com/cwds145/Yank-News)"
TIMEOUT = 25
WORKERS = 8
SUBMIT_DELAY_S = 0.1

PITCHER_POSITION_CODES = {"P", "SP", "RP", "CP", "LHP", "RHP", "TWP"}
SPORT_LEVEL = {11: "AAA", 12: "AA", 13: "High-A", 14: "Low-A"}
KEEP_SPORT_IDS = set(SPORT_LEVEL.keys())

HITTING_FIELDS = [
    ("gamesPlayed", "G"), ("atBats", "AB"), ("runs", "R"), ("hits", "H"),
    ("doubles", "2B"), ("triples", "3B"), ("homeRuns", "HR"), ("rbi", "RBI"),
    ("baseOnBalls", "BB"), ("strikeOuts", "SO"), ("stolenBases", "SB"),
    ("avg", "AVG"), ("obp", "OBP"), ("slg", "SLG"), ("ops", "OPS"),
]
PITCHING_FIELDS = [
    ("gamesPlayed", "G"), ("gamesStarted", "GS"), ("wins", "W"), ("losses", "L"),
    ("saves", "SV"), ("inningsPitched", "IP"), ("hits", "H"), ("runs", "R"),
    ("earnedRuns", "ER"), ("baseOnBalls", "BB"), ("strikeOuts", "SO"),
    ("era", "ERA"), ("whip", "WHIP"), ("homeRuns", "HR"),
]


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_season() -> int:
    override = os.environ.get("SEASON_OVERRIDE")
    if override:
        try:
            return int(override)
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


def atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def read_existing(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def update_last_updated(stamp: str) -> None:
    lu = {"news": None, "stats_safe": None, "stats_scraped": None}
    if LAST_UPDATED_PATH.exists():
        try:
            with open(LAST_UPDATED_PATH, "r", encoding="utf-8") as f:
                existing = json.load(f)
            if isinstance(existing, dict):
                for k in ("news", "stats_safe", "stats_scraped"):
                    if k in existing:
                        lu[k] = existing[k]
        except (json.JSONDecodeError, OSError):
            pass
    lu["stats_safe"] = stamp
    atomic_write(LAST_UPDATED_PATH, lu)


def extract_team_stat(data: dict | None,
                       fields: list[tuple[str, str]]) -> dict[str, Any]:
    if not data:
        return {}
    stats = data.get("stats") or []
    if not stats:
        return {}
    splits = stats[0].get("splits") or []
    if not splits:
        return {}
    stat = splits[0].get("stat") or {}
    return {alias: stat.get(api_key) for api_key, alias in fields}


def extract_player_stat(data: dict, group: str) -> dict[str, Any] | None:
    stats = data.get("stats") or []
    if not stats:
        return None
    splits = stats[0].get("splits") or []
    if not splits:
        return None
    stat = splits[0].get("stat") or {}
    if not stat:
        return None
    fields = HITTING_FIELDS if group == "hitting" else PITCHING_FIELDS
    return {alias: stat.get(api_key) for api_key, alias in fields}


def resolve_affiliates(session: requests.Session, season: int) -> tuple[int, list[dict[str, Any]]]:
    """The MLB API ignores sportId on /teams/affiliates and returns all 11 Yankees
    affiliates (including DSL/FCL/ATS). Filter client-side to AAA/AA/High-A/Low-A.
    """
    url = f"{BASE}/teams/affiliates?teamIds={PARENT_TEAM_ID}&season={season}"
    status, data = fetch_json(session, url)
    teams = (data or {}).get("teams") or []
    affiliates: list[dict[str, Any]] = []
    for t in teams:
        sid = (t.get("sport") or {}).get("id")
        if sid not in KEEP_SPORT_IDS:
            continue
        affiliates.append({
            "id": t.get("id"),
            "name": t.get("name"),
            "abbr": t.get("abbreviation"),
            "level": SPORT_LEVEL.get(sid),
            "sport_id": sid,
            "league": (t.get("league") or {}).get("name"),
        })
    affiliates.sort(key=lambda a: a["sport_id"])
    log_scrape("MLB MiLB: affiliates", url, status, len(affiliates),
               note="ok" if affiliates else ("no_data" if status == 200 else f"http_{status}"))
    return status, affiliates


def fetch_roster(session: requests.Session, affiliate: dict[str, Any],
                  season: int) -> tuple[int, list[dict[str, Any]]]:
    url = f"{BASE}/teams/{affiliate['id']}/roster?rosterType=active&season={season}"
    status, data = fetch_json(session, url)
    entries = (data or {}).get("roster") or []
    roster: list[dict[str, Any]] = []
    for e in entries:
        person = e.get("person") or {}
        position = e.get("position") or {}
        status_obj = e.get("status") or {}
        roster.append({
            "id": person.get("id"),
            "name": person.get("fullName"),
            "jersey": e.get("jerseyNumber"),
            "position": position.get("abbreviation"),
            "status": status_obj.get("description"),
        })
    log_scrape(f"MLB MiLB: {affiliate['abbr']} roster", url, status, len(roster),
               note="ok" if data else f"http_{status}")
    return status, roster


def fetch_team_stats(session: requests.Session, affiliate: dict[str, Any],
                      season: int) -> tuple[dict[str, Any], dict[str, Any], int]:
    aid = affiliate["id"]
    sid = affiliate["sport_id"]
    bat_url = (f"{BASE}/teams/stats?season={season}&stats=season&group=hitting"
               f"&teamId={aid}&sportId={sid}")
    pit_url = (f"{BASE}/teams/stats?season={season}&stats=season&group=pitching"
               f"&teamId={aid}&sportId={sid}")
    bat_status, bat_data = fetch_json(session, bat_url)
    pit_status, pit_data = fetch_json(session, pit_url)
    batting = extract_team_stat(bat_data, HITTING_FIELDS)
    pitching = extract_team_stat(pit_data, PITCHING_FIELDS)
    items = (1 if batting else 0) + (1 if pitching else 0)
    combined_status = bat_status if bat_status == pit_status else (bat_status or pit_status)
    log_scrape(f"MLB MiLB: {affiliate['abbr']} team stats", bat_url,
               combined_status, items,
               note="ok" if items == 2 else "partial" if items else "no_data")
    return batting, pitching, combined_status


def fetch_player_stats(session: requests.Session, affiliate: dict[str, Any],
                        roster: list[dict[str, Any]],
                        season: int) -> dict[str, dict[str, Any]]:
    sid = affiliate["sport_id"]

    def task(player: dict[str, Any]) -> tuple[int, dict[str, Any]] | None:
        pid = player.get("id")
        if pid is None:
            return None
        position = (player.get("position") or "").upper()
        group = "pitching" if position in PITCHER_POSITION_CODES else "hitting"
        url = (f"{BASE}/people/{pid}/stats?stats=season&group={group}"
               f"&season={season}&sportId={sid}")
        status, data = fetch_json(session, url)
        if status != 200 or not data:
            return None
        extracted = extract_player_stat(data, group)
        if not extracted:
            return None
        return pid, {
            "name": player.get("name"),
            "group": group,
            "stats": extracted,
        }

    out: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = []
        for p in roster:
            if p.get("id") is None:
                continue
            futures.append(ex.submit(task, p))
            time.sleep(SUBMIT_DELAY_S)
        for fut in as_completed(futures):
            try:
                result = fut.result()
            except Exception:
                continue
            if not result:
                continue
            pid, payload = result
            out[str(pid)] = payload
    log_scrape(f"MLB MiLB: {affiliate['abbr']} player stats",
               f"<bulk: {len(roster)} players>", 200, len(out), note="ok")
    return out


def main() -> int:
    season = get_season()
    session = make_session()
    existing = read_existing(OUTPUT_PATH)
    existing_by_id = {
        a.get("id"): a
        for a in (existing.get("affiliates") or [])
        if isinstance(a, dict) and a.get("id") is not None
    }

    aff_status, affiliates = resolve_affiliates(session, season)
    if aff_status != 200 or not affiliates:
        # LKG preservation: leave previous file untouched.
        return 0

    output_affiliates: list[dict[str, Any]] = []
    for aff in affiliates:
        ros_status, roster = fetch_roster(session, aff, season)
        if ros_status != 200:
            # Per-affiliate LKG: keep prior block if we have one, else write
            # the resolved metadata with empty stats so the dashboard can still
            # render the affiliate row.
            prev = existing_by_id.get(aff["id"])
            if prev:
                output_affiliates.append(prev)
                continue
            output_affiliates.append({
                "id": aff["id"], "name": aff["name"], "abbr": aff["abbr"],
                "level": aff["level"], "sport_id": aff["sport_id"],
                "league": aff["league"],
                "team_batting": {}, "team_pitching": {},
                "roster": [], "player_stats": {},
            })
            continue

        team_batting, team_pitching, _ = fetch_team_stats(session, aff, season)
        player_stats = fetch_player_stats(session, aff, roster, season) if roster else {}

        output_affiliates.append({
            "id": aff["id"],
            "name": aff["name"],
            "abbr": aff["abbr"],
            "level": aff["level"],
            "sport_id": aff["sport_id"],
            "league": aff["league"],
            "team_batting": team_batting,
            "team_pitching": team_pitching,
            "roster": roster,
            "player_stats": player_stats,
        })

    payload = {
        "updated": now_iso(),
        "season": season,
        "source": "MLB Stats API",
        "affiliates": output_affiliates,
    }
    atomic_write(OUTPUT_PATH, payload)
    update_last_updated(payload["updated"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
