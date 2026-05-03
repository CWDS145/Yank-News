"""Shared scrape logger. Public: log_scrape(source, url, status, items, note='')."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
LOG_PATH = REPO_ROOT / "data" / "scrape-log.json"
MAX_ENTRIES = 500


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_log() -> dict[str, Any]:
    try:
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
            return {"entries": []}
        return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"entries": []}


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def log_scrape(source: str, url: str, status: int, items: int, note: str = "") -> None:
    log = _read_log()
    log["entries"].append({
        "timestamp": _now_iso(),
        "source": source,
        "url": url,
        "status": int(status) if status is not None else 0,
        "items": int(items) if items is not None else 0,
        "note": note or "",
    })
    if len(log["entries"]) > MAX_ENTRIES:
        log["entries"] = log["entries"][-MAX_ENTRIES:]
    _atomic_write(LOG_PATH, log)
