"""Scrape FanGraphs MiLB Yankees affiliates (1x/24h, ToS-respectful)."""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib_scrape_log import log_scrape
from lib_scrape_common import (
    REPO_ROOT, TIMEOUT, Timer, atomic_write_json, gate_should_skip,
    get_season, make_session, map_record, now_iso, parse_html_table,
    read_existing, update_last_updated_stats_scraped,
)
from scrape_fangraphs import (
    BATTER_MAP, BATTER_OUT, PITCHER_MAP, PITCHER_OUT,
    find_player_tables,
)

OUTPUT_PATH = REPO_ROOT / "data" / "stats-fg-milb.json"

AFFILIATES: list[tuple[str, str, str]] = [
    ("Scranton/WB RailRiders",
     "AAA",
     "https://www.fangraphs.com/teams/scranton-wilkes-barre-railriders"),
    ("Somerset Patriots",
     "AA",
     "https://www.fangraphs.com/teams/somerset-patriots"),
    ("Hudson Valley Renegades",
     "High-A",
     "https://www.fangraphs.com/teams/hudson-valley-renegades"),
    ("Tampa Tarpons",
     "Low-A",
     "https://www.fangraphs.com/teams/tampa-tarpons"),
]
DELAY_BETWEEN_AFFILIATES_S = 2

# Per-affiliate sanity floor — MiLB tables can be sparser early-season.
SANITY_MIN_BATTERS = 3
SANITY_MIN_PITCHERS = 2


def fetch(session: requests.Session, url: str) -> tuple[int, str | None]:
    try:
        r = session.get(url, timeout=TIMEOUT)
        return r.status_code, (r.text if r.status_code == 200 else None)
    except requests.RequestException:
        return 0, None


def resolve_affiliate_url(session: requests.Session, name: str,
                          original_url: str) -> tuple[str, int, str]:
    """If the canonical URL 404s, run one search and pick the closest team page.

    Returns (resolved_url, http_status, note). The note is empty if the
    original URL resolved fine, otherwise describes the resolution.
    """
    status, html = fetch(session, original_url)
    if status == 200 and html:
        return original_url, status, ""

    search_url = f"https://www.fangraphs.com/search.aspx?q={name.replace(' ', '+')}"
    try:
        r = session.get(search_url, timeout=TIMEOUT)
    except requests.RequestException:
        return original_url, status, f"search_failed_{status}"
    if r.status_code != 200:
        return original_url, status, f"search_http_{r.status_code}"

    soup = BeautifulSoup(r.text, "lxml")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/teams/" in href:
            text = a.get_text(" ", strip=True).lower()
            if name.split()[0].lower() in text:
                resolved = href if href.startswith("http") else f"https://www.fangraphs.com{href}"
                return resolved, status, f"resolved_via_search:{resolved}"
    return original_url, status, "search_no_match"


def scrape_affiliate(session: requests.Session, name: str, level: str,
                     url: str) -> dict[str, Any]:
    """Try to scrape one affiliate. Return per-affiliate dict.

    Caller decides whether to merge with prior state on failure.
    """
    resolved_url, _initial_status, resolve_note = resolve_affiliate_url(session, name, url)
    status, html = fetch(session, resolved_url)
    note_prefix = f"{resolve_note}; " if resolve_note else ""

    if status != 200 or html is None:
        log_scrape(f"FanGraphs MiLB: {name}", resolved_url, status, 0,
                   note=f"{note_prefix}http_{status}")
        return {
            "name": name,
            "level": level,
            "url": resolved_url,
            "http_status": status,
            "ok": False,
            "team_batting": {},
            "team_pitching": {},
            "batters": [],
            "pitchers": [],
        }

    try:
        soup = BeautifulSoup(html, "lxml")
        batter_table, pitcher_table, locator_note = find_player_tables(soup)
        batter_rows, batter_total = parse_html_table(batter_table) if batter_table else ([], None)
        pitcher_rows, pitcher_total = parse_html_table(pitcher_table) if pitcher_table else ([], None)
        batters = [map_record(r, BATTER_MAP, BATTER_OUT) for r in batter_rows]
        pitchers = [map_record(r, PITCHER_MAP, PITCHER_OUT) for r in pitcher_rows]
        team_batting = map_record(batter_total, BATTER_MAP, BATTER_OUT) if batter_total else {}
        team_pitching = map_record(pitcher_total, PITCHER_MAP, PITCHER_OUT) if pitcher_total else {}
    except Exception as e:
        log_scrape(f"FanGraphs MiLB: {name}", resolved_url, status, 0,
                   note=f"{note_prefix}exception:{type(e).__name__}")
        return {
            "name": name, "level": level, "url": resolved_url,
            "http_status": status, "ok": False,
            "team_batting": {}, "team_pitching": {},
            "batters": [], "pitchers": [],
        }

    if len(batters) < SANITY_MIN_BATTERS and len(pitchers) < SANITY_MIN_PITCHERS:
        log_scrape(f"FanGraphs MiLB: {name}", resolved_url, status,
                   len(batters) + len(pitchers),
                   note=f"{note_prefix}insufficient_data | {locator_note}")
        return {
            "name": name, "level": level, "url": resolved_url,
            "http_status": status, "ok": False,
            "team_batting": {}, "team_pitching": {},
            "batters": [], "pitchers": [],
        }

    log_scrape(f"FanGraphs MiLB: {name}", resolved_url, status,
               len(batters) + len(pitchers),
               note=f"{note_prefix}ok | {locator_note}")
    return {
        "name": name,
        "level": level,
        "url": resolved_url,
        "http_status": status,
        "ok": True,
        "team_batting": team_batting,
        "team_pitching": team_pitching,
        "batters": batters,
        "pitchers": pitchers,
    }


def main() -> int:
    timer = Timer()
    season = get_season()
    existing = read_existing(OUTPUT_PATH)

    if gate_should_skip(existing):
        log_scrape("FanGraphs MiLB", "<all>", 0, 0, note="skipped_23h_gate")
        return 0

    existing_affiliates = {
        a.get("name"): a for a in (existing.get("affiliates") or [])
        if isinstance(a, dict) and a.get("name")
    }

    session = make_session()
    results: list[dict[str, Any]] = []
    for i, (name, level, url) in enumerate(AFFILIATES):
        if i > 0:
            time.sleep(DELAY_BETWEEN_AFFILIATES_S)
        scraped = scrape_affiliate(session, name, level, url)
        ok = scraped.pop("ok")
        if ok:
            results.append(scraped)
        else:
            # Per-affiliate LKG: keep previous block if we have one.
            prev = existing_affiliates.get(name)
            if prev:
                preserved = dict(prev)
                preserved["http_status"] = scraped["http_status"]
                results.append(preserved)
            else:
                # No prior data — record the empty attempt so the file always
                # contains 4 affiliates and the dashboard can show them.
                results.append(scraped)

    success_count = sum(1 for (n, _, _), r in zip(AFFILIATES, results)
                        if r.get("batters") or r.get("pitchers"))
    fresh_count = sum(1 for (n, _, _), r in zip(AFFILIATES, results)
                      if r.get("http_status") == 200 and (r.get("batters") or r.get("pitchers")))

    if fresh_count == 0:
        # Per spec: full LKG preservation — do NOT overwrite, regardless of
        # whether the existing file has prior content. The scaffolded empty
        # file is acceptable LKG state.
        log_scrape("FanGraphs MiLB", "<all>", 0, 0, note="all_failed_kept_lkg")
        return 0

    if fresh_count == len(AFFILIATES):
        status_str = "ok"
    else:
        status_str = "partial"

    payload = {
        "updated": now_iso(),
        "source": "FanGraphs-MiLB",
        "season": season,
        "scrape_attempt": {
            "status": status_str,
            "http_status": 200 if fresh_count else 0,
            "duration_ms": timer.ms(),
            "note": f"fresh={fresh_count}/{len(AFFILIATES)} ok_total={success_count}",
        },
        "affiliates": results,
    }
    atomic_write_json(OUTPUT_PATH, payload)
    if fresh_count > 0:
        update_last_updated_stats_scraped(payload["updated"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
