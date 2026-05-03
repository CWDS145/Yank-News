"""Baseball Savant CSV exports for Yankees players. Public, no auth."""
from __future__ import annotations

import csv
import io
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib_scrape_log import log_scrape

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_PATH = REPO_ROOT / "data" / "stats-savant.json"
LAST_UPDATED_PATH = REPO_ROOT / "data" / "last-updated.json"

USER_AGENT = "YankNewsBot/1.0 (+https://github.com/cwds145/Yank-News)"
TIMEOUT = 25
TEAM_ID = 147


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


def fetch_csv(url: str) -> tuple[int, list[dict[str, str]]]:
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
        if r.status_code != 200:
            return r.status_code, []
        # Some Savant endpoints prepend a BOM, which breaks the first quoted
        # column header (`"last_name, first_name"`) and shifts every key.
        body = r.text.lstrip("﻿").strip()
        if not body:
            return r.status_code, []
        reader = csv.DictReader(io.StringIO(body))
        rows = list(reader)
        # The `team=` URL param is honored by expected_statistics + statcast
        # but silently ignored by sprint_speed_leaderboard, which returns the
        # whole league. Filter post-fetch when a team column is present.
        if rows and ("team_id" in rows[0] or "team" in rows[0]):
            rows = [
                r for r in rows
                if (r.get("team_id") or "").strip() == str(TEAM_ID)
                or (r.get("team") or "").strip().upper() == "NYY"
            ]
        return r.status_code, rows
    except requests.RequestException:
        return 0, []


def coerce_num(v: Any) -> int | float | None:
    if v is None:
        return None
    s = str(v).strip()
    if s == "" or s.lower() in {"null", "nan", "n/a"}:
        return None
    try:
        if "." in s or "e" in s.lower():
            return float(s)
        return int(s)
    except (ValueError, TypeError):
        try:
            return float(s)
        except (ValueError, TypeError):
            return None


def get_player_id(row: dict[str, str]) -> int | None:
    for key in ("player_id", "playerId", "MLBAMID", "mlb_id"):
        if key in row:
            v = coerce_num(row[key])
            if v is not None:
                return int(v)
    return None


def get_player_name(row: dict[str, str]) -> str:
    last = (row.get("last_name") or "").strip()
    first = (row.get("first_name") or "").strip()
    if last and first:
        return f"{first} {last}"
    combo = row.get("last_name, first_name") or row.get("name") or ""
    combo = combo.strip().strip('"')
    if not combo:
        return ""
    if "," in combo:
        parts = [p.strip() for p in combo.split(",", 1)]
        if len(parts) == 2 and parts[0] and parts[1]:
            return f"{parts[1]} {parts[0]}"
    return combo


def extract_expected(row: dict[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "pa": coerce_num(row.get("pa")),
        "est_ba": coerce_num(row.get("est_ba")),
        "est_slg": coerce_num(row.get("est_slg")),
        "est_woba": coerce_num(row.get("est_woba")),
        "ba": coerce_num(row.get("ba")),
        "slg": coerce_num(row.get("slg")),
        "woba": coerce_num(row.get("woba")),
    }
    est, actual = out["est_woba"], out["woba"]
    if isinstance(est, (int, float)) and isinstance(actual, (int, float)):
        out["est_woba_minus_woba"] = round(est - actual, 4)
    else:
        out["est_woba_minus_woba"] = None
    return out


def extract_statcast(row: dict[str, str]) -> dict[str, Any]:
    return {
        "attempts": coerce_num(row.get("attempts")),
        "avg_ev": coerce_num(row.get("avg_hit_speed")),
        "max_ev": coerce_num(row.get("max_hit_speed")),
        "avg_la": coerce_num(row.get("avg_hit_angle")),
        "barrel_pct": coerce_num(row.get("brl_percent")),
        "barrel_per_pa": coerce_num(row.get("brl_pa")),
        "ev95plus": coerce_num(row.get("ev95plus")),
        "ev95_pct": coerce_num(row.get("ev95percent")),
        "hardhit_pct": coerce_num(row.get("hardhit_percent")),
    }


def extract_sprint(row: dict[str, str]) -> dict[str, Any]:
    return {
        "sprint_speed": coerce_num(row.get("sprint_speed")),
        "hp_to_1b": coerce_num(row.get("hp_to_1b")),
        "bolts": coerce_num(row.get("bolts")),
    }


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


def main() -> int:
    season = get_season()

    endpoints = [
        ("expected_batting",
         f"https://baseballsavant.mlb.com/leaderboard/expected_statistics"
         f"?type=batter&year={season}&team={TEAM_ID}&csv=true"),
        ("expected_pitching",
         f"https://baseballsavant.mlb.com/leaderboard/expected_statistics"
         f"?type=pitcher&year={season}&team={TEAM_ID}&csv=true"),
        ("statcast_batting",
         f"https://baseballsavant.mlb.com/leaderboard/statcast"
         f"?type=batter&year={season}&team={TEAM_ID}&min=q&csv=true"),
        ("sprint_speed",
         f"https://baseballsavant.mlb.com/sprint_speed_leaderboard"
         f"?year={season}&team={TEAM_ID}&min_year={season}&min_opp=10&min_pa=10&csv=true"),
    ]

    players: dict[int, dict[str, Any]] = {}
    success_count = 0

    for label, url in endpoints:
        status, rows = fetch_csv(url)
        if status != 200:
            log_scrape(f"Baseball Savant: {label}", url, status, 0, note="http_error")
            continue
        if not rows:
            log_scrape(f"Baseball Savant: {label}", url, status, 0, note="empty_csv")
            continue
        success_count += 1
        log_scrape(f"Baseball Savant: {label}", url, status, len(rows), note="ok")

        for row in rows:
            pid = get_player_id(row)
            if pid is None:
                continue
            entry = players.setdefault(pid, {
                "id": pid,
                "name": "",
                "expected_batting": None,
                "expected_pitching": None,
                "statcast_batting": None,
                "sprint_speed": None,
            })
            name = get_player_name(row)
            if name and not entry["name"]:
                entry["name"] = name
            if label == "expected_batting":
                entry["expected_batting"] = extract_expected(row)
            elif label == "expected_pitching":
                entry["expected_pitching"] = extract_expected(row)
            elif label == "statcast_batting":
                entry["statcast_batting"] = extract_statcast(row)
            elif label == "sprint_speed":
                entry["sprint_speed"] = extract_sprint(row)

    if success_count == 0:
        log_scrape("Baseball Savant", "all-endpoints-failed", 0, 0,
                   note="all_savant_endpoints_failed")
        if OUTPUT_PATH.exists():
            try:
                with open(OUTPUT_PATH, "r", encoding="utf-8") as f:
                    existing = json.load(f)
                if existing.get("players"):
                    return 0
            except (json.JSONDecodeError, OSError):
                pass
        atomic_write(OUTPUT_PATH, {
            "updated": now_iso(),
            "season": season,
            "players": [],
        })
        return 0

    payload = {
        "updated": now_iso(),
        "season": season,
        "players": sorted(players.values(), key=lambda p: p.get("name") or ""),
    }
    atomic_write(OUTPUT_PATH, payload)
    update_last_updated(payload["updated"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
