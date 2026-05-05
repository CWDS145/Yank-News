# Yank News — Claude Code project ground-truth

Public Yankees news + stats aggregator. Lives at `D:\Yank-News` on **ManOAI** (Machine 2). Repo: https://github.com/cwds145/Yank-News (public). Live: https://cwds145.github.io/Yank-News/.

## Architecture in one paragraph
Single-file landing (`index.html`) + single-file dashboard (`app/index.html`) + `data/*.json` + 4 GitHub Actions cron workflows. No server, no DB. Hosted free on GitHub Pages. Python 3.12 (.venv at `D:\Yank-News\.venv`) for the fetchers; vanilla JS for the UI. Mobile responsive at 768px. File-size budget for `app/index.html`: 80 KB.

## Cron workflows (all in `.github/workflows/`)
| Workflow | Schedule | What it does |
|---|---|---|
| `fetch-news.yml` | every 30 min | RSS pulls + CBS transactions |
| `fetch-stats.yml` | every 30 min (`15,45 * * * *`) | MLB Stats API, Savant, MiLB, **Org hitters**, **Org pitchers** |
| `depth-chart.yml` | daily | FG → MLB → ESPN fallback chain |
| `scrape-flagged.yml` | daily | FanGraphs + Baseball-Reference (ToS-flagged, 23h gate + LKG) |

All four use a **3-attempt retry-with-rebase loop on `git push`** (added `1801601`) so concurrent commits during a workflow run don't fail the job.

## Data files (sourced + committed by cron)
- `data/articles.json` — RSS articles (7-day window, 100-cap)
- `data/transactions.json` — CBS-scraped Yankees transactions (60-day window)
- `data/depth-chart.json` — daily depth chart
- `data/stats-mlb.json` — team / standings / roster / schedule / player_stats
- `data/stats-savant.json` — Baseball Savant CSV pull
- `data/stats-milb.json` — 4 affiliates with rosters + stats
- `data/stats-fg.json`, `data/stats-bref.json` — ToS-flagged daily scrapes (23h gate + LKG)
- `data/org-hitters.json` — 67 hitters MLB → Low-A, level history + season + last-10 (window override now driven by browser)
- `data/org-pitchers.json` — 82 pitchers MLB → Low-A, season aggregated across levels
- `data/org-transactions.json` — org-wide level moves
- **`data/contracts.json`** — manually-curated player_id → `{signing_bonus, signing_year, signing_round, aav, total, years, source}`. NOT auto-refreshed; merged into hitters/pitchers at render time. Coverage as of 2026-05-05: 108/149 entries (72%), 82 with confirmed signing bonus (55%). Hitters 67/67, MLB-level pitchers 21/21, MiLB pitchers 20/61.
- `data/scrape-log.json`, `data/last-updated.json` — health/observability

## ToS / password-gated UI
Force Refresh buttons on the 3 ToS-flagged sources (FanGraphs, Baseball-Reference, depth chart override) are gated by **`CWS`** password (sessionStorage). Only present in the dashboard at `/app/`.

## Landing page (`/index.html`) major UI surfaces
1. Hero "YANK NEWS" wordmark over Thurm.png background (gradient overlay)
2. **"We Love Thurm" pill button** (top-center, fixed) — opens fullscreen modal with full Thurm.png; dismiss via close button, backdrop click, or Esc
3. 6-tile nav grid → dashboard tabs
4. **Transactions** section (last 7 days, org-wide)
5. **Org-Wide Hitters** section
   - 13 columns: Player · Pos · Level · L{N} AB/K/BA/OPS · Season AB/K/BA/OPS · **Signing $** · **Current $**
   - Controls: `Last [N] ABs` rolling window (1-500, default 10) · `Min AB` filter · name search · level chips · position chips (All/C/1B/2B/3B/SS/IF/LF/CF/RF/OF/DH)
   - Refresh button (re-reads JSON in-page, NOT a workflow dispatch)
   - Updated timestamp inline next to header
6. **Org-Wide Pitchers** section
   - 24 columns: Player · Pos (SP/RP) · Level · W/L/ERA/G/GS/CG/SHO/SV/SVO/IP/H/R/ER/HR/HB/BB/SO/WHIP/AVG · **Signing $** · **Current $**
   - Controls: `Min IP` filter · name search · role chips (All/SP/RP) · level chips
   - Refresh button (in-page re-read)

## Rolling Last-N-ABs window (browser-side)
The `Last [N] ABs` input on the hitters table calls MLB Stats API `gameLog` directly from the browser (CORS allowed). For each player it fetches `/people/{id}/stats?stats=gameLog&group=hitting&season={year}&sportId={sid}` at every level the player has been at, walks games most-recent-first until ≥N at-bats accumulate, then aggregates raw counts and recomputes AVG/OBP/SLG/OPS exactly. Cached per `(pid, sport_id, season)` in a JS Map so changing N is instant after the first compute.

## "Level" semantics (key invariant)
The Level column on both Hitters and Pitchers tables shows **where the player is actively playing right now**, NOT 40-man membership. Implemented in `build_org_players` / `build_org_pitchers`:
- MLB roster `status_code != "RM"` (Active or any IL flavor) → MLB
- MLB `status_code == "RM"` (optioned/reassigned) → defer to MiLB roster
- Otherwise highest MiLB level the player is rostered at

This means players like Cabrera, Volpe (when optioned), Spencer Jones show their actual AAA assignment, not MLB. MLB-IL'd players doing rehab assignments stay tagged MLB because the MLB-pass runs first.

## Cross-level stat aggregation
Both fetchers query the MLB Stats API at all 5 sport_ids per player (1 MLB, 11 AAA, 12 AA, 13 High-A, 14 Low-A) and aggregate raw counts (AB/H/BB/HBP/SF/TB for hitters; outs/H/ER/BB/SO/HR/HBP/AB-faced for pitchers) → recompute AVG/OBP/SLG/OPS / ERA/WHIP/BAA exactly. This is what lets a recently-promoted player keep his prior MiLB production rolled into season totals.

## Local dev
```
cd D:\Yank-News
python -m http.server 8765
# open http://localhost:8765/
```
The browser-side gameLog fetches go straight to statsapi.mlb.com (CORS open), so localhost works without a proxy.

To dispatch the Stats Refresh workflow without clicking through GitHub:
```
~/bin/gh workflow run fetch-stats.yml --repo CWDS145/Yank-News --ref main
~/bin/gh run watch <run-id> --repo CWDS145/Yank-News --exit-status
```
(`gh` installed at `~/bin/gh`, authenticated as CWDS145.)

## Conventions
- `data/contracts.json` is hand-curated; missing entries render as "—". When adding new players, the lookup is by **MLBAM player_id (string-keyed)**, never name. Schema: `{name, signing_bonus, signing_year, signing_round, aav, total, years, source}`.
- All cron workflows commit on the same branch (`main`) — the retry-rebase loop in each handles cross-workflow push races.
- The `Refresh now` buttons on landing-page sections re-read the local JSON. They do NOT dispatch the workflow. To force a fresh API fetch, dispatch via `gh workflow run` or the Actions UI.
- `.gitattributes` locks LF line endings (CRLF only for `.ps1`); don't fight it.

## What's NOT here (intentionally)
- No backend, no DB, no auth (Pages serves static files only)
- No hitters last-N-days window (replaced by rolling-AB window)
- No pitcher rolling window (only season totals)
- No automated contract scraping (Spotrac blocks bot UAs; Cot's is paywalled; data is hand-curated)
