# ASX Monitor

A daily agent that scrapes ASX company announcements from marketindex.com.au, detects new items, summarises them with Claude Sonnet 4.6, and emails a styled report.

## What you get

A daily email with:

- **Overview table** — every company with new announcements, ticker, count, timestamp of the most recent
- **Per-company sections** — the full list of new announcements with links to PDFs, plus a Claude-written **Assessment** / **Notable items** / **Watch for** brief
- **Quiet companies footer** — one-line list of monitored companies with no new announcements

Rendered as HTML for Gmail, with plain-text markdown fallback.

## Setup (cloud / GitHub Actions)

1. Push this repo to GitHub
2. In `Settings → Secrets and variables → Actions → New repository secret`, add:

   | Name | Example |
   |---|---|
   | `ANTHROPIC_API_KEY` | `sk-ant-api03-...` |
   | `EMAIL_TO` | `you@example.com` |
   | `EMAIL_FROM` | `your-sender@gmail.com` |
   | `SMTP_HOST` | `smtp.gmail.com` |
   | `SMTP_PORT` | `587` |
   | `SMTP_USER` | `your-sender@gmail.com` |
   | `SMTP_PASSWORD` | Gmail App Password ([generate](https://myaccount.google.com/apppasswords)) |

3. Go to the **Actions** tab → `ASX Monitor — daily run` → **Run workflow** → set mode to `seed`. This baselines current announcements as "seen" without burning Claude calls.

4. Done. The scheduled cron (`22:00 UTC` and `08:30 UTC` = `08:00` and `18:30` AEST) takes over.

## Setup (local)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

cp .env.example .env       # edit and fill in your keys
python monitor.py --seed   # baseline current announcements
python monitor.py          # produce a real report and email it
```

For scheduled runs on macOS, see the `launchd` plist in `~/Library/LaunchAgents/com.anirudh.asx-monitor.plist` and the `run.sh` wrapper.

## Adjusting the watchlist

Edit `companies.json`. After adding a ticker, run a seed pass (or trigger the `seed` workflow_dispatch) so the first scheduled run doesn't report 30 historical items.

```json
{
  "companies": [
    {"ticker": "MQG", "name": "Macquarie Group"},
    {"ticker": "CSL", "name": "CSL Limited"}
  ]
}
```

## How it works

1. **Scrape** — Playwright + `playwright-stealth` fetches the announcements table for each ticker. Cloudflare bot protection is bypassed using a fresh browser context per company plus stealth fingerprinting.
2. **Diff** — `seen_announcements.json` (committed to the repo on cloud runs) tracks announcement IDs. New items = items not in the seen set.
3. **Summarise** — Claude Sonnet 4.6 writes a focused Assessment / Notable items / Watch for brief, told explicitly to skip routine items.
4. **Deliver** — markdown report saved to `reports/`, then emailed as multipart text/HTML.

## Repo layout

```
monitor.py                  # the whole pipeline (single file)
companies.json              # watchlist
seen_announcements.json     # persistent state (committed by CI)
reports/                    # daily markdown reports
.github/workflows/daily.yml # cron + workflow_dispatch
requirements.txt
CLAUDE.md                   # design notes for Claude sessions
```

## Cost

~$0.01–$0.05/day in Sonnet tokens, depending on announcement volume. Free runtime on GitHub Actions for the cron jobs.

## Limitations

- Cloudflare can update fingerprinting at any time. If scraping starts returning 0 rows, the most likely cause is that stealth no longer evades detection; check `playwright-stealth` for updates.
- GitHub Actions IPs are well-known to Cloudflare and may be challenged more aggressively than residential IPs. If cloud runs fail consistently, the project falls back to: (a) running locally via launchd, (b) using a paid proxy service, or (c) a self-hosted GitHub Actions runner.
