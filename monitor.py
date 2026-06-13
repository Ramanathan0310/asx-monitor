#!/usr/bin/env python3
"""
ASX Announcement Monitor — v2
Adds on top of v1:
  • Keyword classification (RESULTS / ACQUISITION / CAPITAL_RAISE /
    CONTRACT / SUBSTANTIAL_HOLDER / DIRECTOR_CHANGE / ROUTINE)
  • PDF download + Claude deep-dive for high-impact tiers
  • Substantial shareholder alerts (new >5%, stake increase, exit <5%)
"""

import io
import json
import hashlib
import os
import random
import re
import smtplib
import ssl
import sys
import time
from datetime import date, datetime
from email.message import EmailMessage
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

# ---------------------------------------------------------------------------
# .env loader
# ---------------------------------------------------------------------------

def _load_dotenv():
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
        if not os.environ.get(k):
            os.environ[k] = v

_load_dotenv()

# ---------------------------------------------------------------------------
# Paths / config
# ---------------------------------------------------------------------------

BASE_DIR       = Path(__file__).parent
COMPANIES_FILE = BASE_DIR / "companies.json"
STATE_FILE     = BASE_DIR / "seen_announcements.json"
REPORTS_DIR    = BASE_DIR / "reports"
BASE_URL       = "https://www.marketindex.com.au/asx/{ticker}/announcements"

# Safety caps (mirrors Bob's approach)
MAX_PDFS_PER_RUN     = 10
MAX_LLM_CALLS_PER_RUN = 15

_pdf_count = 0
_llm_count = 0

# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

TIERS = {
    "RESULTS":            ["half year", "half-year", "hy result", "full year", "full-year",
                           "fy result", "annual result", "preliminary final", "appendix 4e",
                           "appendix 4d", "financial result", "earnings"],
    "ACQUISITION":        ["acquisition", "acquires", "merger", "takeover", "scheme of arrangement",
                           "binding agreement to acquire", "proposed acquisition", "transaction",
                           "divestment", "disposal of"],
    "CAPITAL_RAISE":      ["capital raise", "placement", "entitlement offer", "rights issue",
                           "share purchase plan", "spp ", "convertible note", "debt refinanc",
                           "bond issu", "institutional placement"],
    "CONTRACT":           ["contract", "awarded", "agreement", "partnership", "mou ",
                           "memorandum of understanding", "purchase order", "major order",
                           "strategic alliance"],
    "SUBSTANTIAL_HOLDER": ["substantial holder", "substantial shareholder", "ceasing to be",
                           "becoming a substantial", "change in substantial"],
    "DIRECTOR_CHANGE":    ["appendix 3y", "change of director", "director's interest",
                           "director interest", "appointment of", "resignation of",
                           "ceo appointment", "cfo appointment", "md appointment"],
    "ROUTINE":            ["nta ", "net tangible asset", "appendix 3b", "application for quotation",
                           "change of address", "cleansing notice", "notification of dividend",
                           "dividend reinvestment", "investor presentation"],  # low priority
}

# Tiers that get PDF deep-dive
DEEP_DIVE_TIERS = {"RESULTS", "ACQUISITION", "CAPITAL_RAISE", "CONTRACT"}

TIER_PRIORITY = {
    "RESULTS": 1, "ACQUISITION": 2, "CAPITAL_RAISE": 3, "CONTRACT": 4,
    "SUBSTANTIAL_HOLDER": 5, "DIRECTOR_CHANGE": 6, "ROUTINE": 99, "OTHER": 50,
}

TIER_LABELS = {
    "RESULTS":            "📊 Results",
    "ACQUISITION":        "🤝 Acquisition/M&A",
    "CAPITAL_RAISE":      "💰 Capital Raise",
    "CONTRACT":           "📋 Contract/Agreement",
    "SUBSTANTIAL_HOLDER": "👤 Substantial Holder",
    "DIRECTOR_CHANGE":    "🏛 Director Change",
    "ROUTINE":            "🔁 Routine",
    "OTHER":              "📌 Other",
}


def classify(title: str) -> str:
    t = title.lower()
    for tier, keywords in TIERS.items():
        if any(kw in t for kw in keywords):
            return tier
    return "OTHER"


# ---------------------------------------------------------------------------
# Substantial shareholder parser
# ---------------------------------------------------------------------------

def parse_substantial_holder(text: str) -> dict | None:
    """
    Extract key fields from a substantial holder notice PDF text.
    Returns dict with holder, action, prev_pct, new_pct or None if unparseable.
    """
    if not text or len(text) < 100:
        return None

    result = {}

    # Holder name — usually after "Name of substantial holder"
    m = re.search(r"name of (?:substantial )?holder[:\s]+([^\n]{3,80})", text, re.IGNORECASE)
    if m:
        result["holder"] = m.group(1).strip()

    # Previous % — must start with "previous"
    m = re.search(r"previous\s+(?:notice\s+)?(?:percentage|voting power)[:\s]+(\d+\.?\d*)\s*%?",
                  text, re.IGNORECASE)
    if m:
        result["prev_pct"] = float(m.group(1))

    # New / current % — "present percentage" or standalone "new percentage"
    m = re.search(r"(?:present|current|new)\s+(?:percentage|voting power)[:\s]+(\d+\.?\d*)\s*%?",
                  text, re.IGNORECASE)
    if m:
        result["new_pct"] = float(m.group(1))

    if not result:
        return None

    prev = result.get("prev_pct", 0)
    new  = result.get("new_pct", 0)

    if prev == 0 and new >= 5:
        result["action"] = "NEW_ENTRY"          # new >5% holder
    elif new < 5 and prev >= 5:
        result["action"] = "EXIT"               # dropped below 5%
    elif new > prev:
        result["action"] = "INCREASE"
    elif new < prev:
        result["action"] = "DECREASE"
    else:
        result["action"] = "UNCHANGED"

    return result


def format_holder_alert(parsed: dict, title: str) -> str:
    if not parsed:
        return ""
    action = parsed.get("action", "")
    holder = parsed.get("holder", "unknown holder")
    prev   = parsed.get("prev_pct")
    new    = parsed.get("new_pct")

    pct_str = ""
    if prev is not None and new is not None:
        pct_str = f" ({prev:.1f}% → {new:.1f}%)"
    elif new is not None:
        pct_str = f" ({new:.1f}%)"

    icons = {
        "NEW_ENTRY": "🚨 NEW MAJOR HOLDER",
        "EXIT":      "🚪 MAJOR HOLDER EXIT",
        "INCREASE":  "📈 Stake Increase",
        "DECREASE":  "📉 Stake Decrease",
        "UNCHANGED": "➡️ Unchanged",
    }
    label = icons.get(action, "👤 Holder Change")
    return f"**{label}** — {holder}{pct_str}"


# ---------------------------------------------------------------------------
# Companies / state
# ---------------------------------------------------------------------------

def load_companies() -> list[dict]:
    if not COMPANIES_FILE.exists():
        default = {"companies": [
            {"ticker": "DRO", "name": "DroneShield"},
            {"ticker": "BHP", "name": "BHP Group"},
        ]}
        COMPANIES_FILE.write_text(json.dumps(default, indent=2))
        print(f"Created {COMPANIES_FILE} — edit it to add your companies.")
    return json.loads(COMPANIES_FILE.read_text())["companies"]


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    return json.loads(STATE_FILE.read_text())


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def ann_id(ann: dict) -> str:
    key = f"{ann['date']}|{ann.get('time', '')}|{ann['title']}"
    return hashlib.md5(key.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------

def fetch_announcements(page, ticker: str) -> list[dict]:
    url = BASE_URL.format(ticker=ticker.lower())
    announcements = []
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(3000)
    except Exception as e:
        print(f"  Warning: page load failed for {ticker}: {e}")
        return []

    rows = page.query_selector_all("table tbody tr")
    for row in rows[:30]:
        cells = row.query_selector_all("td")
        if len(cells) < 3:
            continue
        date_text  = cells[0].inner_text().strip()
        time_text  = cells[1].inner_text().strip()
        title_text = cells[2].inner_text().strip()

        link = row.query_selector("a[href*='announcement'], a[href*='pdf'], a[href*='.pdf']")
        href = ""
        if link:
            raw = link.get_attribute("href") or ""
            href = raw if raw.startswith("http") else f"https://www.marketindex.com.au{raw}"

        if title_text and date_text:
            tier = classify(title_text)
            announcements.append({
                "date":  date_text,
                "time":  time_text,
                "title": title_text,
                "url":   href,
                "tier":  tier,
            })
    return announcements


def find_new(ticker: str, announcements: list[dict], state: dict) -> tuple[list[dict], list[str]]:
    seen = set(state.get(ticker, []))
    new_anns, new_ids = [], []
    for ann in announcements:
        aid = ann_id(ann)
        if aid not in seen:
            new_anns.append(ann)
            new_ids.append(aid)
    return new_anns, new_ids


# ---------------------------------------------------------------------------
# PDF fetching + text extraction
# ---------------------------------------------------------------------------

def fetch_pdf_text(url: str) -> str | None:
    """
    Download a PDF from the given URL and extract its text.
    Returns extracted text or None on failure.
    """
    if not url:
        return None
    try:
        from pypdf import PdfReader
    except ImportError:
        print("  (pypdf not installed — pip install pypdf)")
        return None

    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept": "application/pdf,*/*",
        }
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()

        if "pdf" not in resp.headers.get("content-type", "").lower() and not url.lower().endswith(".pdf"):
            # Might be HTML consent page — skip
            return None

        reader = PdfReader(io.BytesIO(resp.content))
        pages = [page.extract_text() or "" for page in reader.pages[:20]]  # cap at 20 pages
        text = "\n".join(pages).strip()

        # Sanity check — ASX disclaimer-only pages are ~300 chars
        if len(text) < 400:
            return None

        # Cap to ~15k chars to keep Claude tokens reasonable
        return text[:15_000]

    except Exception as e:
        print(f"  PDF fetch failed ({url[:60]}...): {e}")
        return None


# ---------------------------------------------------------------------------
# Claude calls
# ---------------------------------------------------------------------------

def summarize_company(company_name: str, ticker: str, announcements: list[dict]) -> str:
    """Standard per-company summary (existing behaviour)."""
    global _llm_count
    if _llm_count >= MAX_LLM_CALLS_PER_RUN:
        return "_(LLM call limit reached)_"
    _llm_count += 1

    import anthropic
    client = anthropic.Anthropic()

    ann_text = "\n".join(
        f"- [{a['tier']}] {a['date']} {a.get('time', '')} — {a['title']}"
        for a in announcements
    )

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        messages=[{
            "role": "user",
            "content": f"""You are a financial analyst writing a brief for an investor monitoring {company_name} (ASX: {ticker}).

The investor has already seen the raw list of new ASX announcements below. Your job is to add context — DO NOT restate or summarize each item one-by-one. Each announcement is prefixed with its category tier in [brackets].

New announcements ({len(announcements)} items):
{ann_text}

Produce your response in EXACTLY this format (markdown, no top-level headings, use bold for labels):

**Assessment:** A 2-4 sentence plain-English read on what these announcements collectively suggest about the company's current state. Be specific and concrete; avoid generic phrases.

**Notable items:** A bulleted list of 1-4 specific announcements that deserve closer attention. For each, one tight sentence on why it matters. Skip pure ROUTINE items unless unusually significant.

**Watch for:** One short sentence on what to look for next, or "Nothing material" if all routine.

Under 250 words. Be factual, direct — no hype.""",
        }],
    )
    return response.content[0].text


def deep_dive_pdf(company_name: str, ticker: str, ann: dict, pdf_text: str) -> str:
    """Deep-dive Claude analysis of a specific announcement PDF."""
    global _llm_count
    if _llm_count >= MAX_LLM_CALLS_PER_RUN:
        return "_(LLM call limit reached)_"
    _llm_count += 1

    import anthropic
    client = anthropic.Anthropic()

    tier_desc = {
        "RESULTS":       "financial results report",
        "ACQUISITION":   "acquisition/M&A announcement",
        "CAPITAL_RAISE": "capital raising document",
        "CONTRACT":      "contract/agreement announcement",
    }.get(ann["tier"], "announcement")

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        messages=[{
            "role": "user",
            "content": f"""You are analysing a {tier_desc} for {company_name} (ASX: {ticker}).

Announcement title: {ann['title']}
Date: {ann['date']}

Document text (first ~15,000 chars):
{pdf_text}

Write a focused investor brief with these sections (bold labels, no extra headings):

**Key facts:** 3-5 bullet points with the most important specific numbers/facts from the document (revenue, profit, deal size, contract value, raise amount, etc.). Be precise — use actual figures from the document.

**What it means:** 2-3 sentences on the strategic/financial significance. Is this positive, negative, or neutral for shareholders? Why?

**Risk / Watch:** One sentence on the main risk or thing to monitor arising from this announcement.

Keep under 300 words. Numbers and specifics only — no padding.""",
        }],
    )
    return response.content[0].text


def summarize_substantial_holder(company_name: str, ticker: str, ann: dict, pdf_text: str) -> str:
    """Focused summary for substantial holder notices."""
    global _llm_count
    if _llm_count >= MAX_LLM_CALLS_PER_RUN:
        return "_(LLM call limit reached)_"
    _llm_count += 1

    import anthropic
    client = anthropic.Anthropic()

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        messages=[{
            "role": "user",
            "content": f"""Analyse this substantial shareholder notice for {company_name} (ASX: {ticker}).

Announcement: {ann['title']}
Date: {ann['date']}

Document text:
{pdf_text[:5000]}

Produce a brief 3-sentence summary covering:
1. Who the holder is
2. What changed (previous % → new %, and whether they're entering/exiting/increasing/decreasing)
3. Why this might matter to other shareholders (e.g. activist investor, index fund rebalance, insider exit signal)

Be specific and use exact figures from the document.""",
        }],
    )
    return response.content[0].text


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------

_HTML_CSS = """
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  font-size: 15px; line-height: 1.55; color: #1a1a1a;
  max-width: 780px; margin: 24px auto; padding: 0 20px;
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
blockquote { border-left: 3px solid #4caf50; margin: 12px 0; padding: 8px 14px;
             background: #f1f8e9; color: #2e3a23; }
.tier-badge {
  display: inline-block; font-size: 12px; font-weight: 600;
  padding: 1px 7px; border-radius: 10px; margin-right: 6px;
  background: #e8f0fe; color: #1a47a0;
}
.tier-RESULTS      { background: #fce8e6; color: #c5221f; }
.tier-ACQUISITION  { background: #fef7e0; color: #8a5a00; }
.tier-CAPITAL_RAISE{ background: #e6f4ea; color: #1e7e34; }
.tier-CONTRACT     { background: #e8f0fe; color: #1a47a0; }
.tier-SUBSTANTIAL_HOLDER { background: #f3e8fd; color: #6a1b9a; }
.tier-DIRECTOR_CHANGE    { background: #fff3e0; color: #e65100; }
.tier-ROUTINE      { background: #f5f5f5; color: #757575; }
.holder-alert { background: #f3e8fd; border-left: 4px solid #7b1fa2;
                padding: 8px 14px; margin: 8px 0; border-radius: 0 4px 4px 0; }
.deep-dive { background: #fffde7; border-left: 4px solid #f9a825;
             padding: 10px 14px; margin: 10px 0; border-radius: 0 4px 4px 0; }
hr { border: none; border-top: 1px solid #e6e6e6; margin: 24px 0; }
strong { color: #000; }
"""


def build_report(results: list[tuple]) -> str:
    """
    results: list of (name, ticker, new_anns_with_extras, summary)
    new_anns_with_extras: list of ann dicts, each may have:
      ann['tier'], ann['pdf_text'], ann['pdf_summary'], ann['holder_parsed'], ann['holder_alert']
    """
    today_str = date.today().strftime("%A, %d %B %Y")
    gen_time  = datetime.now().strftime("%H:%M %Z").strip()

    with_news = [(n, t, a, s) for n, t, a, s in results if a]
    quiet     = [(n, t) for n, t, a, _ in results if not a]
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

    lines.append(f"**{total_new}** new announcement{'s' if total_new != 1 else ''} "
                 f"across **{len(with_news)}** of **{len(results)}** companies.")
    lines.append("")

    # --- Overview table ---
    lines.append("## Overview")
    lines.append("")
    lines.append("| Company | Ticker | New | Tiers | Latest |")
    lines.append("|---|---|---:|---|---|")
    for name, ticker, anns, _ in with_news:
        tiers_seen = sorted(set(a["tier"] for a in anns), key=lambda t: TIER_PRIORITY.get(t, 50))
        tier_str   = " · ".join(TIER_LABELS.get(t, t) for t in tiers_seen)
        latest     = anns[0]
        ts         = f"{latest['date']} {latest.get('time', '')}".strip()
        lines.append(f"| {name} | **{ticker}** | {len(anns)} | {tier_str} | {ts} |")
    lines.append("")

    # --- Per-company detail ---
    for name, ticker, anns, summary in with_news:
        lines.append("---")
        lines.append("")
        lines.append(f"## {name} · **{ticker}** · {len(anns)} new")
        lines.append("")

        # Sort by tier priority
        sorted_anns = sorted(anns, key=lambda a: TIER_PRIORITY.get(a["tier"], 50))

        for a in sorted_anns:
            ts         = f"{a['date']} {a.get('time', '')}".strip()
            tier_label = TIER_LABELS.get(a["tier"], a["tier"])
            title_part = f"[{a['title']}]({a['url']})" if a.get("url") else a["title"]
            lines.append(f"- `{ts}` **{tier_label}** — {title_part}")

            # Substantial holder alert inline
            if a.get("holder_alert"):
                lines.append(f"  > {a['holder_alert']}")

            # PDF deep-dive
            if a.get("pdf_summary"):
                lines.append("")
                lines.append(f"  **📄 PDF Analysis:**")
                for pdf_line in a["pdf_summary"].strip().splitlines():
                    lines.append(f"  {pdf_line}")
                lines.append("")

        lines.append("")

        # Company-level Claude summary
        if summary:
            lines.append(summary.strip())
            lines.append("")

    # --- Quiet companies footer ---
    if quiet:
        lines.append("---")
        lines.append("")
        lines.append("### No new announcements")
        lines.append("")
        lines.append(", ".join(f"{n} ({t})" for n, t in quiet))
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def _render_html(markdown_body: str) -> str:
    import markdown as md
    html = md.markdown(markdown_body, extensions=["tables", "fenced_code"])
    # Inject tier badge classes into rendered HTML
    for tier in TIERS:
        html = html.replace(
            f"[{tier}]",
            f'<span class="tier-badge tier-{tier}">{TIER_LABELS.get(tier, tier)}</span>'
        )
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>{_HTML_CSS}</style></head>
<body>{html}</body></html>"""


def send_email(subject: str, body_markdown: str) -> bool:
    to_addr  = os.environ.get("EMAIL_TO")
    from_addr = os.environ.get("EMAIL_FROM")
    host     = os.environ.get("SMTP_HOST")
    port     = int(os.environ.get("SMTP_PORT", "587"))
    user     = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")

    if not all([to_addr, from_addr, host, user, password]):
        print("  (email skipped — SMTP env vars not all set)")
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"]    = from_addr
    msg["To"]      = to_addr
    msg.set_content(body_markdown)

    try:
        msg.add_alternative(_render_html(body_markdown), subtype="html")
    except Exception as e:
        print(f"  (HTML rendering failed, plain text only: {e})")

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
        print(f"  ✓ Emailed to {to_addr}")
        return True
    except Exception as e:
        print(f"  Email failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run(seed_only: bool = False):
    global _pdf_count, _llm_count
    _pdf_count = 0
    _llm_count = 0

    mode = "SEED (silent baseline)" if seed_only else "REPORT"
    print(f"\nASX Monitor v2 — {date.today().isoformat()} [{mode}]")
    print("=" * 55)

    REPORTS_DIR.mkdir(exist_ok=True)
    companies = load_companies()
    state     = load_state()
    results   = []
    stealth   = Stealth()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        for i, company in enumerate(companies):
            ticker = company["ticker"].upper()
            name   = company.get("name", ticker)
            print(f"\n→ {name} ({ticker}) [{i+1}/{len(companies)}]")

            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
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

                if new_anns and not seed_only:
                    # Show tier breakdown
                    tier_counts = {}
                    for a in new_anns:
                        tier_counts[a["tier"]] = tier_counts.get(a["tier"], 0) + 1
                    tier_str = ", ".join(f"{t}:{c}" for t, c in tier_counts.items())
                    print(f"  Tiers: {tier_str}")

                    # --- PDF fetch + deep-dive for high-impact tiers ---
                    for ann in new_anns:
                        tier = ann["tier"]

                        # PDF deep-dive
                        if tier in DEEP_DIVE_TIERS and ann.get("url") and _pdf_count < MAX_PDFS_PER_RUN:
                            print(f"  Fetching PDF for [{tier}]: {ann['title'][:60]}...")
                            pdf_text = fetch_pdf_text(ann["url"])
                            if pdf_text:
                                _pdf_count += 1
                                ann["pdf_text"] = pdf_text
                                print(f"  → PDF extracted ({len(pdf_text)} chars), running deep-dive...")
                                ann["pdf_summary"] = deep_dive_pdf(name, ticker, ann, pdf_text)
                            else:
                                print(f"  → PDF unavailable/empty")

                        # Substantial holder: fetch PDF + parse + summarise
                        if tier == "SUBSTANTIAL_HOLDER" and ann.get("url"):
                            if _pdf_count < MAX_PDFS_PER_RUN:
                                print(f"  Fetching substantial holder PDF...")
                                pdf_text = fetch_pdf_text(ann["url"])
                                if pdf_text:
                                    _pdf_count += 1
                                    ann["pdf_text"] = pdf_text
                                    parsed = parse_substantial_holder(pdf_text)
                                    if parsed:
                                        ann["holder_parsed"] = parsed
                                        ann["holder_alert"]  = format_holder_alert(parsed, ann["title"])
                                        print(f"  → {ann['holder_alert']}")
                                    # Only Claude-summarise notable actions
                                    if parsed and parsed.get("action") in ("NEW_ENTRY", "EXIT", "INCREASE"):
                                        ann["pdf_summary"] = summarize_substantial_holder(
                                            name, ticker, ann, pdf_text
                                        )

                    # Company-level summary
                    print(f"  Summarizing with Claude...")
                    try:
                        summary = summarize_company(name, ticker, new_anns)
                    except Exception as e:
                        print(f"  Summary failed: {e}")
                        summary = f"_(Summary unavailable: {e})_"

                # Mark as seen
                state.setdefault(ticker, [])
                state[ticker] = (state[ticker] + new_ids)[-500:]

            except Exception as e:
                print(f"  ERROR fetching {ticker}: {e}")
            finally:
                context.close()

            results.append((name, ticker, new_anns, summary))

            if i < len(companies) - 1:
                time.sleep(random.uniform(2, 5))

        browser.close()

    save_state(state)

    if seed_only:
        total_seeded = sum(len(anns) for _, _, anns, _ in results)
        print(f"\n✓ Seeded {total_seeded} existing announcements as 'seen'.")
        print("  Future runs will only report new ones.")
        return

    print(f"\n  LLM calls used: {_llm_count}/{MAX_LLM_CALLS_PER_RUN}")
    print(f"  PDFs fetched:   {_pdf_count}/{MAX_PDFS_PER_RUN}")

    report      = build_report(results)
    report_path = REPORTS_DIR / f"report_{date.today().isoformat()}.md"
    report_path.write_text(report)
    print(f"\n✓ Report saved → {report_path}")

    total_new = sum(len(anns) for _, _, anns, _ in results)
    if total_new > 0:
        # Flag substantial holder alerts in subject line
        holder_alerts = sum(
            1 for _, _, anns, _ in results
            for a in anns
            if a.get("holder_alert")
        )
        subject = f"ASX Update — {total_new} new announcement{'s' if total_new != 1 else ''}"
        if holder_alerts:
            subject += f" · {holder_alerts} holder alert{'s' if holder_alerts > 1 else ''}"
        subject += f" ({date.today().isoformat()})"
        send_email(subject, report)
    else:
        if os.environ.get("EMAIL_EMPTY_REPORTS") == "1":
            send_email(f"ASX Update — no news ({date.today().isoformat()})", report)
        else:
            print("  (no new announcements — email skipped)")

    print("\n" + report)


if __name__ == "__main__":
    seed = "--seed" in sys.argv
    run(seed_only=seed)
