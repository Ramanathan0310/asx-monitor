# -*- coding: utf-8 -*-
#!/usr/bin/env python3
"""
Wally - ASX 52-Week Low Screener
Screens companies.json + watchlist.json for stocks within N% of 52-week low.
Sends an HTML email with inline range charts for each flagged stock.
Runs Tue + Fri via GitHub Actions.
"""

import base64
import io
import json
import os
import smtplib
import ssl
import sys
from datetime import date, datetime
from email.message import EmailMessage
from pathlib import Path

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
# Config
# ---------------------------------------------------------------------------

BASE_DIR        = Path(__file__).parent
COMPANIES_FILE  = BASE_DIR / "companies.json"
WATCHLIST_FILE  = BASE_DIR / "watchlist.json"   # broader watchlist
REPORTS_DIR     = BASE_DIR / "reports"

# Flag stocks within this % of 52-week low
THRESHOLD_PCT   = 5.0

# ---------------------------------------------------------------------------
# Load tickers
# ---------------------------------------------------------------------------

def load_portfolio() -> list[dict]:
    """Load from companies.json (same file monitor.py uses)."""
    if not COMPANIES_FILE.exists():
        return []
    data = json.loads(COMPANIES_FILE.read_text())
    return [
        {"ticker": c["ticker"].upper() + ".AX", "name": c.get("name", c["ticker"]), "list": "Portfolio"}
        for c in data.get("companies", [])
    ]


def load_watchlist() -> list[dict]:
    """
    Load from watchlist.json - broader universe beyond your portfolio.
    Format:
    {
      "watchlist": [
        {"ticker": "WES", "name": "Wesfarmers"},
        {"ticker": "CSL", "name": "CSL Ltd"}
      ]
    }
    If the file doesn't exist, creates a sample one.
    """
    if not WATCHLIST_FILE.exists():
        sample = {
            "watchlist": [
                {"ticker": "WES",  "name": "Wesfarmers"},
                {"ticker": "CSL",  "name": "CSL Ltd"},
                {"ticker": "CBA",  "name": "Commonwealth Bank"},
                {"ticker": "NAB",  "name": "NAB"},
                {"ticker": "WBC",  "name": "Westpac"},
                {"ticker": "ANZ",  "name": "ANZ"},
                {"ticker": "MQG",  "name": "Macquarie Group"},
                {"ticker": "WOW",  "name": "Woolworths"},
                {"ticker": "COL",  "name": "Coles Group"},
                {"ticker": "TCL",  "name": "Transurban"},
                {"ticker": "SHL",  "name": "Sonic Healthcare"},
                {"ticker": "RMD",  "name": "ResMed"},
                {"ticker": "COH",  "name": "Cochlear"},
                {"ticker": "XRO",  "name": "Xero"},
                {"ticker": "REA",  "name": "REA Group"},
                {"ticker": "SEK",  "name": "Seek"},
                {"ticker": "CPU",  "name": "Computershare"},
                {"ticker": "ASX",  "name": "ASX Ltd"},
                {"ticker": "AMC",  "name": "Amcor"},
                {"ticker": "JHX",  "name": "James Hardie"}
            ]
        }
        WATCHLIST_FILE.write_text(json.dumps(sample, indent=2))
        print(f"Created {WATCHLIST_FILE} - edit it to customise your watchlist.")
    data = json.loads(WATCHLIST_FILE.read_text())
    return [
        {"ticker": c["ticker"].upper() + ".AX", "name": c.get("name", c["ticker"]), "list": "Watchlist"}
        for c in data.get("watchlist", [])
    ]


def dedupe(entries: list[dict]) -> list[dict]:
    """Remove duplicate tickers, portfolio takes priority."""
    seen = set()
    result = []
    for e in entries:
        if e["ticker"] not in seen:
            seen.add(e["ticker"])
            result.append(e)
    return result

# ---------------------------------------------------------------------------
# Price data
# ---------------------------------------------------------------------------

def fetch_price_data(ticker: str) -> dict | None:
    """
    Fetch current price, 52-week low/high from Yahoo Finance.
    ticker should be in Yahoo format e.g. 'BHP.AX'
    Returns dict or None on failure.
    """
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        info = t.info

        price = info.get("currentPrice") or info.get("regularMarketPrice")
        low   = info.get("fiftyTwoWeekLow")
        high  = info.get("fiftyTwoWeekHigh")

        if not all([price, low, high]):
            # Fallback: calculate from history
            hist = t.history(period="1y")
            if hist.empty:
                return None
            price = float(hist["Close"].iloc[-1])
            low   = float(hist["Low"].min())
            high  = float(hist["High"].max())

        return {
            "price": round(float(price), 3),
            "low":   round(float(low),   3),
            "high":  round(float(high),  3),
        }
    except Exception as e:
        print(f"  Price fetch failed ({ticker}): {e}")
        return None


def calc_distance(price: float, low: float) -> float:
    """% distance above 52-week low. 0 = at the low."""
    if low <= 0:
        return 999.0
    return round(((price - low) / low) * 100, 2)


def calc_position(price: float, low: float, high: float) -> float:
    """Position in 52-week range as 0-100%. 0=at low, 100=at high."""
    rng = high - low
    if rng <= 0:
        return 50.0
    return round(((price - low) / rng) * 100, 1)

# ---------------------------------------------------------------------------
# Chart generation
# ---------------------------------------------------------------------------

def make_range_chart(entry: dict, data: dict) -> str:
    """
    Generate a horizontal 52-week range bar chart.
    Returns base64-encoded PNG string for embedding in HTML email.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
        import numpy as np

        price  = data["price"]
        low    = data["low"]
        high   = data["high"]
        pos    = calc_position(price, low, high)
        dist   = calc_distance(price, low)
        name   = entry["name"]
        ticker = entry["ticker"].replace(".AX", "")

        fig, ax = plt.subplots(figsize=(7, 1.4))
        fig.patch.set_facecolor("#1a1a2e")
        ax.set_facecolor("#1a1a2e")

        # Background track
        ax.barh(0, 100, left=0, height=0.5, color="#2d2d4e", zorder=1)

        # Filled portion (low to current)
        color = "#e74c3c" if dist <= 5 else "#f39c12" if dist <= 15 else "#2ecc71"
        ax.barh(0, pos, left=0, height=0.5, color=color, zorder=2, alpha=0.85)

        # Current price marker
        ax.plot([pos, pos], [-0.35, 0.35], color="white", linewidth=2.5, zorder=3)

        # Labels
        ax.text(0,   0.45, f"${low:.2f}",   color="#aaaaaa", fontsize=8,  ha="left",   va="bottom")
        ax.text(100, 0.45, f"${high:.2f}",  color="#aaaaaa", fontsize=8,  ha="right",  va="bottom")
        ax.text(pos, 0.45, f"${price:.2f}", color="white",   fontsize=8.5, ha="center", va="bottom", fontweight="bold")

        # Title
        ax.text(50, -0.55,
                f"{ticker} - {name}   |   {dist:.1f}% above 52w low   |   Range position: {pos:.0f}%",
                color="#cccccc", fontsize=8, ha="center", va="top")

        ax.set_xlim(-2, 102)
        ax.set_ylim(-0.9, 0.9)
        ax.axis("off")
        plt.tight_layout(pad=0.2)

        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=130, bbox_inches="tight",
                    facecolor=fig.get_facecolor())
        plt.close(fig)
        buf.seek(0)
        return base64.b64encode(buf.read()).decode()

    except Exception as e:
        print(f"  Chart failed ({entry['ticker']}): {e}")
        return ""

# ---------------------------------------------------------------------------
# Email builder
# ---------------------------------------------------------------------------

_CSS = """
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       font-size: 15px; line-height: 1.5; color: #e0e0e0;
       background: #0f0f1a; max-width: 800px; margin: 24px auto; padding: 0 20px; }
h1   { font-size: 22px; color: #ffffff; border-bottom: 2px solid #3a3a6e; padding-bottom: 8px; }
h2   { font-size: 16px; color: #a0a0cc; margin-top: 28px; text-transform: uppercase;
       letter-spacing: 1px; }
.card { background: #1a1a2e; border: 1px solid #2d2d4e; border-radius: 8px;
        padding: 14px 16px; margin: 10px 0; }
.card.hot { border-left: 4px solid #e74c3c; }
.card.warm { border-left: 4px solid #f39c12; }
.ticker { font-size: 18px; font-weight: 700; color: #ffffff; }
.name   { color: #8888aa; font-size: 13px; margin-left: 8px; }
.badge  { display: inline-block; font-size: 11px; font-weight: 600;
          padding: 2px 8px; border-radius: 10px; margin-left: 10px; }
.badge-hot  { background: #4a1010; color: #ff6b6b; }
.badge-warm { background: #4a3010; color: #ffa040; }
.badge-ok   { background: #103020; color: #40cc80; }
.stats { display: flex; gap: 24px; margin: 8px 0 10px; }
.stat  { text-align: center; }
.stat-val  { font-size: 20px; font-weight: 700; color: #ffffff; }
.stat-lbl  { font-size: 11px; color: #6666aa; text-transform: uppercase; }
.chart { width: 100%; margin: 6px 0 2px; border-radius: 4px; }
.list-tag { font-size: 11px; color: #555588; margin-left: 6px; }
.quiet { color: #555566; font-size: 13px; font-style: italic; }
table { border-collapse: collapse; width: 100%; margin: 12px 0; font-size: 13px; }
th, td { padding: 6px 12px; text-align: left; border-bottom: 1px solid #2a2a3e; }
th { color: #8888aa; font-weight: 600; }
tr:hover td { background: #1e1e35; }
"""

def build_html(flagged: list[dict], quiet_count: int, total: int, threshold: float) -> str:
    today_str = date.today().strftime("%A, %d %B %Y")
    gen_time  = datetime.now().strftime("%H:%M AEST")

    hot  = [f for f in flagged if f["dist"] <= threshold]
    warm = [f for f in flagged if threshold < f["dist"] <= threshold * 3]

    def card_html(f: dict) -> str:
        dist    = f["dist"]
        cls     = "hot" if dist <= threshold else "warm"
        badge   = (f'<span class="badge badge-hot">🔴 {dist:.1f}% above low</span>'
                   if dist <= threshold else
                   f'<span class="badge badge-warm">🟡 {dist:.1f}% above low</span>')
        list_tag = f'<span class="list-tag">{f["list"]}</span>'
        chart_html = ""
        if f.get("chart_b64"):
            chart_html = f'<img class="chart" src="data:image/png;base64,{f["chart_b64"]}" />'

        p    = f["data"]["price"]
        low  = f["data"]["low"]
        high = f["data"]["high"]
        pos  = f["pos"]

        return f"""
<div class="card {cls}">
  <span class="ticker">{f['ticker'].replace('.AX','')}</span>
  <span class="name">{f['name']}</span>
  {list_tag}
  {badge}
  {chart_html}
  <div class="stats">
    <div class="stat"><div class="stat-val">${p:.2f}</div><div class="stat-lbl">Current</div></div>
    <div class="stat"><div class="stat-val">${low:.2f}</div><div class="stat-lbl">52w Low</div></div>
    <div class="stat"><div class="stat-val">${high:.2f}</div><div class="stat-lbl">52w High</div></div>
    <div class="stat"><div class="stat-val">{pos:.0f}%</div><div class="stat-lbl">Range pos</div></div>
  </div>
</div>"""

    sections = ""

    if hot:
        sections += f'<h2>🔴 Within {threshold:.0f}% of 52-week low ({len(hot)})</h2>\n'
        sections += "\n".join(card_html(f) for f in hot)

    if warm:
        sections += f'<h2>🟡 Within {threshold*3:.0f}% of 52-week low ({len(warm)})</h2>\n'
        sections += "\n".join(card_html(f) for f in warm)

    if not flagged:
        sections += '<p class="quiet">✅ No stocks near 52-week lows today - all clear.</p>'

    # Summary table
    if flagged:
        rows = "\n".join(
            f"<tr><td><b>{f['ticker'].replace('.AX','')}</b></td>"
            f"<td>{f['name']}</td>"
            f"<td>{f['list']}</td>"
            f"<td>${f['data']['price']:.2f}</td>"
            f"<td>${f['data']['low']:.2f}</td>"
            f"<td>${f['data']['high']:.2f}</td>"
            f"<td>{f['dist']:.1f}%</td>"
            f"<td>{f['pos']:.0f}%</td></tr>"
            for f in sorted(flagged, key=lambda x: x["dist"])
        )
        table = f"""
<h2>Summary</h2>
<table>
<tr><th>Ticker</th><th>Name</th><th>List</th><th>Price</th>
    <th>52w Low</th><th>52w High</th><th>Dist to Low</th><th>Range Pos</th></tr>
{rows}
</table>"""
    else:
        table = ""

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>{_CSS}</style></head>
<body>
<h1>📉 Wally - 52-Week Low Screen</h1>
<p style="color:#666688">{today_str} . {gen_time} . {len(flagged)} flagged / {total} screened</p>
{sections}
{table}
<hr style="border-color:#2a2a3e;margin-top:32px">
<p style="color:#444466;font-size:12px">
  Wally screens {total} stocks ({total - quiet_count} with data) for proximity to 52-week lows.
  Threshold: within {threshold:.0f}% shown as 🔴, within {threshold*3:.0f}% shown as 🟡.
  Data via Yahoo Finance. Not financial advice.
</p>
</body></html>"""

# ---------------------------------------------------------------------------
# Email sender
# ---------------------------------------------------------------------------

def send_email(subject: str, html_body: str, plain_body: str) -> bool:
    to_addr  = os.environ.get("EMAIL_TO")
    from_addr = os.environ.get("EMAIL_FROM")
    host     = os.environ.get("SMTP_HOST")
    port     = int(os.environ.get("SMTP_PORT", "587"))
    user     = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")

    if not all([to_addr, from_addr, host, user, password]):
        print("  (email skipped - SMTP env vars not set)")
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"]    = from_addr
    msg["To"]      = to_addr
    msg.set_content(plain_body)
    msg.add_alternative(html_body, subtype="html")

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
        print(f"  OK Emailed to {to_addr}")
        return True
    except Exception as e:
        print(f"  Email failed: {e}")
        return False

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(threshold: float = THRESHOLD_PCT):
    print(f"\nWally - 52-Week Low Screen - {date.today().isoformat()}")
    print("=" * 55)

    REPORTS_DIR.mkdir(exist_ok=True)

    # Combine portfolio + watchlist (deduped, portfolio first)
    portfolio  = load_portfolio()
    watchlist  = load_watchlist()
    all_stocks = dedupe(portfolio + watchlist)

    print(f"Screening {len(all_stocks)} stocks "
          f"({len(portfolio)} portfolio + {len(watchlist)} watchlist, deduped)")
    print(f"Threshold: within {threshold:.0f}% of 52-week low\n")

    flagged     = []
    quiet_count = 0

    for i, entry in enumerate(all_stocks):
        ticker = entry["ticker"]
        name   = entry["name"]
        print(f"  [{i+1:3d}/{len(all_stocks)}] {ticker:<12} {name}")

        data = fetch_price_data(ticker)
        if not data:
            quiet_count += 1
            continue

        dist = calc_distance(data["price"], data["low"])
        pos  = calc_position(data["price"], data["low"], data["high"])

        # Flag if within 3x threshold (hot + warm zones)
        if dist <= threshold * 3:
            flag = {**entry, "data": data, "dist": dist, "pos": pos, "chart_b64": ""}
            if dist <= threshold:
                print(f"    🔴 {dist:.1f}% above 52w low - FLAGGED HOT")
            else:
                print(f"    🟡 {dist:.1f}% above 52w low - flagged warm")

            # Generate chart
            chart_b64 = make_range_chart(entry, data)
            flag["chart_b64"] = chart_b64
            flagged.append(flag)
        else:
            print(f"    OK  {dist:.1f}% above low")

    # Sort: hottest first
    flagged.sort(key=lambda x: x["dist"])

    hot_count = sum(1 for f in flagged if f["dist"] <= threshold)
    print(f"\nResults: {hot_count} HOT (≤{threshold:.0f}%), "
          f"{len(flagged)-hot_count} WARM, "
          f"{len(all_stocks)-len(flagged)-quiet_count} clear, "
          f"{quiet_count} no data")

    # Save JSON output
    output = {
        "date":      date.today().isoformat(),
        "threshold": threshold,
        "total":     len(all_stocks),
        "flagged": [
            {k: v for k, v in f.items() if k != "chart_b64"}
            for f in flagged
        ]
    }
    out_path = REPORTS_DIR / f"wally_{date.today().isoformat()}.json"
    out_path.write_text(json.dumps(output, indent=2))
    print(f"OK JSON saved → {out_path}")

    # Build + send email
    html  = build_html(flagged, quiet_count, len(all_stocks), threshold)
    plain = "\n".join(
        f"{f['ticker'].replace('.AX','')} | {f['name']} | "
        f"${f['data']['price']:.2f} | {f['dist']:.1f}% above 52w low"
        for f in flagged
    ) or "No stocks near 52-week lows today."

    hot_count = sum(1 for f in flagged if f["dist"] <= threshold)
    if flagged:
        subject = (
            f"📉 Wally - {hot_count} HOT, {len(flagged)-hot_count} WARM "
            f"({date.today().isoformat()})"
        )
    else:
        subject = f"📉 Wally - All clear ({date.today().isoformat()})"

    send_email(subject, html, plain)

    # Always save HTML report too
    html_path = REPORTS_DIR / f"wally_{date.today().isoformat()}.html"
    html_path.write_text(html)
    print(f"OK HTML saved → {html_path}")


if __name__ == "__main__":
    thresh = float(sys.argv[1]) if len(sys.argv) > 1 else THRESHOLD_PCT
    run(threshold=thresh)
