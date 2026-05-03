"""Fetch Yankees stats from MLB Stats API. Public, no auth."""
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
OUTPUT_PATH = REPO_ROOT / "data" / "stats-mlb.json"
LAST_UPDATED_PATH = REPO_ROOT / "data" / "last-updated.json"

BASE = "https://statsapi.mlb.com/api/v1"
TEAM_ID = 147
USER_AGENT = "YankNewsBot/1.0 (+https://github.com/cwds145/Yank-News)"
TIMEOUT = 25
WORKERS = 8
SUBMIT_DELAY_S = 0.1
PITCHER_POSITION_CODES = {"P", "SP", "RP", "CP", "LHP", "RHP", "TWP"}

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
    ("era", "ERA"), ("whip", "WHIP"), ("avg", "AVG"), ("homeRuns", "HR"),
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


def to_int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


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


def extract_team_stat(data: dict | None, fields: list[tuple[str, str]]) -> dict[str, Any]:
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


def fetch_team_block(session: requests.Session, season: int) -> tuple[dict, dict]:
    url = f"{BASE}/teams/{TEAM_ID}?hydrate=record(type=regularSeason),league,division"
    status, data = fetch_json(session, url)
    log_scrape("MLB StatsAPI: team", url, status, 1 if data else 0,
               note="ok" if data else "no_data")

    team_block = {"id": TEAM_ID, "name": "New York Yankees", "abbr": "NYY",
                  "league": None, "division": None}
    record_block = {
        "w": None, "l": None, "pct": None, "gb": None,
        "runs_scored": None, "runs_allowed": None, "run_diff": None,
        "streak": None, "last10": None,
    }
    if data and data.get("teams"):
        t = data["teams"][0]
        team_block["id"] = t.get("id", TEAM_ID)
        team_block["name"] = t.get("name") or team_block["name"]
        team_block["abbr"] = t.get("abbreviation") or team_block["abbr"]
        team_block["league"] = (t.get("league") or {}).get("name")
        team_block["division"] = (t.get("division") or {}).get("name")
        rec = t.get("record") or {}
        streak = rec.get("streak") or {}
        record_block.update({
            "w": to_int(rec.get("wins")),
            "l": to_int(rec.get("losses")),
            "pct": rec.get("winningPercentage") or rec.get("pct"),
            "gb": rec.get("divisionGamesBack") or rec.get("gamesBack"),
            "runs_scored": to_int(rec.get("runsScored")),
            "runs_allowed": to_int(rec.get("runsAllowed")),
            "run_diff": to_int(rec.get("runDifferential")),
            "streak": streak.get("streakCode") if isinstance(streak, dict) else None,
        })
    return team_block, record_block


def fetch_standings(session: requests.Session, season: int,
                    record_block: dict) -> list[dict[str, Any]]:
    url = f"{BASE}/standings?leagueId=103&season={season}&standingsTypes=regularSeason"
    status, data = fetch_json(session, url)
    log_scrape("MLB StatsAPI: standings", url, status, 1 if data else 0,
               note="ok" if data else "no_data")
    al_east: list[dict[str, Any]] = []
    if not data:
        return al_east
    for div_record in data.get("records") or []:
        div = div_record.get("division") or {}
        div_name = (div.get("name") or "").lower()
        if div.get("id") != 201 and "east" not in div_name:
            continue
        for tr in div_record.get("teamRecords") or []:
            team = tr.get("team") or {}
            streak = tr.get("streak") or {}
            last10 = None
            for s in (tr.get("records") or {}).get("splitRecords") or []:
                if (s.get("type") or "").lower() == "lastten":
                    last10 = f"{s.get('wins', 0)}-{s.get('losses', 0)}"
                    break
            al_east.append({
                "team_id": team.get("id"),
                "name": team.get("name"),
                "abbr": team.get("abbreviation"),
                "w": to_int(tr.get("wins")),
                "l": to_int(tr.get("losses")),
                "pct": tr.get("winningPercentage") or tr.get("pct"),
                "gb": tr.get("gamesBack"),
                "wcgb": tr.get("wildCardGamesBack"),
                "last10": last10,
                "streak": streak.get("streakCode") if isinstance(streak, dict) else None,
                "run_diff": to_int(tr.get("runDifferential")),
            })
            if team.get("id") == TEAM_ID and record_block.get("last10") is None:
                record_block["last10"] = last10
        break
    return al_east


def parse_roster_entries(entries: list[dict]) -> list[dict[str, Any]]:
    out = []
    for entry in entries:
        person = entry.get("person") or {}
        position = entry.get("position") or {}
        status_obj = entry.get("status") or {}
        out.append({
            "id": person.get("id"),
            "name": person.get("fullName"),
            "jersey": entry.get("jerseyNumber"),
            "position": position.get("abbreviation"),
            "position_name": position.get("name"),
            "is_active": False,
            "status": status_obj.get("description"),
            "status_code": status_obj.get("code"),
        })
    return out


def fetch_roster(session: requests.Session, season: int) -> tuple[list[dict], list[dict]]:
    # 40-man
    url_40 = f"{BASE}/teams/{TEAM_ID}/roster?rosterType=40Man&season={season}"
    status, data = fetch_json(session, url_40)
    entries_40 = (data or {}).get("roster") or []
    log_scrape("MLB StatsAPI: roster 40Man", url_40, status, len(entries_40),
               note="ok" if data else "no_data")
    roster = parse_roster_entries(entries_40)

    # active
    url_active = f"{BASE}/teams/{TEAM_ID}/roster?rosterType=active&season={season}"
    status, data = fetch_json(session, url_active)
    active_entries = (data or {}).get("roster") or []
    active_ids = {(e.get("person") or {}).get("id") for e in active_entries}
    log_scrape("MLB StatsAPI: roster active", url_active, status, len(active_entries),
               note="ok" if data else "no_data")
    for r in roster:
        if r["id"] in active_ids:
            r["is_active"] = True

    # full season for IL — combined with 40-man because each source omits some
    # IL'd players (60-day IL'd are sometimes only in fullSeason; recent IL
    # moves only show in 40Man until rosters re-sync).
    url_full = f"{BASE}/teams/{TEAM_ID}/roster?rosterType=fullSeason&season={season}"
    status, data = fetch_json(session, url_full)
    full_entries = (data or {}).get("roster") or []
    log_scrape("MLB StatsAPI: roster fullSeason", url_full, status, len(full_entries),
               note="ok" if data else "no_data")

    injuries: list[dict[str, Any]] = []
    seen_il: set[int | None] = set()
    for entry in entries_40 + full_entries:
        status_obj = entry.get("status") or {}
        code = (status_obj.get("code") or "").upper()
        if not (code.startswith("D") or code.startswith("IL")):
            continue
        person = entry.get("person") or {}
        pid = person.get("id")
        if pid in seen_il:
            continue
        seen_il.add(pid)
        position = entry.get("position") or {}
        injuries.append({
            "id": pid,
            "name": person.get("fullName"),
            "position": position.get("abbreviation"),
            "status": status_obj.get("description"),
            "status_code": status_obj.get("code"),
        })

    return roster, injuries


def fetch_schedule(session: requests.Session) -> list[dict[str, Any]]:
    today = datetime.now(timezone.utc).date()
    start = (today - timedelta(days=7)).strftime("%Y-%m-%d")
    end = (today + timedelta(days=7)).strftime("%Y-%m-%d")
    url = f"{BASE}/schedule?sportId=1&teamId={TEAM_ID}&startDate={start}&endDate={end}"
    status, data = fetch_json(session, url)
    games: list[dict[str, Any]] = []
    if data:
        for date_block in data.get("dates") or []:
            for game in date_block.get("games") or []:
                teams = game.get("teams") or {}
                home_rec = teams.get("home") or {}
                away_rec = teams.get("away") or {}
                home_team = home_rec.get("team") or {}
                away_team = away_rec.get("team") or {}
                is_home = home_team.get("id") == TEAM_ID
                opp = away_team if is_home else home_team
                decisions = game.get("decisions") or {}
                games.append({
                    "gamePk": game.get("gamePk"),
                    "date": game.get("gameDate"),
                    "home_away": "home" if is_home else "away",
                    "opponent": opp.get("name"),
                    "opponent_abbr": opp.get("abbreviation"),
                    "opponent_id": opp.get("id"),
                    "status": (game.get("status") or {}).get("detailedState"),
                    "home_score": to_int(home_rec.get("score")),
                    "away_score": to_int(away_rec.get("score")),
                    "winning_pitcher": (decisions.get("winner") or {}).get("fullName"),
                    "losing_pitcher": (decisions.get("loser") or {}).get("fullName"),
                })
    log_scrape("MLB StatsAPI: schedule", url, status, len(games),
               note="ok" if data else "no_data")
    return games


def fetch_team_stats_block(session: requests.Session, season: int,
                            group: str, fields: list[tuple[str, str]],
                            label: str) -> dict[str, Any]:
    url = f"{BASE}/teams/stats?season={season}&stats=season&group={group}&teamId={TEAM_ID}"
    status, data = fetch_json(session, url)
    out = extract_team_stat(data, fields)
    log_scrape(f"MLB StatsAPI: team {label}", url, status,
               1 if out else 0, note="ok" if out else "no_data")
    return out


def fetch_player_stats(session: requests.Session, roster: list[dict],
                        season: int) -> dict[str, dict[str, Any]]:
    def task(player: dict) -> tuple[int, dict] | None:
        pid = player["id"]
        if pid is None:
            return None
        position = (player.get("position") or "").upper()
        group = "pitching" if position in PITCHER_POSITION_CODES else "hitting"
        url = f"{BASE}/people/{pid}/stats?stats=season&group={group}&season={season}"
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
        for player in roster:
            if player.get("id") is None:
                continue
            futures.append(ex.submit(task, player))
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
    return out


def main() -> int:
    season = get_season()
    session = make_session()

    team_block, record_block = fetch_team_block(session, season)
    al_east = fetch_standings(session, season, record_block)
    # The /teams hydrate=record block is often sparse mid-season; backfill from
    # the standings entry which carries the full W-L/pct/GB/streak/run_diff set.
    for entry in al_east:
        if entry.get("team_id") == TEAM_ID:
            for key in ("w", "l", "pct", "gb", "streak", "run_diff", "last10"):
                if record_block.get(key) in (None, ""):
                    record_block[key] = entry.get(key)
            break
    roster, injuries = fetch_roster(session, season)
    schedule = fetch_schedule(session)
    team_block["batting"] = fetch_team_stats_block(
        session, season, "hitting", HITTING_FIELDS, "batting")
    team_block["pitching"] = fetch_team_stats_block(
        session, season, "pitching", PITCHING_FIELDS, "pitching")
    team_block["record"] = record_block

    player_stats = fetch_player_stats(session, roster, season)
    log_scrape("MLB StatsAPI: player stats", "<bulk>", 200, len(player_stats), note="ok")

    payload = {
        "updated": now_iso(),
        "season": season,
        "team": team_block,
        "standings_al_east": al_east,
        "roster": roster,
        "injuries": injuries,
        "schedule": schedule,
        "player_stats": player_stats,
    }
    atomic_write(OUTPUT_PATH, payload)
    update_last_updated(payload["updated"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
