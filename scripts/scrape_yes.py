"""Scrape YES Network Yankees article cards. Merges into data/articles.json."""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode

import requests
from bs4 import BeautifulSoup
from dateutil import parser as date_parser

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib_scrape_log import log_scrape

REPO_ROOT = Path(__file__).resolve().parents[1]
ARTICLES_PATH = REPO_ROOT / "data" / "articles.json"

URL = "https://www.yesnetwork.com/yankees"
SOURCE = "YES Network"
USER_AGENT = (
    "Mozilla/5.0 (compatible; YankNewsBot/1.0; "
    "+https://github.com/cwds145/Yank-News)"
)
TIMEOUT = 20
MAX_CARDS = 30
RETENTION_DAYS = 7
MAX_ITEMS_TOTAL = 100

TRACKING_PARAMS_RE = re.compile(r"^(utm_|fbclid|gclid|ref|mc_cid|mc_eid)$", re.IGNORECASE)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def canonicalize_url(url: str) -> str:
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        clean_qs = [
            (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
            if not TRACKING_PARAMS_RE.match(k)
        ]
        new_query = urlencode(clean_qs)
        path = parsed.path.rstrip("/") if parsed.path != "/" else "/"
        return urlunparse((
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            path,
            parsed.params,
            new_query,
            "",
        ))
    except ValueError:
        return url


def atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


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
        )
        return r.status_code, (r.text if r.status_code == 200 else None)
    except requests.RequestException:
        return 0, None


def parse_cards(html: str, base_url: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "lxml")
    items: list[dict[str, Any]] = []
    seen_links: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith("#") or href.lower().startswith("javascript:"):
            continue
        absolute = urljoin(base_url, href)
        parsed = urlparse(absolute)
        if "yesnetwork.com" not in parsed.netloc.lower():
            continue
        # Yankees-only: the page also surfaces Brooklyn Nets cards in nav/feeds.
        if "yankees" not in parsed.path.lower():
            continue
        if absolute in seen_links:
            continue

        title = a.get_text(" ", strip=True)
        heading = a.find(["h1", "h2", "h3", "h4"])
        if heading:
            ht = heading.get_text(" ", strip=True)
            if len(ht) > len(title):
                title = ht

        if len(title) < 15 or " " not in title:
            continue

        container = a
        for _ in range(4):
            if container.parent is None:
                break
            container = container.parent

        image = None
        img = a.find("img")
        if img is None and hasattr(container, "find"):
            img = container.find("img")
        if img:
            src = (
                img.get("src")
                or img.get("data-src")
                or img.get("data-lazy-src")
                or img.get("data-original")
            )
            if src:
                image = urljoin(base_url, src)

        summary = ""
        p = container.find("p") if hasattr(container, "find") else None
        if p:
            summary = p.get_text(" ", strip=True)

        published = None
        t = container.find("time") if hasattr(container, "find") else None
        if t:
            dt_attr = t.get("datetime") or t.get_text(" ", strip=True)
            try:
                dt = date_parser.parse(dt_attr)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                published = dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            except (ValueError, TypeError):
                published = None

        seen_links.add(absolute)
        items.append({
            "title": title,
            "summary": summary,
            "source": SOURCE,
            "author": None,
            "link": absolute,
            "published": published or now_iso(),
            "image": image,
        })
        if len(items) >= MAX_CARDS:
            break

    return items


def load_articles() -> dict[str, Any]:
    if ARTICLES_PATH.exists():
        try:
            with open(ARTICLES_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("items"), list):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {"updated": now_iso(), "count": 0, "items": []}


def main() -> int:
    status, html = fetch(URL)
    if status != 200 or html is None:
        log_scrape(SOURCE, URL, status, 0, note="http_error")
        return 0

    new_items = parse_cards(html, URL)
    if not new_items:
        log_scrape(SOURCE, URL, status, 0, note="selector miss")
        return 0

    log_scrape(SOURCE, URL, status, len(new_items), note="ok")

    existing = load_articles()
    merged = list(existing.get("items", [])) + new_items

    by_canon: dict[str, dict[str, Any]] = {}
    for it in merged:
        canon = canonicalize_url(it.get("link", ""))
        if not canon:
            continue
        if canon not in by_canon:
            by_canon[canon] = it
        else:
            try:
                cur = date_parser.parse(it.get("published") or "")
                ex = date_parser.parse(by_canon[canon].get("published") or "")
                if cur < ex:
                    by_canon[canon] = {**it, "source": by_canon[canon]["source"]}
            except (ValueError, TypeError):
                pass
    deduped = list(by_canon.values())

    cutoff = now_utc() - timedelta(days=RETENTION_DAYS)
    fresh: list[dict[str, Any]] = []
    for it in deduped:
        try:
            dt = date_parser.parse(it.get("published") or now_iso())
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            dt = now_utc()
        if dt >= cutoff:
            fresh.append(it)

    def sort_key(it: dict[str, Any]) -> datetime:
        try:
            dt = date_parser.parse(it.get("published") or "")
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, TypeError):
            return datetime.min.replace(tzinfo=timezone.utc)

    fresh.sort(key=sort_key, reverse=True)
    fresh = fresh[:MAX_ITEMS_TOTAL]

    payload = {
        "updated": now_iso(),
        "count": len(fresh),
        "items": fresh,
    }
    atomic_write(ARTICLES_PATH, payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
