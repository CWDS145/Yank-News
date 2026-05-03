"""Scrape FanGraphs Yankees team page (1x/24h, ToS-respectful)."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib_scrape_log import log_scrape
from lib_scrape_common import (
    LAST_UPDATED_PATH, REPO_ROOT, TIMEOUT, Timer, atomic_write_json,
    force_enabled, gate_should_skip, get_season, make_session, map_record,
    now_iso, parse_html_table, read_existing,
    update_last_updated_stats_scraped,
)

OUTPUT_PATH = REPO_ROOT / "data" / "stats-fg.json"
SOURCE_LABEL = "FanGraphs Yankees"
URL = "https://www.fangraphs.com/teams/yankees"
SANITY_MIN_BATTERS = 5
SANITY_MIN_PITCHERS = 3

BATTER_OUT = ["name", "G", "PA", "AB", "H", "HR", "R", "RBI", "SB",
              "BB%", "K%", "ISO", "BABIP", "AVG", "OBP", "SLG",
              "wOBA", "wRC+", "BsR", "Off", "Def", "WAR"]
BATTER_MAP = {
    "name":  ["Name", "Player", "PlayerName"],
    "G":     ["G"],
    "PA":    ["PA"],
    "AB":    ["AB"],
    "H":     ["H"],
    "HR":    ["HR"],
    "R":     ["R"],
    "RBI":   ["RBI"],
    "SB":    ["SB"],
    "BB%":   ["BB%", "BB_pct"],
    "K%":    ["K%", "K_pct"],
    "ISO":   ["ISO"],
    "BABIP": ["BABIP"],
    "AVG":   ["AVG"],
    "OBP":   ["OBP"],
    "SLG":   ["SLG"],
    "wOBA":  ["wOBA"],
    "wRC+":  ["wRC+", "wRCp", "wRC_plus"],
    "BsR":   ["BsR"],
    "Off":   ["Off"],
    "Def":   ["Def"],
    "WAR":   ["WAR", "fWAR"],
}

PITCHER_OUT = ["name", "G", "GS", "IP", "K_per_9", "BB_per_9", "HR_per_9",
               "BABIP", "LOB%", "GB%", "HR_FB", "vFA", "ERA", "FIP", "xFIP", "WAR"]
PITCHER_MAP = {
    "name":     ["Name", "Player", "PlayerName"],
    "G":        ["G"],
    "GS":       ["GS"],
    "IP":       ["IP"],
    "K_per_9":  ["K/9", "K_9"],
    "BB_per_9": ["BB/9", "BB_9"],
    "HR_per_9": ["HR/9", "HR_9"],
    "BABIP":    ["BABIP"],
    "LOB%":     ["LOB%", "LOB_pct"],
    "GB%":      ["GB%", "GB_pct"],
    "HR_FB":    ["HR/FB", "HR_FB"],
    "vFA":      ["vFA", "FBv", "vFA (pi)", "vFA (sc)"],
    "ERA":      ["ERA"],
    "FIP":      ["FIP"],
    "xFIP":     ["xFIP"],
    "WAR":      ["WAR", "fWAR"],
}


def _is_batter_table(headers: list[str]) -> bool:
    s = set(headers)
    return "Name" in s and ("AVG" in s or "wOBA" in s) and "ERA" not in s


def _is_pitcher_table(headers: list[str]) -> bool:
    s = set(headers)
    return "Name" in s and ("ERA" in s or "FIP" in s) and "OBP" not in s


def find_player_tables(soup: BeautifulSoup) -> tuple[Any, Any, str]:
    """Find the largest batter table and pitcher table in the page.

    Returns (batter_table, pitcher_table, locator_note) — locator_note
    describes what we found, for the scrape-log breadcrumb on first run.
    """
    batter_table = None
    pitcher_table = None
    batter_rows = pitcher_rows = 0
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        headers = [c.get_text(" ", strip=True) for c in rows[0].find_all(["th", "td"])]
        if _is_batter_table(headers) and len(rows) > batter_rows:
            batter_table, batter_rows = table, len(rows)
        elif _is_pitcher_table(headers) and len(rows) > pitcher_rows:
            pitcher_table, pitcher_rows = table, len(rows)
    note = f"html_tables: batter_rows={batter_rows} pitcher_rows={pitcher_rows}"
    return batter_table, pitcher_table, note


def try_next_data_payload(soup: BeautifulSoup) -> tuple[Any, Any, str] | None:
    """If __NEXT_DATA__ holds the player tables, surface them here.

    The /teams/yankees page currently ships with empty pageProps
    (client-side fetched), so this is a defensive future-proofing path.
    """
    script = soup.find("script", id="__NEXT_DATA__")
    if not script or not script.string:
        return None
    try:
        data = json.loads(script.string)
    except json.JSONDecodeError:
        return None
    page_props = (data.get("props") or {}).get("pageProps") or {}
    if not page_props:
        return None
    # No known SSR shape currently; leave for future probing.
    return None


def fetch(session: requests.Session, url: str) -> tuple[int, str | None]:
    try:
        r = session.get(url, timeout=TIMEOUT)
        return r.status_code, (r.text if r.status_code == 200 else None)
    except requests.RequestException:
        return 0, None


def main() -> int:
    timer = Timer()
    season = get_season()
    existing = read_existing(OUTPUT_PATH)

    if gate_should_skip(existing):
        log_scrape(SOURCE_LABEL, URL, 0, 0, note="skipped_23h_gate")
        return 0

    session = make_session()
    status, html = fetch(session, URL)
    if status != 200 or html is None:
        log_scrape(SOURCE_LABEL, URL, status, 0, note=f"http_{status}")
        return 0

    try:
        soup = BeautifulSoup(html, "lxml")
        result = try_next_data_payload(soup)
        if result is not None:
            batter_table, pitcher_table, locator_note = result
        else:
            batter_table, pitcher_table, locator_note = find_player_tables(soup)

        if batter_table is None and pitcher_table is None:
            log_scrape(SOURCE_LABEL, URL, status, 0, note="selector_miss")
            return 0

        batter_rows, batter_total = parse_html_table(batter_table) if batter_table else ([], None)
        pitcher_rows, pitcher_total = parse_html_table(pitcher_table) if pitcher_table else ([], None)

        batters = [map_record(r, BATTER_MAP, BATTER_OUT) for r in batter_rows]
        pitchers = [map_record(r, PITCHER_MAP, PITCHER_OUT) for r in pitcher_rows]
        team_batting = map_record(batter_total, BATTER_MAP, BATTER_OUT) if batter_total else {}
        team_pitching = map_record(pitcher_total, PITCHER_MAP, PITCHER_OUT) if pitcher_total else {}
    except Exception as e:
        log_scrape(SOURCE_LABEL, URL, status, 0, note=f"exception:{type(e).__name__}")
        return 0

    if len(batters) < SANITY_MIN_BATTERS and len(pitchers) < SANITY_MIN_PITCHERS:
        log_scrape(SOURCE_LABEL, URL, status,
                   len(batters) + len(pitchers),
                   note="insufficient_data")
        return 0

    payload = {
        "updated": now_iso(),
        "source": "FanGraphs",
        "url": URL,
        "season": season,
        "scrape_attempt": {
            "status": "ok",
            "http_status": status,
            "duration_ms": timer.ms(),
            "note": locator_note,
        },
        "team_batting": team_batting,
        "team_pitching": team_pitching,
        "batters": batters,
        "pitchers": pitchers,
    }
    atomic_write_json(OUTPUT_PATH, payload)
    update_last_updated_stats_scraped(payload["updated"])
    log_scrape(SOURCE_LABEL, URL, status,
               len(batters) + len(pitchers),
               note=f"ok | {locator_note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
