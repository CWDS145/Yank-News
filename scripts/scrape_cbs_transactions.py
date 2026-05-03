"""Scrape CBS Sports Yankees transactions; write data/transactions.json."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup
from dateutil import parser as date_parser

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib_scrape_log import log_scrape

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_PATH = REPO_ROOT / "data" / "transactions.json"

PRIMARY_URL = "https://www.cbssports.com/mlb/teams/NYY/new-york-yankees/transactions/"
FALLBACK_URL = "https://www.cbssports.com/mlb/transactions/"
SOURCE = "CBS Transactions"
USER_AGENT = (
    "Mozilla/5.0 (compatible; YankNewsBot/1.0; "
    "+https://github.com/cwds145/Yank-News)"
)
TIMEOUT = 20
RETENTION_DAYS = 60

TYPE_KEYWORDS = [
    "signed", "released", "optioned", "recalled", "designated", "claimed",
    "purchased", "traded", "activated", "placed", "sent", "selected",
    "outrighted", "transferred", "reinstated", "assigned", "promoted",
    "demoted", "called up",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch(url: str) -> tuple[int, str | None]:
    try:
        r = requests.get(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=TIMEOUT,
            allow_redirects=True,
        )
        return r.status_code, (r.text if r.status_code == 200 else None)
    except requests.RequestException:
        return 0, None


def atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def parse_date(s: str) -> str | None:
    if not s:
        return None
    s = s.strip()
    if not s:
        return None
    try:
        default = datetime(datetime.now().year, 1, 1)
        dt = date_parser.parse(s, default=default, fuzzy=True)
        return dt.strftime("%Y-%m-%d")
    except (ValueError, TypeError, OverflowError):
        return None


def detect_type(description: str) -> str:
    desc_l = description.lower()
    for kw in TYPE_KEYWORDS:
        if re.search(rf"\b{re.escape(kw)}\b", desc_l):
            return kw
    return ""


def extract_player(description: str) -> str:
    desc = re.sub(r"^(The\s+)?(New York\s+)?Yankees\s*", "", description,
                  flags=re.IGNORECASE)
    m = re.search(
        r"\b([A-Z][A-Za-zÀ-ÿ'.\-]+(?:\s+[A-Z][A-Za-zÀ-ÿ'.\-]+){1,3})\b",
        desc,
    )
    return m.group(1).strip() if m else ""


def _player_from_cell(cell: Any) -> str:
    """CBS player cell holds two anchors: short name + full name. Take full name."""
    anchors = cell.find_all("a")
    if len(anchors) >= 2:
        return _norm(anchors[-1].get_text(" ", strip=True))
    if anchors:
        return _norm(anchors[0].get_text(" ", strip=True))
    return _norm(cell.get_text(" ", strip=True))


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def parse_transactions_page(html: str, yankees_only: bool) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "lxml")
    items: list[dict[str, Any]] = []

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        # Detect header so we skip it and learn column order.
        header_cells = rows[0].find_all(["th", "td"])
        header_texts = [c.get_text(" ", strip=True).lower() for c in header_cells]
        date_idx = next((i for i, t in enumerate(header_texts) if "date" in t), 0)
        player_idx = next((i for i, t in enumerate(header_texts) if "player" in t), 1)
        txn_idx = next((i for i, t in enumerate(header_texts)
                        if "transaction" in t or "action" in t),
                       len(header_texts) - 1 if header_texts else 3)
        body_rows = rows[1:] if header_texts and "date" in header_texts[date_idx] else rows

        for row in body_rows:
            cells = row.find_all(["td", "th"])
            if len(cells) <= max(date_idx, player_idx, txn_idx):
                continue
            date_str = parse_date(cells[date_idx].get_text(" ", strip=True))
            if not date_str:
                continue
            player = _player_from_cell(cells[player_idx])
            description = cells[txn_idx].get_text(" ", strip=True)
            if not description:
                continue
            haystack = f"{player} {description}".lower()
            if yankees_only and "yankee" not in haystack:
                continue
            ttype = detect_type(description)
            items.append({
                "date": date_str,
                "type": ttype,
                "player": player,
                "description": description,
            })

    if items:
        return items

    # Fallback: structured list/div rows that some CBS layouts use.
    for li in soup.find_all(["li", "div"]):
        time_tag = li.find("time")
        if not time_tag:
            continue
        date_attr = time_tag.get("datetime") or time_tag.get_text(" ", strip=True)
        date_str = parse_date(date_attr)
        if not date_str:
            continue
        description = li.get_text(" ", strip=True)
        if not description or len(description) < 10:
            continue
        if yankees_only and "yankee" not in description.lower():
            continue
        player = extract_player(description)
        ttype = detect_type(description)
        items.append({
            "date": date_str,
            "type": ttype,
            "player": player,
            "description": description,
        })

    return items


def dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for it in items:
        key = hashlib.sha256(
            f"{it.get('date','')}|{it.get('player','')}|{it.get('description','')}".encode("utf-8")
        ).hexdigest()
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def main() -> int:
    status, html = fetch(PRIMARY_URL)
    used_url = PRIMARY_URL
    yankees_only = False
    if status != 200 or html is None:
        log_scrape(SOURCE, PRIMARY_URL, status, 0, note="http_error_primary")
        status, html = fetch(FALLBACK_URL)
        used_url = FALLBACK_URL
        yankees_only = True
        if status != 200 or html is None:
            log_scrape(SOURCE, FALLBACK_URL, status, 0, note="http_error_fallback")
            return 0

    items = parse_transactions_page(html, yankees_only=yankees_only)
    if not items:
        log_scrape(SOURCE, used_url, status, 0, note="selector miss")
        return 0

    items = dedupe(items)

    cutoff = (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)).date()
    fresh: list[dict[str, Any]] = []
    for it in items:
        try:
            d = datetime.strptime(it["date"], "%Y-%m-%d").date()
            if d >= cutoff:
                fresh.append(it)
        except (ValueError, KeyError):
            continue

    fresh.sort(key=lambda it: it.get("date", ""), reverse=True)

    payload = {
        "updated": now_iso(),
        "count": len(fresh),
        "items": fresh,
    }
    atomic_write(OUTPUT_PATH, payload)
    log_scrape(SOURCE, used_url, status, len(fresh), note="ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
