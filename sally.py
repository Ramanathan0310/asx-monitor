# -*- coding: utf-8 -*-
#!/usr/bin/env python3
"""
Sunday Sally - ASX 52-Week High Screen
Screens portfolio + watchlist for stocks near 52-week highs.
Flags: within 5% of high (HOT) + top 20% of range (WARM).
Runs Friday evening via GitHub Actions.
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

BASE_DIR       = Path(__file__).parent
COMPANIES_FILE = BASE_DIR / "companies.json"
WATCHLIST_FILE = BASE_DIR / "watchlist.json"
REPORTS_DIR    = BASE_DIR / "reports"

# HOT: within this % of 52-week high
HIGH_THRESHOLD_PCT = 5.0
# WARM: top X% of 52-week range
RANGE_TOP_PCT      = 20.0

# ---------------------------------------------------------------------------
# Load tickers (reuse same logic as wally)
# ---------------------------------------------------------------------------

def load_portfolio() -> list[dict]:
    if not COMPANIES_FILE.exists():
        return []
    data = json.loads(COMPANIES_FILE.read_text())
    return [
        {"ticker": c["ticker"].upper() + ".AX",
         "name": c.get("name", c["ticker"]),
         "list": "Portfolio"}
        for c in data.get("companies", [])
    ]

def load_watchlist() -> list[dict]:
    if not WATCHLIST_FILE.exists():
        print("  watchlist.json not found - screening portfolio only")
        return []
    data = json.loads(WATCHLIST_FILE.read_text())
    return [
        {"ticker": c["ticker"].upper() + ".AX",
         "name": c.get("name", c["ticker"]),
         "list": "Watchlist"}
        for c in data.get("watchlist", [])
    ]

def dedupe(entries: list[dict]) -> list[dict]:
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
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        info = t.info
        price = info.get("currentPrice") or info.get("regularMarketPrice")
        low   = info.get("fiftyTwoWeekLow")
        high  = info.get("fiftyTwoWeekHigh")
        if not all([price, low, high]):
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

def calc_distance_to_high(price: float, high: float) -> float:
    """% below 52-week high. 0 = at the high."""
    if high <= 0:
        return 999.0
    return round(((high - price) / high) * 100, 2)

def calc_position(price: float, low: float, high: float) -> float:
    """Position in 52-week range as 0-100%. 100 = at high."""
    rng = high - low
    if rng <= 0:
        return 50.0
    return round(((price - low) / rng) * 100, 1)

def is_hot(dist_to_high: float, threshold: float) -> bool:
    return dist_to_high <= threshold

def is_warm(pos: float, top_pct: float) -> bool:
    return pos >= (100 - top_pct)

# ---------------------------------------------------------------------------
# Chart generation
# ---------------------------------------------------------------------------

def make_range_chart(entry: dict, data: dict) -> str:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        price = data["price"]
        low   = data["low"]
        high  = data["high"]
        pos   = calc_position(price, low, high)
        dist  = calc_distance_to_high(price, high)
        name  = entry["name"]
        ticker = entry["ticker"].replace(".AX", "")

        fig, ax = plt.subplots(figsize=(7, 1.4))
        fig.patch.set_facecolor("#1a1a2e")
        ax.set_facecolor("#1a1a2e")

        # Background track
        ax.barh(0, 100, left=0, height=0.5, color="#2d2d4e", zorder=1)

        # Filled bar - green tones for high screen (opposite of Wally)
        color = "#2ecc71" if dist <= HIGH_THRESHOLD_PCT else "#f39c12" if pos >= 80 else "#3498db"
        ax.barh(0, pos, left=0, height=0.5, color=color, zorder=2, alpha=0.85)

        # Current price marker
        ax.plot([pos, pos], [-0.35, 0.35], color="white", linewidth=2.5, zorder=3)

        # Labels
        ax.text(0,   0.45, f"${low:.2f}",   color="#aaaaaa", fontsize=8,   ha="left",   va="bottom")
        ax.text(100, 0.45, f"${high:.2f}",  color="#aaaaaa", fontsize=8,   ha="right",  va="bottom")
        ax.text(pos, 0.45, f"${price:.2f}", color="white",   fontsize=8.5, ha="center", va="bottom", fontweight="bold")

        ax.text(50, -0.55,
                f"{ticker} - {name}   |   {dist:.1f}% below 52w high   |   Range position: {pos:.0f}%",
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
       background: #0f1a0f; max-width: 800px; margin: 24px auto; padding: 0 20px; }
h1   { font-size: 22px; color: #ffffff; border-bottom: 2px solid #1a4a1a; padding-bottom: 8px; }
h2   { font-size: 16px; color: #80cc80; margin-top: 28px; text-transform: uppercase;
       letter-spacing: 1px; }
.card { background: #0f1f0f; border: 1px solid #1a3a1a; border-radius: 8px;
        padding: 14px 16px; margin: 10px 0; }
.card.hot  { border-left: 4px solid #2ecc71; }
.card.warm { border-left: 4px solid #f39c12; }
.ticker { font-size: 18px; font-weight: 700; color: #ffffff; }
.name   { color: #6a8a6a; font-size: 13px; margin-left: 8px; }
.badge  { display: inline-block; font-size: 11px; font-weight: 600;
          padding: 2px 8px; border-radius: 10px; margin-left: 10px; }
.badge-hot  { background: #0a3a1a; color: #2ecc71; }
.badge-warm { background: #3a2a00; color: #f39c12; }
.list-tag { font-size: 11px; color: #3a5a3a; margin-left: 6px; }
.stats { display: flex; gap: 0; margin: 12px 0 10px; border-top: 1px solid #1a3a1a; padding-top: 10px; }
.stat  { text-align: center; flex: 1; min-width: 80px; padding: 0 8px; border-right: 1px solid #1a3a1a; }
.stat:last-child { border-right: none; }
.stat-val { font-size: 18px; font-weight: 700; color: #ffffff; display: block; }
.stat-lbl { font-size: 10px; color: #3a6a3a; text-transform: uppercase; letter-spacing: 0.5px; display: block; margin-top: 2px; }
.chart { width: 100%; margin: 6px 0 2px; border-radius: 4px; }
.quiet { color: #2a4a2a; font-size: 13px; font-style: italic; }
table { border-collapse: collapse; width: 100%; margin: 12px 0; font-size: 13px; }
th, td { padding: 6px 12px; text-align: left; border-bottom: 1px solid #1a3a1a; }
th { color: #6a8a6a; font-weight: 600; }
tr:hover td { background: #0f250f; }
hr { border: none; border-top: 1px solid #1a3a1a; margin-top: 32px; }
"""

def build_html(hot: list, warm: list, quiet_count: int, total: int) -> str:
    today_str = date.today().strftime("%A, %d %B %Y")
    gen_time  = datetime.now().strftime("%H:%M AEST")
    flagged   = hot + warm

    def card_html(f: dict, is_hot_card: bool) -> str:
        dist = f["dist_to_high"]
        pos  = f["pos"]
        cls  = "hot" if is_hot_card else "warm"

        if is_hot_card:
            badge = f'<span class="badge badge-hot">&#x1F7E2; {dist:.1f}% below high</span>'
        else:
            badge = f'<span class="badge badge-warm">&#x1F7E1; Top {100-pos:.0f}% of range</span>'

        list_tag   = f'<span class="list-tag">{f["list"]}</span>'
        chart_html = ""
        if f.get("chart_b64"):
            chart_html = f'<img class="chart" src="data:image/png;base64,{f["chart_b64"]}" />'

        p    = f["data"]["price"]
        low  = f["data"]["low"]
        high = f["data"]["high"]

        return f"""
<div class="card {cls}">
  <span class="ticker">{f['ticker'].replace('.AX','')}</span>
  <span class="name">{f['name']}</span>
  {list_tag}
  {badge}
  {chart_html}
  <div class="stats">
    <div class="stat"><span class="stat-val">${p:.2f}</span><span class="stat-lbl">Current</span></div>
    <div class="stat"><span class="stat-val">${low:.2f}</span><span class="stat-lbl">52w Low</span></div>
    <div class="stat"><span class="stat-val">${high:.2f}</span><span class="stat-lbl">52w High</span></div>
    <div class="stat"><span class="stat-val">{pos:.0f}%</span><span class="stat-lbl">Range Pos</span></div>
    <div class="stat"><span class="stat-val">{dist:.1f}%</span><span class="stat-lbl">Below High</span></div>
  </div>
</div>"""

    sections = ""
    if hot:
        sections += f'<h2>&#x1F7E2; Within {HIGH_THRESHOLD_PCT:.0f}% of 52-week high ({len(hot)})</h2>\n'
        sections += "\n".join(card_html(f, True) for f in hot)
    if warm:
        sections += f'<h2>&#x1F7E1; Top {RANGE_TOP_PCT:.0f}% of 52-week range ({len(warm)})</h2>\n'
        sections += "\n".join(card_html(f, False) for f in warm)
    if not flagged:
        sections += '<p class="quiet">No stocks near 52-week highs this week.</p>'

    if flagged:
        all_flagged = sorted(flagged, key=lambda x: x["pos"], reverse=True)
        rows = "\n".join(
            f"<tr><td><b>{f['ticker'].replace('.AX','')}</b></td>"
            f"<td>{f['name']}</td>"
            f"<td>{f['list']}</td>"
            f"<td>${f['data']['price']:.2f}</td>"
            f"<td>${f['data']['low']:.2f}</td>"
            f"<td>${f['data']['high']:.2f}</td>"
            f"<td>{f['pos']:.0f}%</td>"
            f"<td>{f['dist_to_high']:.1f}%</td></tr>"
            for f in all_flagged
        )
        table = f"""
<h2>Summary</h2>
<table>
<tr><th>Ticker</th><th>Name</th><th>List</th><th>Price</th>
    <th>52w Low</th><th>52w High</th><th>Range Pos</th><th>Below High</th></tr>
{rows}
</table>"""
    else:
        table = ""

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>{_CSS}</style></head>
<body>
<h1>&#x1F4C8; Sunday Sally - 52-Week High Screen</h1>
<p style="color:#3a6a3a">{today_str} . {gen_time} . {len(flagged)} flagged / {total} screened</p>
{sections}
{table}
<hr>
<p style="color:#1a3a1a;font-size:12px">
  Sally screens {total} stocks for proximity to 52-week highs.
  HOT: within {HIGH_THRESHOLD_PCT:.0f}% of high. WARM: top {RANGE_TOP_PCT:.0f}% of range.
  Data via Yahoo Finance. Not financial advice.
</p>
</body></html>"""

# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def send_email(subject: str, html_body: str, plain_body: str) -> bool:
    to_addr   = os.environ.get("EMAIL_TO")
    from_addr = os.environ.get("EMAIL_FROM")
    host      = os.environ.get("SMTP_HOST")
    port      = int(os.environ.get("SMTP_PORT", "587"))
    user      = os.environ.get("SMTP_USER")
    password  = os.environ.get("SMTP_PASSWORD")

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

def run():
    print(f"\nSunday Sally - 52-Week High Screen - {date.today().isoformat()}")
    print("=" * 55)

    REPORTS_DIR.mkdir(exist_ok=True)

    portfolio  = load_portfolio()
    watchlist  = load_watchlist()
    all_stocks = dedupe(portfolio + watchlist)

    print(f"Screening {len(all_stocks)} stocks "
          f"({len(portfolio)} portfolio + {len(watchlist)} watchlist, deduped)\n")

    hot        = []
    warm_only  = []
    quiet_count = 0

    for i, entry in enumerate(all_stocks):
        ticker = entry["ticker"]
        name   = entry["name"]
        print(f"  [{i+1:3d}/{len(all_stocks)}] {ticker:<12} {name}")

        data = fetch_price_data(ticker)
        if not data:
            quiet_count += 1
            continue

        dist = calc_distance_to_high(data["price"], data["high"])
        pos  = calc_position(data["price"], data["low"], data["high"])
        flag = {**entry, "data": data, "dist_to_high": dist, "pos": pos, "chart_b64": ""}

        if is_hot(dist, HIGH_THRESHOLD_PCT):
            print(f"    GREEN {dist:.1f}% below 52w high - HOT")
            flag["chart_b64"] = make_range_chart(entry, data)
            hot.append(flag)
        elif is_warm(pos, RANGE_TOP_PCT):
            print(f"    YELLOW top {100-pos:.0f}% of range - WARM")
            flag["chart_b64"] = make_range_chart(entry, data)
            warm_only.append(flag)
        else:
            print(f"    range pos {pos:.0f}%")

    # Sort HOT by closest to high, WARM by range position
    hot.sort(key=lambda x: x["dist_to_high"])
    warm_only.sort(key=lambda x: x["pos"], reverse=True)

    print(f"\nResults: {len(hot)} HOT, {len(warm_only)} WARM, "
          f"{len(all_stocks)-len(hot)-len(warm_only)-quiet_count} clear, "
          f"{quiet_count} no data")

    # Save JSON
    output = {
        "date":  date.today().isoformat(),
        "total": len(all_stocks),
        "hot":   [{k: v for k, v in f.items() if k != "chart_b64"} for f in hot],
        "warm":  [{k: v for k, v in f.items() if k != "chart_b64"} for f in warm_only],
    }
    out_path = REPORTS_DIR / f"sally_{date.today().isoformat()}.json"
    out_path.write_text(json.dumps(output, indent=2))
    print(f"OK JSON saved -> {out_path}")

    html  = build_html(hot, warm_only, quiet_count, len(all_stocks))
    plain = "\n".join(
        f"{f['ticker'].replace('.AX','')} | {f['name']} | "
        f"${f['data']['price']:.2f} | {f['dist_to_high']:.1f}% below 52w high | pos {f['pos']:.0f}%"
        for f in hot + warm_only
    ) or "No stocks near 52-week highs this week."

    html_path = REPORTS_DIR / f"sally_{date.today().isoformat()}.html"
    html_path.write_text(html)
    print(f"OK HTML saved -> {html_path}")

    subject = (
        f"Sally - {len(hot)} HOT, {len(warm_only)} WARM near 52w highs ({date.today().isoformat()})"
        if hot or warm_only
        else f"Sally - All clear ({date.today().isoformat()})"
    )
    send_email(subject, html, plain)


if __name__ == "__main__":
    run()
