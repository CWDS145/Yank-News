"""Shared helpers for ToS-flagged daily scrapes (FG team, FG MiLB, B-Ref).

Centralizes the 23-hour gate, last-known-good preservation, atomic JSON
writes, the User-Agent, the season helper, and the last-updated update.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dateutil import parser as date_parser

REPO_ROOT = Path(__file__).resolve().parents[1]
LAST_UPDATED_PATH = REPO_ROOT / "data" / "last-updated.json"

USER_AGENT = "YankNewsBot/1.0 (+https://github.com/cwds145/Yank-News)"
TIMEOUT = 25
GATE_HOURS = 23


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def get_season() -> int:
    override = os.environ.get("SEASON_OVERRIDE")
    if override:
        try:
            return int(override)
        except ValueError:
            pass
    return now_utc().year


def force_enabled() -> bool:
    return (os.environ.get("FORCE") or "").strip().lower() == "true"


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    return s


def read_existing(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def within_23h(updated_str: str | None) -> bool:
    if not updated_str:
        return False
    try:
        dt = date_parser.parse(updated_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return False
    return (now_utc() - dt).total_seconds() < GATE_HOURS * 3600


def gate_should_skip(existing: dict[str, Any]) -> bool:
    if force_enabled():
        return False
    return within_23h(existing.get("updated"))


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def update_last_updated_stats_scraped(stamp: str) -> None:
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
    lu["stats_scraped"] = stamp
    atomic_write_json(LAST_UPDATED_PATH, lu)


def parse_html_table(table: Any, totals_match: tuple[str, ...] = ("Team Total",)) -> tuple[list[dict[str, str]], dict[str, str] | None]:
    """Return (player_rows, team_total_row) parsed from an HTML <table>."""
    rows = table.find_all("tr")
    if len(rows) < 2:
        return [], None
    headers = [c.get_text(" ", strip=True) for c in rows[0].find_all(["th", "td"])]
    if not headers:
        return [], None
    players: list[dict[str, str]] = []
    team_total: dict[str, str] | None = None
    for row in rows[1:]:
        cells = [c.get_text(" ", strip=True) for c in row.find_all(["th", "td"])]
        if not cells:
            continue
        if len(cells) < len(headers):
            cells = cells + [""] * (len(headers) - len(cells))
        elif len(cells) > len(headers):
            cells = cells[:len(headers)]
        record = dict(zip(headers, cells))
        name_val = (record.get("Name") or record.get("Player") or "").strip()
        if any(m.lower() in name_val.lower() for m in totals_match):
            team_total = record
        else:
            players.append(record)
    return players, team_total


def map_record(raw: dict[str, str], mapping: dict[str, list[str]],
                output_fields: list[str]) -> dict[str, Any]:
    """Re-key a raw header→value dict into the spec output schema."""
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


class Timer:
    """Simple monotonic timer for duration_ms in scrape_attempt."""
    def __init__(self) -> None:
        self.start = time.monotonic()

    def ms(self) -> int:
        return int((time.monotonic() - self.start) * 1000)
