# Yank News

Public Yankees news + stats aggregator.

## Live
- Landing: https://cwds145.github.io/Yank-News/
- Dashboard: https://cwds145.github.io/Yank-News/app/

## Architecture
Single-file dashboard (app/index.html) + /data/*.json + 4 GitHub Actions cron workflows. No server, no DB. Hosted free on GitHub Pages.

## Stack
Python 3.12, vanilla JS, GitHub Actions, GitHub Pages.

## Pipeline
- News: 7 RSS feeds (MLB.com, ESPN, NY Post, MLB Trade Rumors, Pinstripe Alley, CBS, Pinstripe Post) — 30 min cron, 7-day retention, 100-article cap, source-aware Yankees filter
- Transactions: CBS Yankees scrape — 60-day window
- Depth chart: FanGraphs → MLB → ESPN fallback chain — daily
- MLB stats: Stats API team/standings/roster/schedule/player_stats — 30 min
- Baseball Savant: CSV pull — 30 min
- MiLB stats: Stats API for 4 affiliates — daily
- FanGraphs Yankees: ToS-flagged daily scrape (23h gate + LKG)
- Baseball-Reference Yankees: ToS-flagged daily scrape (23h gate + LKG)
- Org-Wide Hitters: 67 hitters MLB→Low-A, daily

## Workflows
- .github/workflows/fetch-news.yml — 30 min
- .github/workflows/fetch-stats.yml — 30 min (offset)
- .github/workflows/scrape-flagged.yml — daily (FG + Bref, 23h gate)
- .github/workflows/depth-chart.yml — daily

## ToS notes
Force Refresh buttons on the 3 ToS-flagged sources (FanGraphs, Baseball-Reference, depth chart override) are gated by 'CWS' password (sessionStorage).

## Local dev
Python venv at .venv (3.12.10). To test the UI locally without CORS issues:

    cd D:\Yank-News
    python -m http.server 8765
    open http://localhost:8765/

## JCS Command
Tracked at http://100.105.209.1:8090/ (projects.yank_news in master-config).
