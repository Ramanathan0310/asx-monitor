#!/usr/bin/env python3
"""
ASX Announcement Monitor
Checks marketindex.com.au daily for new announcements from specified companies
and generates a Claude-powered summary report.
"""

import json
import hashlib
import os
import random
import smtplib
import ssl
import sys
import time
from datetime import date, datetime
from email.message import EmailMessage
from pathlib import Path

from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth


# --- Load .env if present ---
def _load_dotenv():
    """
    Load .env into os.environ. Overwrites existing values only if they are
    empty — handy because some shells/harnesses export the key as "" which
    silently breaks SDKs that test for unset, not empty.
    """
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        k = key.strip()
        v = value.strip().strip('"').strip("'")
        if not os.environ.get(k):  # not set OR empty string
            os.environ[k] = v


_load_dotenv()

# --- Paths ---
BASE_DIR = Path(__file__).parent
COMPANIES_FILE = BASE_DIR / "companies.json"
STATE_FILE = BASE_DIR / "seen_announcements.json"
REPORTS_DIR = BASE_DIR / "reports"

BASE_URL = "https://www.marketindex.com.au/asx/{ticker}/announcements"


# --- Config ---

def load_companies() -> list[dict]:
    if not COMPANIES_FILE.exists():
        default = {
            "companies": [
                {"ticker": "DRO", "name": "DroneShield"},
                {"ticker": "BHP", "name": "BHP Group"},
            ]
        }
        COMPANIES_FILE.write_text(json.dumps(default, indent=2))
        print(f"Created {COMPANIES_FILE} — edit it to add your companies.")
    return json.loads(COMPANIES_FILE.read_text())["companies"]


# --- State ---

def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    return json.loads(STATE_FILE.read_text())


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def ann_id(ann: dict) -> str:
    key = f"{ann['date']}|{ann.get('time', '')}|{ann['title']}"
    return hashlib.md5(key.encode()).hexdigest()


# --- Scraping ---

def fetch_announcements(page, ticker: str) -> list[dict]:
    """
    Scrape the announcements table from marketindex.com.au using a shared
    stealth-enabled Playwright page.

    Page structure (verified May 2026): table rows with columns
        [date, time, title, pages, PDF link]
    e.g. "14/05/26 | 4:53pm | Ceasing to be a substantial holder | 4 | PDF"

    Cloudflare Turnstile is bypassed by playwright_stealth; we still
    add jittered delays in the caller to avoid tripping rate limits.
    """
    url = BASE_URL.format(ticker=ticker.lower())
    announcements = []

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        # Wait for Vue to populate the table (no networkidle on this site)
        page.wait_for_timeout(3000)
    except Exception as e:
        print(f"    Warning: page load failed for {ticker}: {e}")
        return []

    rows = page.query_selector_all("table tbody tr")

    for row in rows[:30]:  # most recent 30
        cells = row.query_selector_all("td")
        if len(cells) < 3:
            continue

        date_text = cells[0].inner_text().strip()
        time_text = cells[1].inner_text().strip()
        title_text = cells[2].inner_text().strip()

        link = row.query_selector("a[href*='announcement'], a[href*='pdf'], a[href*='.pdf']")
        href = ""
        if link:
            raw = link.get_attribute("href") or ""
            href = raw if raw.startswith("http") else f"https://www.marketindex.com.au{raw}"

        if title_text and date_text:
            announcements.append({
                "date": date_text,
                "time": time_text,
                "title": title_text,
                "url": href,
            })

    return announcements


# --- New announcement detection ---

def find_new(ticker: str, announcements: list[dict], state: dict) -> tuple[list[dict], list[str]]:
    seen = set(state.get(ticker, []))
    new_anns, new_ids = [], []
    for ann in announcements:
        aid = ann_id(ann)
        if aid not in seen:
            new_anns.append(ann)
            new_ids.append(aid)
    return new_anns, new_ids


# --- Claude summarization ---

def summarize(company_name: str, ticker: str, announcements: list[dict]) -> str:
    import anthropic  # imported here so --seed mode works without the API key
    client = anthropic.Anthropic()

    ann_text = "\n".join(
        f"- {a['date']} {a.get('time', '')} — {a['title']}"
        for a in announcements
    )

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        messages=[{
            "role": "user",
            "content": f"""You are a financial analyst writing a brief for an investor monitoring {company_name} (ASX: {ticker}).

The investor has already seen the raw list of new ASX announcements below. Your job is to add context — DO NOT restate or summarize each item one-by-one.

New announcements ({len(announcements)} items):
{ann_text}

Produce your response in EXACTLY this format (markdown, no top-level # or ## headings, use bold for section labels):

**Assessment:** A 2-4 sentence plain-English read on what these announcements collectively suggest about the company's current state — operational momentum, capital actions, governance changes, results, etc. Be specific and concrete; avoid generic phrases like "the company is active."

**Notable items:** A bulleted list of 1-4 specific announcements that deserve closer attention (results, dividends, director changes, capital raises, substantial holders, material contracts, etc.). For each, give one tight sentence on why it matters. Skip routine items (weekly NTAs, application for quotation, ceasing/becoming substantial holder, change of director's interest notices unless unusually large) — only flag items an investor would actually act on or watch for.

**Watch for:** One short sentence on what to look for in the next few weeks given these signals, or "Nothing material" if it's all routine.

Keep the entire response under 250 words. Be factual, plain, and direct — no hype, no hedging.""",
        }],
    )
    return response.content[0].text


# --- Report ---

def build_report(results: list[tuple]) -> str:
    """
    Build the daily markdown report.

    Layout:
      1. Header with date + summary stats
      2. Overview table (only companies with new announcements)
      3. Per-company sections (only those with new announcements)
      4. "No new announcements" footer listing the quiet companies
    """
    today_str = date.today().strftime("%A, %d %B %Y")
    gen_time = datetime.now().strftime("%H:%M %Z").strip()

    with_news = [(n, t, a, s) for n, t, a, s in results if a]
    quiet = [(n, t) for n, t, a, _ in results if not a]
    total_new = sum(len(a) for _, _, a, _ in with_news)

    lines = []
    lines.append("# ASX Monitor — Daily Report")
    lines.append("")
    lines.append(f"**{today_str}** · generated {gen_time}")
    lines.append("")

    if not with_news:
        lines.append("> ✅ No new announcements across any of the "
                     f"{len(results)} monitored companies.")
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append("### Companies monitored")
        lines.append("")
        lines.append(", ".join(f"{n} ({t})" for n, t, _, _ in results))
        return "\n".join(lines)

    # --- Stats banner ---
    lines.append(f"**{total_new}** new announcement{'s' if total_new != 1 else ''} "
                 f"across **{len(with_news)}** of **{len(results)}** companies.")
    lines.append("")

    # --- Overview table ---
    lines.append("## Overview")
    lines.append("")
    lines.append("| Company | Ticker | New | Latest |")
    lines.append("|---|---|---:|---|")
    for name, ticker, anns, _ in with_news:
        latest = anns[0]  # already most-recent-first
        ts = f"{latest['date']} {latest.get('time', '')}".strip()
        lines.append(f"| {name} | **{ticker}** | {len(anns)} | {ts} |")
    lines.append("")

    # --- Per-company detail ---
    for name, ticker, anns, summary in with_news:
        lines.append("---")
        lines.append("")
        lines.append(f"## {name}  ·  **{ticker}**  ·  {len(anns)} new")
        lines.append("")

        # Announcement list
        for a in anns:
            ts = f"{a['date']} {a.get('time', '')}".strip()
            if a.get("url"):
                lines.append(f"- `{ts}` — [{a['title']}]({a['url']})")
            else:
                lines.append(f"- `{ts}` — {a['title']}")
        lines.append("")

        # Claude analysis
        if summary:
            lines.append(summary.strip())
            lines.append("")

    # --- Footer: quiet companies ---
    if quiet:
        lines.append("---")
        lines.append("")
        lines.append("### No new announcements")
        lines.append("")
        lines.append(", ".join(f"{n} ({t})" for n, t in quiet))
        lines.append("")

    return "\n".join(lines)


# --- Main ---

_HTML_CSS = """
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  font-size: 15px;
  line-height: 1.55;
  color: #1a1a1a;
  max-width: 760px;
  margin: 24px auto;
  padding: 0 20px;
}
h1 { font-size: 24px; border-bottom: 2px solid #1a1a1a; padding-bottom: 8px; margin-bottom: 8px; }
h2 { font-size: 19px; margin-top: 32px; padding-top: 18px; border-top: 1px solid #e6e6e6; }
h3 { font-size: 16px; color: #444; margin-top: 24px; }
code { background: #f4f4f4; padding: 1px 5px; border-radius: 3px; font-size: 13px; color: #2c3e50; }
a { color: #1259c3; text-decoration: none; }
a:hover { text-decoration: underline; }
table { border-collapse: collapse; margin: 12px 0; font-size: 14px; }
th, td { border: 1px solid #d8d8d8; padding: 6px 12px; text-align: left; }
th { background: #f4f4f4; }
ul { padding-left: 22px; }
li { margin: 4px 0; }
blockquote { border-left: 3px solid #4caf50; margin: 12px 0; padding: 8px 14px; background: #f1f8e9; color: #2e3a23; }
hr { border: none; border-top: 1px solid #e6e6e6; margin: 24px 0; }
strong { color: #000; }
"""


def _render_html(markdown_body: str) -> str:
    """Render the markdown report as a self-contained HTML email body."""
    import markdown as md
    html = md.markdown(markdown_body, extensions=["tables", "fenced_code"])
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>{_HTML_CSS}</style></head>
<body>{html}</body></html>"""


def send_email(subject: str, body_markdown: str) -> bool:
    """
    Send the report as a multipart email: text/plain (raw markdown) for
    fallback, text/html (rendered) for clients like Gmail.

    Required env vars (set in .env):
        EMAIL_TO          recipient address
        EMAIL_FROM        sender address (usually same as SMTP user)
        SMTP_HOST         e.g. smtp.gmail.com
        SMTP_PORT         587 (STARTTLS) or 465 (SSL)
        SMTP_USER         your email address
        SMTP_PASSWORD     app password (NOT your account password)
    """
    to_addr = os.environ.get("EMAIL_TO")
    from_addr = os.environ.get("EMAIL_FROM")
    host = os.environ.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")

    if not all([to_addr, from_addr, host, user, password]):
        print("  (email skipped — SMTP env vars not all set)")
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.set_content(body_markdown)              # text/plain
    try:
        msg.add_alternative(_render_html(body_markdown), subtype="html")  # text/html
    except Exception as e:
        print(f"  (HTML rendering failed, sending plain text only: {e})")

    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=30) as s:
                s.login(user, password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as s:
                s.starttls(context=ssl.create_default_context())
                s.login(user, password)
                s.send_message(msg)
        print(f"  ✓ Emailed report to {to_addr}")
        return True
    except Exception as e:
        print(f"  Email failed: {e}")
        return False


def run(seed_only: bool = False):
    mode = "SEED (silent baseline)" if seed_only else "REPORT"
    print(f"\nASX Monitor — {date.today().isoformat()} [{mode}]")
    print("=" * 55)

    REPORTS_DIR.mkdir(exist_ok=True)
    companies = load_companies()
    state = load_state()
    results = []

    stealth = Stealth()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        for i, company in enumerate(companies):
            ticker = company["ticker"].upper()
            name = company.get("name", ticker)
            print(f"\n→ {name} ({ticker})  [{i+1}/{len(companies)}]")

            # Fresh context per company — Cloudflare flags same-session
            # navigation across many pages as bot-like, but treats each
            # fresh context as a new visitor.
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/131.0.0.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080},
                locale="en-AU",
                timezone_id="Australia/Sydney",
            )
            stealth.apply_stealth_sync(context)
            page = context.new_page()

            new_anns, new_ids, summary = [], [], None
            try:
                announcements = fetch_announcements(page, ticker)
                print(f"  Fetched {len(announcements)} announcements")

                new_anns, new_ids = find_new(ticker, announcements, state)
                print(f"  New: {len(new_anns)}")

                if new_anns:
                    if seed_only:
                        print("  Seed mode — marking as seen without summarizing")
                    else:
                        print("  Summarizing with Claude...")
                        try:
                            summary = summarize(name, ticker, new_anns)
                        except Exception as e:
                            # Don't lose the new announcements if Claude fails;
                            # report the raw list with an error note.
                            print(f"  Summary failed: {e}")
                            summary = f"_(Summary unavailable: {e})_"

                    # Mark as seen regardless of summary outcome
                    state.setdefault(ticker, [])
                    state[ticker] = (state[ticker] + new_ids)[-500:]

            except Exception as e:
                print(f"  ERROR fetching {ticker}: {e}")

            finally:
                context.close()

            results.append((name, ticker, new_anns, summary))

            # Polite jittered delay between requests
            if i < len(companies) - 1:
                time.sleep(random.uniform(2, 5))

        browser.close()

    save_state(state)

    if seed_only:
        total_seeded = sum(len(anns) for _, _, anns, _ in results)
        print(f"\n✓ Seeded {total_seeded} existing announcements as 'seen'.")
        print("  Future runs (without --seed) will only summarize new ones.")
        return

    report = build_report(results)
    report_path = REPORTS_DIR / f"report_{date.today().isoformat()}.md"
    report_path.write_text(report)
    print(f"\n✓ Report saved → {report_path}")

    # Email it
    total_new = sum(len(anns) for _, _, anns, _ in results)
    if total_new > 0:
        subject = f"ASX Update — {total_new} new announcement{'s' if total_new != 1 else ''} ({date.today().isoformat()})"
        send_email(subject, report)
    else:
        # Quiet mode: still email a "no news" note? Skip by default.
        if os.environ.get("EMAIL_EMPTY_REPORTS") == "1":
            send_email(f"ASX Update — no news ({date.today().isoformat()})", report)
        else:
            print("  (no new announcements — email skipped; set EMAIL_EMPTY_REPORTS=1 to send anyway)")

    print("\n" + report)


if __name__ == "__main__":
    seed = "--seed" in sys.argv
    run(seed_only=seed)
