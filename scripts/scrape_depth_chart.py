"""Yankees depth chart scraper with FanGraphs -> MLB.com -> ESPN fallback."""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib_scrape_log import log_scrape

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_PATH = REPO_ROOT / "data" / "depth-chart.json"

USER_AGENT = (
    "Mozilla/5.0 (compatible; YankNewsBot/1.0; "
    "+https://github.com/cwds145/Yank-News)"
)
TIMEOUT = 25
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

CANONICAL_POSITIONS = ["C", "1B", "2B", "3B", "SS", "LF", "CF", "RF", "DH", "SP", "RP"]
MIN_DISTINCT_NAMES = 15

POSITION_ALIASES = {
    "C": "C", "CATCHER": "C",
    "1B": "1B", "FIRST BASE": "1B", "FIRST": "1B", "FIRST BASEMAN": "1B",
    "2B": "2B", "SECOND BASE": "2B", "SECOND": "2B", "SECOND BASEMAN": "2B",
    "3B": "3B", "THIRD BASE": "3B", "THIRD": "3B", "THIRD BASEMAN": "3B",
    "SS": "SS", "SHORTSTOP": "SS",
    "LF": "LF", "LEFT FIELD": "LF", "LEFT FIELDER": "LF", "LEFT": "LF",
    "CF": "CF", "CENTER FIELD": "CF", "CENTER FIELDER": "CF", "CENTER": "CF",
    "CENTERFIELD": "CF",
    "RF": "RF", "RIGHT FIELD": "RF", "RIGHT FIELDER": "RF", "RIGHT": "RF",
    "DH": "DH", "DESIGNATED HITTER": "DH",
    "P": "SP", "PITCHER": "SP",
    "SP": "SP", "STARTING PITCHER": "SP", "STARTING PITCHERS": "SP",
    "ROTATION": "SP", "STARTER": "SP", "STARTERS": "SP",
    "RP": "RP", "RELIEF PITCHER": "RP", "RELIEF PITCHERS": "RP",
    "BULLPEN": "RP", "RELIEF": "RP", "RELIEVER": "RP", "RELIEVERS": "RP",
    "CL": "RP", "CLOSER": "RP", "SETUP": "RP", "SETUP MAN": "RP",
    "MIDDLE RELIEF": "RP", "LONG RELIEF": "RP",
}

NUMBERED_POS_RE = re.compile(r"^(SP|RP|LHP|RHP|SU|CL)\d+$")

FG_KEEP_TYPES = {
    "mlb-sl", "mlb-sp", "mlb-bp", "mlb-bn",
    "il-sp", "il-rp", "il-pp",
}
FG_TYPE_PRIORITY = {
    "mlb-sl": 0, "mlb-sp": 0, "mlb-bp": 0,
    "mlb-bn": 1,
    "il-sp": 2, "il-rp": 2, "il-pp": 2,
}


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_position(label: str) -> str | None:
    if not label:
        return None
    norm = re.sub(r"[^A-Z0-9 ]", "", label.upper().strip())
    norm = re.sub(r"\s+", " ", norm).strip()
    if not norm:
        return None
    if NUMBERED_POS_RE.match(norm):
        prefix = re.match(r"^([A-Z]+)\d+$", norm).group(1)
        if prefix == "SP":
            return "SP"
        return "RP"
    return POSITION_ALIASES.get(norm)


def clean_name(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"\([^)]*\)", "", s)
    s = re.sub(r"\[[^\]]*\]", "", s)
    s = re.sub(r"^\s*#?\d{1,3}\s+", "", s)
    s = re.sub(r"\s+#?\d{1,3}\s*$", "", s)
    s = re.sub(
        r"\s+\b(SP|RP|LHP|RHP|IL\d*|DTD|GTD|OUT|DAY-TO-DAY|10-DAY|15-DAY|60-DAY)\b.*$",
        "",
        s,
        flags=re.IGNORECASE,
    )
    s = re.sub(r"\s+", " ", s).strip()
    return s


def is_plausible_name(s: str) -> bool:
    if not s or len(s) < 3 or len(s) > 60:
        return False
    if s in {"-", "—", "–"}:
        return False
    if not re.search(r"[A-Za-zÀ-ÿ]", s):
        return False
    parts = s.split()
    if len(parts) < 2:
        return False
    alpha = [p for p in parts if re.search(r"[A-Za-zÀ-ÿ]", p)]
    if len(alpha) < 2:
        return False
    lower = s.lower()
    bad_words = {
        "starter", "rotation", "bullpen", "depth chart", "yankees",
        "lineup", "injured", "sign in", "subscribe", "newsletter",
    }
    if any(b in lower for b in bad_words):
        return False
    return True


def fetch(url: str) -> tuple[int, str | None]:
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        return r.status_code, (r.text if r.status_code == 200 else None)
    except requests.RequestException:
        return 0, None


def _to_ranked(positions: dict[str, list[str]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {pos: [] for pos in CANONICAL_POSITIONS}
    for pos, names in positions.items():
        if pos not in out:
            continue
        seen: set[str] = set()
        for name in names:
            if name in seen:
                continue
            seen.add(name)
            out[pos].append({"name": name, "rank": len(out[pos]) + 1})
    return out


def parse_fangraphs(html: str) -> dict[str, list[dict[str, Any]]]:
    """FanGraphs Roster Resource embeds the depth chart in __NEXT_DATA__ JSON.

    The page is React-rendered; the canonical roster lives inside
    `props.pageProps.dehydratedState.queries[0].state.data.dataRoster`,
    a flat list of player records typed by level (mlb-sl/sp/bp/bn, il-*, aaa-*, etc.).
    We keep MLB-level + IL types and bucket by canonical position.
    """
    soup = BeautifulSoup(html, "lxml")
    script = soup.find("script", id="__NEXT_DATA__")
    if not script or not script.string:
        return {pos: [] for pos in CANONICAL_POSITIONS}
    try:
        data = json.loads(script.string)
        roster = (
            data["props"]["pageProps"]["dehydratedState"]["queries"][0]
            ["state"]["data"]["dataRoster"]
        )
    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
        return {pos: [] for pos in CANONICAL_POSITIONS}
    if not isinstance(roster, list):
        return {pos: [] for pos in CANONICAL_POSITIONS}

    bucketed: dict[str, list[tuple[int, int, str]]] = {}
    for entry in roster:
        if not isinstance(entry, dict):
            continue
        rtype = entry.get("type", "")
        if rtype not in FG_KEEP_TYPES:
            continue
        raw_pos = entry.get("position") or ""
        canonical: str | None = None
        for token in raw_pos.split("/"):
            canonical = normalize_position(token)
            if canonical:
                break
        if not canonical:
            continue
        name = clean_name(entry.get("player") or "")
        if not is_plausible_name(name):
            continue
        try:
            role = int(entry.get("role") or 99)
        except (ValueError, TypeError):
            role = 99
        priority = FG_TYPE_PRIORITY.get(rtype, 9)
        bucketed.setdefault(canonical, []).append((priority, role, name))

    positions: dict[str, list[str]] = {}
    for pos, items in bucketed.items():
        items.sort(key=lambda x: (x[0], x[1]))
        positions[pos] = [name for _, _, name in items]
    return _to_ranked(positions)


def parse_table_depth_chart(soup: BeautifulSoup) -> dict[str, list[dict[str, Any]]]:
    """Generic single-table extractor (label in first cell, names in rest)."""
    positions: dict[str, list[str]] = {}
    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            cells = row.find_all(["td", "th"])
            if not cells:
                continue
            pos = normalize_position(cells[0].get_text(" ", strip=True))
            if not pos:
                continue
            depth: list[str] = positions.setdefault(pos, [])
            for cell in cells[1:]:
                a_tags = cell.find_all("a")
                if a_tags:
                    for a in a_tags:
                        name = clean_name(a.get_text(" ", strip=True))
                        if is_plausible_name(name) and name not in depth:
                            depth.append(name)
                else:
                    name = clean_name(cell.get_text(" ", strip=True))
                    if is_plausible_name(name) and name not in depth:
                        depth.append(name)
    return _to_ranked(positions)


def parse_mlb_com(html: str) -> dict[str, list[dict[str, Any]]]:
    soup = BeautifulSoup(html, "lxml")
    yankees_root = None
    for tag in soup.find_all(attrs={"data-team": True}):
        team_attr = tag.get("data-team", "").lower()
        if "yankees" in team_attr or team_attr == "nyy":
            yankees_root = tag
            break
    if yankees_root is None:
        for tag in soup.find_all(attrs={"id": True}):
            tid = tag.get("id", "").lower()
            if "yankees" in tid or "nyy" in tid:
                yankees_root = tag
                break
    if yankees_root is None:
        for h in soup.find_all(["h1", "h2", "h3", "h4"]):
            if "yankees" in h.get_text(" ", strip=True).lower():
                yankees_root = h.parent
                break
    if yankees_root is None:
        return {pos: [] for pos in CANONICAL_POSITIONS}
    inner = BeautifulSoup(str(yankees_root), "lxml")
    return parse_table_depth_chart(inner)


def parse_espn(html: str) -> dict[str, list[dict[str, Any]]]:
    """ESPN pairs Table--fixed-left (labels) with the next sibling Table (data).

    Each `Table--fixed-left` table holds one column of position labels
    (P, RP, CL, C, 1B, 2B, ...). The immediately-following table holds the
    same number of rows with player-name cells (Starter | 2nd | 3rd | ...).
    Pair them by row index.
    """
    soup = BeautifulSoup(html, "lxml")
    positions: dict[str, list[str]] = {}

    label_tables = soup.find_all(
        "table", class_=lambda c: c and "Table--fixed-left" in c
    )
    for label_table in label_tables:
        data_table = None
        sibling = label_table
        while True:
            sibling = sibling.find_next("table")
            if sibling is None:
                break
            classes = sibling.get("class") or []
            if "Table--fixed-left" not in classes:
                data_table = sibling
                break
        if data_table is None:
            continue
        label_rows = label_table.find_all("tr")
        data_rows = data_table.find_all("tr")
        for label_row, data_row in zip(label_rows, data_rows):
            label_cells = label_row.find_all(["td", "th"])
            if not label_cells:
                continue
            pos = normalize_position(label_cells[0].get_text(" ", strip=True))
            if not pos:
                continue
            depth: list[str] = positions.setdefault(pos, [])
            for cell in data_row.find_all(["td", "th"]):
                a = cell.find("a")
                raw = a.get_text(" ", strip=True) if a else cell.get_text(" ", strip=True)
                name = clean_name(raw)
                if is_plausible_name(name) and name not in depth:
                    depth.append(name)

    if any(positions.values()):
        return _to_ranked(positions)

    return parse_table_depth_chart(soup)


def count_distinct(positions: dict[str, list[dict[str, Any]]]) -> int:
    seen: set[str] = set()
    for plist in positions.values():
        for entry in plist:
            seen.add(entry["name"])
    return len(seen)


def collect_all_players(positions: dict[str, list[dict[str, Any]]]) -> list[str]:
    s: set[str] = set()
    for plist in positions.values():
        for entry in plist:
            s.add(entry["name"])
    return sorted(s)


def atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def main() -> int:
    sources = [
        ("FanGraphs",
         "https://www.fangraphs.com/roster-resource/depth-charts/yankees",
         parse_fangraphs),
        ("MLB.com",
         "https://www.mlb.com/depth-charts",
         parse_mlb_com),
        ("ESPN",
         "https://www.espn.com/mlb/team/depth/_/name/nyy/new-york-yankees",
         parse_espn),
    ]

    chosen_source: str | None = None
    final_positions: dict[str, list[dict[str, Any]]] | None = None

    for source_name, url, parser in sources:
        status, html = fetch(url)
        if status != 200 or not html:
            log_scrape(source_name, url, status, 0, note="http_error")
            continue
        try:
            positions = parser(html)
            distinct = count_distinct(positions)
            if distinct == 0:
                log_scrape(source_name, url, status, 0, note="selector_miss")
                continue
            if distinct < MIN_DISTINCT_NAMES:
                log_scrape(source_name, url, status, distinct, note="insufficient_names")
                continue
            log_scrape(source_name, url, status, distinct, note="ok")
            chosen_source = source_name
            final_positions = positions
            break
        except Exception as e:
            log_scrape(source_name, url, status, 0, note=f"exception:{type(e).__name__}")
            continue

    if final_positions is None:
        if OUTPUT_PATH.exists():
            try:
                with open(OUTPUT_PATH, "r", encoding="utf-8") as f:
                    existing = json.load(f)
                if existing.get("all_players"):
                    log_scrape(
                        "DepthChart", "all-sources-failed", 0, 0,
                        note="all_failed_kept_existing",
                    )
                    return 0
            except (json.JSONDecodeError, OSError):
                pass
        payload = {
            "updated": now_iso(),
            "source_used": None,
            "positions": {pos: [] for pos in CANONICAL_POSITIONS},
            "all_players": [],
        }
        atomic_write(OUTPUT_PATH, payload)
        log_scrape("DepthChart", "all-sources-failed", 0, 0, note="wrote_empty")
        return 0

    full = {pos: final_positions.get(pos, []) for pos in CANONICAL_POSITIONS}
    payload = {
        "updated": now_iso(),
        "source_used": chosen_source,
        "positions": full,
        "all_players": collect_all_players(full),
    }
    atomic_write(OUTPUT_PATH, payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
