"""Scrape Baseball-Reference Yankees team page (1x/24h, ToS-respectful)."""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup, Comment

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib_scrape_log import log_scrape
from lib_scrape_common import (
    REPO_ROOT, TIMEOUT, Timer, atomic_write_json, gate_should_skip,
    get_season, make_session, now_iso, read_existing,
    update_last_updated_stats_scraped,
)

OUTPUT_PATH = REPO_ROOT / "data" / "stats-bref.json"
SOURCE_LABEL = "Baseball-Reference Yankees"
SANITY_MIN_BATTERS = 5
SANITY_MIN_PITCHERS = 3

# B-Ref column names use a `Player`/`WAR` schema. The output keys below match
# the Step 5 spec; `rWAR` comes from the table's WAR column directly (not from
# the comment-hidden value tables — current standard tables already include it).
BATTER_OUT = ["name", "age", "G", "PA", "AB", "R", "H", "2B", "3B", "HR",
              "RBI", "SB", "BB", "SO", "BA", "OBP", "SLG", "OPS",
              "OPS+", "TB", "rWAR"]
BATTER_HEADER_MAP = {
    "name":  ["Player"],
    "age":   ["Age"],
    "G":     ["G"],
    "PA":    ["PA"],
    "AB":    ["AB"],
    "R":     ["R"],
    "H":     ["H"],
    "2B":    ["2B"],
    "3B":    ["3B"],
    "HR":    ["HR"],
    "RBI":   ["RBI"],
    "SB":    ["SB"],
    "BB":    ["BB"],
    "SO":    ["SO"],
    "BA":    ["BA"],
    "OBP":   ["OBP"],
    "SLG":   ["SLG"],
    "OPS":   ["OPS"],
    "OPS+":  ["OPS+"],
    "TB":    ["TB"],
    "rWAR":  ["WAR"],
}

PITCHER_OUT = ["name", "age", "W", "L", "ERA", "G", "GS", "SV", "IP", "H",
               "R", "ER", "BB", "SO", "HR", "ERA+", "FIP", "WHIP", "rWAR"]
PITCHER_HEADER_MAP = {
    "name": ["Player"],
    "age":  ["Age"],
    "W":    ["W"],
    "L":    ["L"],
    "ERA":  ["ERA"],
    "G":    ["G"],
    "GS":   ["GS"],
    "SV":   ["SV"],
    "IP":   ["IP"],
    "H":    ["H"],
    "R":    ["R"],
    "ER":   ["ER"],
    "BB":   ["BB"],
    "SO":   ["SO"],
    "HR":   ["HR"],
    "ERA+": ["ERA+"],
    "FIP":  ["FIP"],
    "WHIP": ["WHIP"],
    "rWAR": ["WAR"],
}

RECORD_RE = re.compile(r"Record:\s*(\d+)-(\d+)", re.IGNORECASE)
RANK_RE = re.compile(r"(\d+)(?:st|nd|rd|th)\s+place\s+in\s+(\S+)", re.IGNORECASE)
HANDEDNESS_RE = re.compile(r"\s*[\*#\?]+\s*$")
PARENTHETICAL_RE = re.compile(r"\s*\([^)]*\)\s*$")


def clean_name(s: str) -> str:
    """Strip handedness markers (`*`, `#`) and trailing notes like '(10-day IL)'."""
    s = (s or "").strip()
    prev = None
    while prev != s:
        prev = s
        s = PARENTHETICAL_RE.sub("", s).strip()
        s = HANDEDNESS_RE.sub("", s).strip()
    return s


def get_table(soup: BeautifulSoup, table_id: str) -> Any:
    """Return the <table> with the given id, even if buried in an HTML comment."""
    direct = soup.find("table", id=table_id)
    if direct:
        return direct
    for comment in soup.find_all(string=lambda t: isinstance(t, Comment)):
        if table_id in str(comment):
            sub = BeautifulSoup(str(comment), "lxml")
            t = sub.find("table", id=table_id)
            if t:
                return t
    return None


def find_header_row(table: Any) -> Any:
    """B-Ref tables sometimes have an over_header row above the real headers."""
    candidates = []
    for r in table.find_all("tr"):
        ths = r.find_all("th", scope="col")
        if ths and len(ths) >= 5:
            candidates.append(r)
    return candidates[-1] if candidates else table.find("tr")


def parse_table(table: Any, totals_label: str = "Team Totals") -> tuple[list[dict[str, str]], dict[str, str] | None]:
    if table is None:
        return [], None
    header_row = find_header_row(table)
    if not header_row:
        return [], None
    headers = [c.get_text(" ", strip=True) for c in header_row.find_all(["th", "td"])]
    if not headers:
        return [], None

    body = table.find("tbody") or table
    foot = table.find("tfoot")
    body_rows = body.find_all("tr")
    foot_rows = foot.find_all("tr") if foot else []

    players: list[dict[str, str]] = []
    team_total: dict[str, str] | None = None

    def row_to_dict(row: Any) -> dict[str, str] | None:
        cells = row.find_all(["th", "td"])
        if not cells:
            return None
        # Skip B-Ref over_header / repeated-header rows
        if all(c.name == "th" for c in cells):
            return None
        if len(cells) < len(headers):
            cells = cells + [None] * (len(headers) - len(cells))
        elif len(cells) > len(headers):
            cells = cells[:len(headers)]
        return {h: (c.get_text(" ", strip=True) if c is not None else "")
                for h, c in zip(headers, cells)}

    for r in body_rows:
        rec = row_to_dict(r)
        if rec is None:
            continue
        name_val = (rec.get("Player") or "").strip()
        if not name_val:
            continue
        if totals_label.lower() in name_val.lower():
            team_total = rec
        else:
            rec["Player"] = clean_name(name_val)
            players.append(rec)

    if team_total is None:
        for r in foot_rows:
            rec = row_to_dict(r)
            if rec is None:
                continue
            name_val = (rec.get("Player") or "").strip()
            if totals_label.lower() in name_val.lower() or "team" in name_val.lower():
                team_total = rec
                break

    return players, team_total


def map_player(raw: dict[str, str], mapping: dict[str, list[str]],
               output_fields: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for f in output_fields:
        v: Any = None
        for cand in mapping.get(f, [f]):
            if cand in raw:
                val = raw[cand]
                if val not in (None, "", "-", "—"):
                    v = val
                    break
        out[f] = v
    return out


def parse_team_record(soup: BeautifulSoup) -> dict[str, Any]:
    record: dict[str, Any] = {"w": None, "l": None, "div_rank": None, "gb": None}
    meta = soup.find("div", id="meta") or soup.find("div", id="info")
    if not meta:
        return record
    text = meta.get_text(" ", strip=True)

    m = RECORD_RE.search(text)
    if m:
        try:
            record["w"] = int(m.group(1))
            record["l"] = int(m.group(2))
        except ValueError:
            pass
    m = RANK_RE.search(text)
    if m:
        try:
            record["div_rank"] = int(m.group(1))
        except ValueError:
            pass
    return record


def fetch(session: requests.Session, url: str) -> tuple[int, str | None]:
    try:
        r = session.get(url, timeout=TIMEOUT)
        if r.status_code != 200:
            return r.status_code, None
        # B-Ref serves UTF-8 content but its Content-Type header sometimes lacks
        # a charset, so requests defaults to Latin-1 and mojibake-encodes names
        # like "José Caballero". Force UTF-8.
        r.encoding = "utf-8"
        return r.status_code, r.text
    except requests.RequestException:
        return 0, None


def main() -> int:
    timer = Timer()
    season = get_season()
    url = f"https://www.baseball-reference.com/teams/NYY/{season}.shtml"
    existing = read_existing(OUTPUT_PATH)

    if gate_should_skip(existing):
        log_scrape(SOURCE_LABEL, url, 0, 0, note="skipped_23h_gate")
        return 0

    session = make_session()
    status, html = fetch(session, url)
    if status != 200 or html is None:
        log_scrape(SOURCE_LABEL, url, status, 0, note=f"http_{status}")
        return 0

    try:
        soup = BeautifulSoup(html, "lxml")
        bat_table = get_table(soup, "players_standard_batting") or get_table(soup, "team_batting")
        pit_table = get_table(soup, "players_standard_pitching") or get_table(soup, "team_pitching")
        bat_rows, bat_total = parse_table(bat_table)
        pit_rows, pit_total = parse_table(pit_table)
        batters = [map_player(r, BATTER_HEADER_MAP, BATTER_OUT) for r in bat_rows]
        pitchers = [map_player(r, PITCHER_HEADER_MAP, PITCHER_OUT) for r in pit_rows]
        team_batting = map_player(bat_total, BATTER_HEADER_MAP, BATTER_OUT) if bat_total else {}
        team_pitching = map_player(pit_total, PITCHER_HEADER_MAP, PITCHER_OUT) if pit_total else {}
        team_record = parse_team_record(soup)
    except Exception as e:
        log_scrape(SOURCE_LABEL, url, status, 0, note=f"exception:{type(e).__name__}")
        return 0

    if len(batters) < SANITY_MIN_BATTERS and len(pitchers) < SANITY_MIN_PITCHERS:
        log_scrape(SOURCE_LABEL, url, status,
                   len(batters) + len(pitchers), note="insufficient_data")
        return 0

    note = (f"tables: bat_id=players_standard_batting pit_id=players_standard_pitching "
            f"batters={len(batters)} pitchers={len(pitchers)}")
    payload = {
        "updated": now_iso(),
        "source": "Baseball-Reference",
        "url": url,
        "season": season,
        "scrape_attempt": {
            "status": "ok",
            "http_status": status,
            "duration_ms": timer.ms(),
            "note": note,
        },
        "team_record": team_record,
        "team_batting": team_batting,
        "team_pitching": team_pitching,
        "batters": batters,
        "pitchers": pitchers,
    }
    atomic_write_json(OUTPUT_PATH, payload)
    update_last_updated_stats_scraped(payload["updated"])
    log_scrape(SOURCE_LABEL, url, status,
               len(batters) + len(pitchers), note=f"ok | {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
