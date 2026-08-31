"""Org-wide Yankees hitters splits (vs LHP/RHP). Fetches season-to-date
statSplits with sitCodes=vl,vr from StatsAPI for all org hitters across all
levels. Aggregates raw counts across levels, recomputes rates. Writes
data/org-splits.json.
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
DATA = REPO_ROOT / "data"
SPLITS_PATH = DATA / "org-splits.json"
HITTERS_PATH = DATA / "org-hitters.json"

BASE = "https://statsapi.mlb.com/api/v1"
USER_AGENT = "YankNewsBot/1.0 (+https://github.com/cwds145/Yank-News)"
TIMEOUT = 25
WORKERS = 8
SUBMIT_DELAY_S = 0.1
ALL_SPORT_LEVELS = [(1, "MLB"), (11, "AAA"), (12, "AA"), (13, "High-A"), (14, "Low-A")]


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


def to_int(v: Any) -> int | None:
    if v in (None, ""):
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        try:
            return int(float(v))
        except (ValueError, TypeError):
            return None


def fmt_rate(x: float | None) -> str | None:
    if x is None:
        return None
    s = f"{x:.3f}"
    if 0 <= x < 1 and s.startswith("0."):
        s = s[1:]
    return s


def aggregate_blocks(blocks: list[dict | None]) -> dict | None:
    """Sum raw counting stats across per-level blocks; recompute AVG/OBP/SLG/OPS
    from totals (exact, no weighting). Each block must have keys: ab, h, bb, so,
    hbp, sf, tb. Returns the frontend-compatible block shape (ab, k, avg, ops,
    avg_num, ops_num) or None if no input has any AB.
    """
    valid = [b for b in blocks if b]
    if not valid:
        return None
    ab = sum(to_int(b.get("ab")) or 0 for b in valid)
    h = sum(to_int(b.get("h")) or 0 for b in valid)
    bb = sum(to_int(b.get("bb")) or 0 for b in valid)
    so = sum(to_int(b.get("so")) or 0 for b in valid)
    hbp = sum(to_int(b.get("hbp")) or 0 for b in valid)
    sf = sum(to_int(b.get("sf")) or 0 for b in valid)
    tb = sum(to_int(b.get("tb")) or 0 for b in valid)
    if ab == 0 and bb == 0:
        return None
    avg = (h / ab) if ab > 0 else None
    obp_den = ab + bb + hbp + sf
    obp = ((h + bb + hbp) / obp_den) if obp_den > 0 else None
    slg = (tb / ab) if ab > 0 else None
    ops = (obp + slg) if (obp is not None and slg is not None) else None
    return {
        "ab": ab,
        "k": so,
        "avg": fmt_rate(avg),
        "ops": fmt_rate(ops),
        "avg_num": avg,
        "ops_num": ops,
    }


def _parse_split_block(stat: dict) -> dict:
    return {
        "ab": to_int(stat.get("atBats")),
        "h": to_int(stat.get("hits")),
        "bb": to_int(stat.get("baseOnBalls")),
        "so": to_int(stat.get("strikeOuts")),
        "hbp": to_int(stat.get("hitByPitch")),
        "sf": to_int(stat.get("sacFlies")),
        "tb": to_int(stat.get("totalBases")),
        "avg": stat.get("avg"),
        "obp": stat.get("obp"),
        "slg": stat.get("slg"),
        "ops": stat.get("ops"),
    }


def fetch_player_splits(session: requests.Session, player_ids: list[int],
                         season: int) -> dict[tuple[int, int], dict[str, dict | None]]:
    """For every player × every org level (sport_id), fetch statSplits with
    sitCodes=vl,vr (vs-left, vs-right pitcher splits). Returns {(pid, sport_id):
    {"vl": block_or_None, "vr": block_or_None}}. Empty splits → block None.
    """
    targets: list[tuple[int, int, str]] = [
        (pid, sid, lvl) for pid in player_ids for sid, lvl in ALL_SPORT_LEVELS
    ]
    out: dict[tuple[int, int], dict[str, dict | None]] = {}
    by_level_count: dict[str, int] = {}
    by_level_ok: dict[str, int] = {}
    for _, _, lvl in targets:
        by_level_count[lvl] = by_level_count.get(lvl, 0) + 1
        by_level_ok.setdefault(lvl, 0)

    def task(pid: int, sport_id: int, lvl: str
             ) -> tuple[int, int, str, int, dict[str, dict | None] | None]:
        url = (
            f"{BASE}/people/{pid}/stats?stats=statSplits&sitCodes=vl,vr"
            f"&group=hitting&season={season}&sportId={sport_id}"
        )
        status, data = fetch_json(session, url)
        if status != 200 or not data:
            return pid, sport_id, lvl, status, None
        result: dict[str, dict | None] = {"vl": None, "vr": None}
        for stat_set in (data.get("stats") or []):
            for split in stat_set.get("splits") or []:
                split_obj = split.get("split") or {}
                code = (split_obj.get("code") or "").lower()
                desc = (split_obj.get("description") or "").lower()
                side = None
                if code == "vl" or "left" in desc:
                    side = "vl"
                elif code == "vr" or "right" in desc:
                    side = "vr"
                if not side:
                    continue
                block = _parse_split_block(split.get("stat") or {})
                result[side] = block
        return pid, sport_id, lvl, status, result

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = []
        for pid, sid, lvl in targets:
            futures.append(ex.submit(task, pid, sid, lvl))
            time.sleep(SUBMIT_DELAY_S)
        for fut in as_completed(futures):
            try:
                pid, sport_id, lvl, status, result = fut.result()
            except Exception:
                continue
            out[(pid, sport_id)] = result or {"vl": None, "vr": None}
            if status == 200:
                by_level_ok[lvl] = by_level_ok.get(lvl, 0) + 1

    for lvl, count in by_level_count.items():
        ok = by_level_ok.get(lvl, 0)
        log_scrape(
            f"MLB statSplits vl/vr: {lvl}",
            f"{BASE}/people/{{id}}/stats?stats=statSplits&sitCodes=vl,vr&sportId={{sid}}",
            200 if ok else 0, ok,
            note=f"ok={ok}/{count}",
        )
    return out


def main() -> int:
    season = get_season()

    hitters_file = load_json(HITTERS_PATH)
    if not hitters_file:
        log_scrape("OrgSplits", "<no-hitters-cache>", 0, 0, note="missing_org_hitters_json")
        return 0

    player_ids = [h.get("id") for h in (hitters_file.get("hitters") or []) if h.get("id")]
    if not player_ids:
        log_scrape("OrgSplits", "<no-players>", 0, 0, note="no_hitters_in_file")
        return 0

    session = make_session()
    splits_map = fetch_player_splits(session, player_ids, season)

    splits_out: dict[int, dict[str, dict | None]] = {}
    for pid in player_ids:
        vl_blocks: list[dict | None] = []
        vr_blocks: list[dict | None] = []
        for sport_id, _ in ALL_SPORT_LEVELS:
            entry = splits_map.get((pid, sport_id)) or {}
            vl = entry.get("vl")
            vr = entry.get("vr")
            if vl and (to_int(vl.get("ab")) or 0) > 0:
                vl_blocks.append(vl)
            if vr and (to_int(vr.get("ab")) or 0) > 0:
                vr_blocks.append(vr)

        vl_agg = aggregate_blocks(vl_blocks)
        vr_agg = aggregate_blocks(vr_blocks)
        if vl_agg or vr_agg:
            splits_out[pid] = {}
            if vl_agg:
                splits_out[pid]["vl"] = vl_agg
            if vr_agg:
                splits_out[pid]["vr"] = vr_agg

    payload = {
        "updated": now_iso(),
        "season": season,
        "players": splits_out,
    }
    atomic_write(SPLITS_PATH, payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
