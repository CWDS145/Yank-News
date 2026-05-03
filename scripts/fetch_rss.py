"""Fetch RSS feeds, dedupe, filter, write data/articles.json."""
from __future__ import annotations

import html as html_mod
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser as date_parser

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib_scrape_log import log_scrape

REPO_ROOT = Path(__file__).resolve().parents[1]
ARTICLES_PATH = REPO_ROOT / "data" / "articles.json"
DEPTH_PATH = REPO_ROOT / "data" / "depth-chart.json"
LAST_UPDATED_PATH = REPO_ROOT / "data" / "last-updated.json"

USER_AGENT = "YankNewsBot/1.0 (+https://github.com/cwds145/Yank-News)"
TIMEOUT = 20
RETENTION_DAYS = 7
MAX_ITEMS = 100

FEEDS = [
    ("MLB.com Yankees",          "https://www.mlb.com/yankees/feeds/news/rss.xml",       True),
    ("Pinstripe Alley",          "https://www.pinstripealley.com/rss/current.xml",       True),
    ("NY Post Yankees",          "https://nypost.com/tag/new-york-yankees/feed/",        True),
    ("MLB Trade Rumors NYY",     "https://www.mlbtraderumors.com/category/new-york-yankees/feed", True),
    ("ESPN MLB",                 "https://www.espn.com/espn/rss/mlb/news",               False),
    ("CBS Sports MLB",           "https://www.cbssports.com/rss/headlines/mlb/",         False),
    ("The Athletic Yankees Pod", "https://feeds.megaphone.fm/yankeespod",                True),
]

BASE_KEYWORDS = [
    "yankees", "yankee ", "yanks", "bronx bombers", "bronx", "pinstripes", "boone",
]
HARDCODED_FALLBACK = [
    "aaron judge", "juan soto", "gerrit cole", "giancarlo stanton", "anthony volpe",
    "jasson dominguez", "domínguez", "austin wells", "luis gil", "clarke schmidt",
    "carlos rodon", "rodón", "aaron boone",
]
TRACKING_PARAMS_RE = re.compile(r"^(utm_|fbclid|gclid|ref|mc_cid|mc_eid)$", re.IGNORECASE)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def load_keywords() -> list[str]:
    keywords = list(BASE_KEYWORDS)
    try:
        with open(DEPTH_PATH, "r", encoding="utf-8") as f:
            depth = json.load(f)
        players = depth.get("all_players") or []
        if players:
            keywords.extend([p.lower() for p in players if isinstance(p, str)])
            return sorted(set(keywords))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    keywords.extend(HARDCODED_FALLBACK)
    return sorted(set(keywords))


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


def strip_html(s: str) -> str:
    if not s:
        return ""
    try:
        soup = BeautifulSoup(s, "lxml")
        text = soup.get_text(" ", strip=True)
    except Exception:
        text = s
    return html_mod.unescape(text)


def parse_published(entry: Any) -> datetime | None:
    for key in ("published", "updated", "created"):
        val = entry.get(key) if hasattr(entry, "get") else None
        if val:
            try:
                dt = date_parser.parse(val)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except (ValueError, TypeError, OverflowError):
                continue
    for key in ("published_parsed", "updated_parsed"):
        val = entry.get(key) if hasattr(entry, "get") else None
        if val:
            try:
                return datetime(*val[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    return None


def extract_image(entry: Any, summary_html: str) -> str | None:
    media_thumb = entry.get("media_thumbnail") if hasattr(entry, "get") else None
    if media_thumb:
        for m in media_thumb:
            if isinstance(m, dict) and m.get("url"):
                return m["url"]

    media_content = entry.get("media_content") if hasattr(entry, "get") else None
    if media_content:
        for m in media_content:
            if isinstance(m, dict) and m.get("url"):
                medium = (m.get("medium") or m.get("type") or "").lower()
                href = m["url"]
                if "image" in medium or href.lower().endswith(
                    (".jpg", ".jpeg", ".png", ".gif", ".webp")
                ):
                    return href

    enclosures = entry.get("enclosures") if hasattr(entry, "get") else None
    if enclosures:
        for enc in enclosures:
            if isinstance(enc, dict):
                etype = (enc.get("type") or "").lower()
                if etype.startswith("image/") and enc.get("href"):
                    return enc["href"]

    if summary_html:
        try:
            soup = BeautifulSoup(summary_html, "lxml")
            img = soup.find("img")
            if img and img.get("src"):
                return img["src"]
        except Exception:
            pass
    return None


def matches_keywords(text: str, keywords: list[str]) -> bool:
    t = text.lower()
    return any(k in t for k in keywords)


def fetch_feed(url: str) -> tuple[int, str | None]:
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
        return r.status_code, (r.text if r.status_code == 200 else None)
    except requests.RequestException:
        return 0, None


def atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def main() -> int:
    keywords = load_keywords()
    cutoff = now_utc() - timedelta(days=RETENTION_DAYS)

    items: list[dict[str, Any]] = []

    for source, url, yankees_only in FEEDS:
        status, body = fetch_feed(url)
        if status != 200 or body is None:
            log_scrape(source, url, status, 0, note="http_error")
            continue
        try:
            parsed = feedparser.parse(body)
        except Exception as e:
            log_scrape(source, url, status, 0, note=f"parse_error:{type(e).__name__}")
            continue

        kept = 0
        for entry in parsed.entries:
            title = (entry.get("title") or "").strip() if hasattr(entry, "get") else ""
            summary_html = ""
            if hasattr(entry, "get"):
                summary_html = entry.get("summary") or entry.get("description") or ""
                if not summary_html:
                    contents = entry.get("content")
                    if isinstance(contents, list) and contents:
                        first = contents[0]
                        if isinstance(first, dict):
                            summary_html = first.get("value", "") or ""
            summary = strip_html(summary_html)
            link = (entry.get("link") or "").strip() if hasattr(entry, "get") else ""
            if not link or not title:
                continue
            published_dt = parse_published(entry)
            published_iso = (
                published_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                if published_dt else now_iso()
            )
            author = entry.get("author") if hasattr(entry, "get") else None
            if author:
                author = str(author).strip() or None

            image = extract_image(entry, summary_html)

            if not yankees_only:
                if not matches_keywords(title + " " + summary, keywords):
                    continue

            items.append({
                "title": title,
                "summary": summary,
                "source": source,
                "author": author,
                "link": link,
                "published": published_iso,
                "image": image,
            })
            kept += 1

        log_scrape(source, url, status, kept, note="ok")

    by_canon: dict[str, dict[str, Any]] = {}
    for it in items:
        canon = canonicalize_url(it["link"])
        if not canon:
            continue
        if canon not in by_canon:
            by_canon[canon] = it
        else:
            existing = by_canon[canon]
            try:
                cur_dt = date_parser.parse(it["published"])
                ex_dt = date_parser.parse(existing["published"])
                if cur_dt < ex_dt:
                    by_canon[canon] = {**it, "source": existing["source"]}
            except (ValueError, TypeError):
                pass
    deduped = list(by_canon.values())

    fresh: list[dict[str, Any]] = []
    for it in deduped:
        try:
            dt = date_parser.parse(it["published"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            dt = now_utc()
        if dt >= cutoff:
            fresh.append(it)

    def sort_key(it: dict[str, Any]) -> datetime:
        try:
            dt = date_parser.parse(it["published"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, TypeError):
            return datetime.min.replace(tzinfo=timezone.utc)

    fresh.sort(key=sort_key, reverse=True)
    fresh = fresh[:MAX_ITEMS]

    payload = {
        "updated": now_iso(),
        "count": len(fresh),
        "items": fresh,
    }
    atomic_write(ARTICLES_PATH, payload)

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
    lu["news"] = payload["updated"]
    atomic_write(LAST_UPDATED_PATH, lu)

    return 0


if __name__ == "__main__":
    sys.exit(main())
