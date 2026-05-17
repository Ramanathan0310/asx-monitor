# ASX Monitor — project notes for Claude

A daily agent that scrapes ASX announcements from marketindex.com.au for a configured list of companies, detects new items since the last run, summarises them with Claude Sonnet 4.6, and emails a markdown+HTML report.

## Key files

- **`monitor.py`** — the entire pipeline. Single-file Python script. Sections:
  - `_load_dotenv()` — reads `.env`; overwrites empty env vars (some shells/CI export `ANTHROPIC_API_KEY=""` and SDKs treat that as set-but-empty, which breaks them)
  - `fetch_announcements(page, ticker)` — Playwright scraping with `wait_until="domcontentloaded"` then a 3s timeout (this site never reaches `networkidle` due to long-running analytics connections)
  - `find_new()` / `ann_id()` — diff against `seen_announcements.json`. ID is md5(date|time|title) for robust dedup
  - `summarize()` — Claude Sonnet 4.6 call. Prompt enforces exact structure: **Assessment** / **Notable items** / **Watch for**. Tells Claude NOT to restate the list (we render it separately) and NOT to emit `#` or `##` headings (we own the heading hierarchy)
  - `build_report()` — markdown layout: header + stats banner + overview table + per-company sections + quiet companies footer
  - `send_email()` — multipart text/plain (raw markdown) + text/html (rendered via python-markdown). Gmail renders the HTML version
  - `run(seed_only)` — main loop, fresh browser context per company

- **`companies.json`** — the watchlist. Edit this to add/remove tickers.
- **`seen_announcements.json`** — persistent state mapping ticker → list of seen announcement IDs (capped at 500/company). MUST persist across runs.
- **`reports/`** — daily markdown reports, one per day.
- **`.github/workflows/daily.yml`** — cron 22:00 UTC + 08:30 UTC = 08:00 + 18:30 AEST. Commits state + report back to repo.
- **`requirements.txt`** — pinned deps.

## Critical non-obvious behaviours

1. **Cloudflare bot protection.** marketindex.com.au is behind Cloudflare Turnstile. Without mitigations every navigation past the first gets a "Just a moment..." challenge.
   - **Mitigation 1:** `playwright_stealth.Stealth().apply_stealth_sync(context)` — patches navigator fingerprinting
   - **Mitigation 2:** **Fresh `browser.new_context()` per company**, not a shared page. Same-session navigation across many pages is flagged as bot-like; each fresh context looks like a new visitor and gets through.
   - **Mitigation 3:** Jittered 2–5 s sleep between companies.
   - Do NOT reuse the page across companies. Do NOT use `wait_until="networkidle"` — long-poll analytics keep the network "busy" forever.

2. **Wait strategy.** Use `wait_until="domcontentloaded"` + `page.wait_for_timeout(3000)`. Don't use `wait_for_selector("table tbody tr")` — even with `state="attached"` it's flaky on this site.

3. **Page structure.** Each announcement row is a `<tr>` with 5 cells: `[date, time, title, pages, PDF link]`. Title is `cells[2]`, not `cells[1]`. PDF URL is in an `<a>` inside the row (look for `href*='announcement'`).

4. **Error isolation.** Summary failures must NOT erase `new_anns` from results. The inner try/except around `summarize()` catches Claude errors and substitutes a `_(Summary unavailable: ...)_` placeholder, so the raw announcement list still renders in the report.

5. **Seed mode.** `python monitor.py --seed` marks current announcements as "seen" without burning Claude calls. Use after adding a new company to prevent the first real run from reporting 30 historical items.

## Environment variables

| Var | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Claude API auth |
| `EMAIL_TO` | Recipient |
| `EMAIL_FROM` | Sender (Gmail address) |
| `SMTP_HOST` | e.g. `smtp.gmail.com` |
| `SMTP_PORT` | 587 (STARTTLS) or 465 (SSL) |
| `SMTP_USER` | SMTP login user |
| `SMTP_PASSWORD` | Gmail **app password**, not account password |

Local: read from `.env`. GitHub Actions: read from repo secrets.

## Deployment modes

- **Local cron / launchd:** `~/Library/LaunchAgents/com.anirudh.asx-monitor.plist` invokes `run.sh`. Logs go to `logs/YYYY-MM-DD.log`.
- **GitHub Actions:** `.github/workflows/daily.yml` runs on cron, commits state + reports back to `main`.

Both can coexist, but you'd then get duplicate emails. Pick one.

## Adding a new company

1. Append to `companies.json`
2. Run `python monitor.py --seed` to baseline (or trigger the seed workflow_dispatch on GitHub)
3. From the next run on, only new announcements for that ticker will appear

## Adjusting the prompt / report

- The Claude prompt in `summarize()` is the main lever for output style. Constraint pattern: tell Claude what NOT to do (e.g. "don't restate the list", "no top-level headings"), and give an exact section structure.
- The markdown structure is in `build_report()` — table layout, separators, etc.
- HTML email styling is in `_HTML_CSS` (string constant near `_render_html()`).

## Cost

- Sonnet 4.6 daily run: ~$0.01–$0.05 depending on how many new announcements
- Seed run: $0 (no Claude calls)
- A "wipe state" full re-summarisation of 25 companies × 30 items: ~$0.15
