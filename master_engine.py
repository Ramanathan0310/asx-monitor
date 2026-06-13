# -*- coding: utf-8 -*-
#!/usr/bin/env python3
"""
Master Engine - Weekly Super Investor Briefing
Runs Saturday morning, aggregates Bob + Wally + Sally outputs
into one prioritised email digest.
"""

import json
import os
import smtplib
import ssl
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

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

BASE_DIR     = Path(__file__).parent
REPORTS_DIR  = BASE_DIR / "reports"

# ---------------------------------------------------------------------------
# Load latest report JSONs
# ---------------------------------------------------------------------------

def find_latest(prefix: str, days_back: int = 7) -> dict | None:
    """Find most recent JSON report for a given agent prefix within N days."""
    best = None
    best_date = None
    for f in REPORTS_DIR.glob(f"{prefix}_*.json"):
        try:
            d = date.fromisoformat(f.stem.split("_", 1)[1])
            if d >= date.today() - timedelta(days=days_back):
                if best_date is None or d > best_date:
                    best = f
                    best_date = d
        except Exception:
            continue
    if best:
        try:
            return json.loads(best.read_text())
        except Exception:
            return None
    return None


def load_bob_reports(days_back: int = 7) -> list[dict]:
    """Load all monitor (Bob) reports from last N days."""
    reports = []
    cutoff = date.today() - timedelta(days=days_back)
    for f in sorted(REPORTS_DIR.glob("report_*.md")):
        try:
            d = date.fromisoformat(f.stem.split("_", 1)[1])
            if d >= cutoff:
                reports.append({"date": d.isoformat(), "content": f.read_text()})
        except Exception:
            continue
    return reports


# ---------------------------------------------------------------------------
# Priority scoring
# ---------------------------------------------------------------------------

def score_wally_flag(f: dict) -> int:
    """Higher score = more urgent."""
    dist = f.get("dist", 999)
    if dist <= 2:   return 10
    if dist <= 5:   return 8
    if dist <= 10:  return 5
    return 2

def score_sally_flag(f: dict) -> int:
    dist = f.get("dist_to_high", 999)
    pos  = f.get("pos", 0)
    if dist <= 2:   return 9
    if dist <= 5:   return 7
    if pos >= 95:   return 6
    if pos >= 80:   return 3
    return 1

def priority_label(score: int) -> str:
    if score >= 9:  return "URGENT"
    if score >= 7:  return "HIGH"
    if score >= 5:  return "MEDIUM"
    return "WATCH"

def priority_color(score: int) -> str:
    if score >= 9:  return "#ff4444"
    if score >= 7:  return "#ff9900"
    if score >= 5:  return "#ffdd00"
    return "#aaaaaa"

# ---------------------------------------------------------------------------
# Extract Bob highlights from markdown
# ---------------------------------------------------------------------------

def extract_bob_highlights(reports: list[dict]) -> list[dict]:
    """Pull key lines from Bob's markdown reports."""
    highlights = []
    for r in reports:
        lines = r["content"].splitlines()
        current_company = None
        for line in lines:
            # Company header
            if line.startswith("## ") and "new" in line.lower():
                current_company = line.replace("##", "").strip()
            # Tier lines - grab HIGH IMPACT ones
            if current_company and any(t in line for t in [
                "RESULTS", "ACQUISITION", "CAPITAL_RAISE", "SUBSTANTIAL_HOLDER"
            ]):
                highlights.append({
                    "date":    r["date"],
                    "company": current_company,
                    "line":    line.strip(),
                })
    return highlights[:20]  # cap at 20


# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------

_CSS = """
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       font-size: 15px; line-height: 1.5; color: #e0e0e0;
       background: #0a0a14; max-width: 820px; margin: 24px auto; padding: 0 20px; }
h1 { font-size: 24px; color: #ffffff; border-bottom: 2px solid #333366;
     padding-bottom: 10px; margin-bottom: 6px; }
h2 { font-size: 17px; color: #aaaadd; margin-top: 32px; text-transform: uppercase;
     letter-spacing: 1px; border-left: 4px solid #333366; padding-left: 10px; }
h3 { font-size: 14px; color: #8888bb; margin: 20px 0 8px; }
.meta { color: #444466; font-size: 13px; margin-bottom: 24px; }
.priority-bar { display: flex; gap: 16px; flex-wrap: wrap; margin: 16px 0 24px; }
.priority-item { background: #12121e; border: 1px solid #222244; border-radius: 8px;
                 padding: 12px 16px; flex: 1; min-width: 160px; }
.priority-val { font-size: 28px; font-weight: 700; }
.priority-lbl { font-size: 11px; color: #555577; text-transform: uppercase;
                letter-spacing: 0.5px; margin-top: 2px; }
.badge { display: inline-block; font-size: 11px; font-weight: 700;
         padding: 2px 8px; border-radius: 10px; margin-right: 6px; }
.card { background: #12121e; border: 1px solid #222244; border-radius: 8px;
        padding: 12px 16px; margin: 8px 0; }
.card-urgent { border-left: 4px solid #ff4444; }
.card-high   { border-left: 4px solid #ff9900; }
.card-medium { border-left: 4px solid #ffdd00; }
.card-watch  { border-left: 4px solid #555577; }
.ticker { font-size: 16px; font-weight: 700; color: #fff; }
.name   { color: #666688; font-size: 13px; margin-left: 6px; }
.detail { color: #8888aa; font-size: 13px; margin-top: 4px; }
table { border-collapse: collapse; width: 100%; margin: 12px 0; font-size: 13px; }
th, td { padding: 6px 12px; text-align: left; border-bottom: 1px solid #1e1e32; }
th { color: #666688; font-weight: 600; }
tr:hover td { background: #14142a; }
.section-bob  { border-left-color: #4488ff; }
.section-wally{ border-left-color: #ff4444; }
.section-sally{ border-left-color: #44cc44; }
.all-clear { color: #333355; font-size: 13px; font-style: italic; padding: 8px 0; }
hr { border: none; border-top: 1px solid #1e1e32; margin: 28px 0; }
.footer { color: #222244; font-size: 12px; margin-top: 24px; }
"""

# ---------------------------------------------------------------------------
# HTML builder
# ---------------------------------------------------------------------------

def build_html(
    wally_data: dict | None,
    sally_data: dict | None,
    bob_highlights: list[dict],
) -> str:
    today_str = date.today().strftime("%A, %d %B %Y")
    gen_time  = datetime.now().strftime("%H:%M AEST")

    # --- Aggregate all flags ---
    wally_hot  = wally_data.get("flagged", []) if wally_data else []
    wally_hot  = [f for f in wally_hot if f.get("dist", 999) <= 5]
    wally_warm = [f for f in (wally_data.get("flagged", []) if wally_data else [])
                  if f.get("dist", 999) > 5]

    sally_hot  = sally_data.get("hot",  []) if sally_data else []
    sally_warm = sally_data.get("warm", []) if sally_data else []

    total_flags = len(wally_hot) + len(wally_warm) + len(sally_hot) + len(sally_warm)
    urgent = sum(1 for f in wally_hot + sally_hot
                 if score_wally_flag(f) >= 9 or score_sally_flag(f) >= 9)

    # --- Priority summary bar ---
    priority_bar = f"""
<div class="priority-bar">
  <div class="priority-item">
    <div class="priority-val" style="color:#ff4444">{len(wally_hot)}</div>
    <div class="priority-lbl">Near 52w Low (HOT)</div>
  </div>
  <div class="priority-item">
    <div class="priority-val" style="color:#ff9900">{len(wally_warm)}</div>
    <div class="priority-lbl">Near 52w Low (WARM)</div>
  </div>
  <div class="priority-item">
    <div class="priority-val" style="color:#44cc44">{len(sally_hot)}</div>
    <div class="priority-lbl">Near 52w High (HOT)</div>
  </div>
  <div class="priority-item">
    <div class="priority-val" style="color:#f39c12">{len(sally_warm)}</div>
    <div class="priority-lbl">Near 52w High (WARM)</div>
  </div>
  <div class="priority-item">
    <div class="priority-val" style="color:#4488ff">{len(bob_highlights)}</div>
    <div class="priority-lbl">Key Announcements</div>
  </div>
</div>"""

    # --- Action items (top priority across all agents) ---
    action_items = []
    for f in wally_hot:
        score = score_wally_flag(f)
        action_items.append({
            "score":   score,
            "ticker":  f["ticker"].replace(".AX", ""),
            "name":    f["name"],
            "source":  "Wally",
            "detail":  f"${f['data']['price']:.2f} - {f['dist']:.1f}% above 52w low of ${f['data']['low']:.2f}",
            "label":   priority_label(score),
            "color":   priority_color(score),
        })
    for f in sally_hot:
        score = score_sally_flag(f)
        action_items.append({
            "score":   score,
            "ticker":  f["ticker"].replace(".AX", ""),
            "name":    f["name"],
            "source":  "Sally",
            "detail":  f"${f['data']['price']:.2f} - {f['dist_to_high']:.1f}% below 52w high of ${f['data']['high']:.2f}",
            "label":   priority_label(score),
            "color":   priority_color(score),
        })
    action_items.sort(key=lambda x: x["score"], reverse=True)

    action_html = ""
    if action_items:
        cards = ""
        for a in action_items[:10]:
            cls = f"card-{a['label'].lower()}"
            source_color = "#ff4444" if a["source"] == "Wally" else "#44cc44"
            cards += f"""
<div class="card {cls}">
  <span class="ticker">{a['ticker']}</span>
  <span class="name">{a['name']}</span>
  <span class="badge" style="background:#1a1a2e;color:{source_color}">{a['source']}</span>
  <span class="badge" style="background:#1a1a2e;color:{a['color']}">{a['label']}</span>
  <div class="detail">{a['detail']}</div>
</div>"""
        action_html = f'<h2>&#x26A1; Priority Action Items</h2>\n{cards}'
    else:
        action_html = '<h2>&#x26A1; Priority Action Items</h2>\n<p class="all-clear">No urgent flags this week.</p>'

    # --- Bob section ---
    bob_html = '<h2 style="border-left-color:#4488ff">&#x1F4E2; Bob - Key Announcements (Last 7 Days)</h2>\n'
    if bob_highlights:
        for h in bob_highlights[:10]:
            bob_html += f"""
<div class="card">
  <div style="color:#4488ff;font-size:11px;margin-bottom:4px">{h['date']} - {h['company']}</div>
  <div class="detail">{h['line']}</div>
</div>"""
    else:
        bob_html += '<p class="all-clear">No high-impact announcements in the last 7 days.</p>'

    # --- Wally section ---
    wally_html = '<h2 style="border-left-color:#ff4444">&#x1F4C9; Wally - 52-Week Low Watch</h2>\n'
    if wally_data:
        wally_date = wally_data.get("date", "")
        wally_total = wally_data.get("total", 0)
        wally_html += f'<p class="detail">Last run: {wally_date} - {wally_total} stocks screened</p>\n'
        if wally_hot or wally_warm:
            all_wally = sorted(wally_hot + wally_warm, key=lambda x: x.get("dist", 999))
            rows = "\n".join(
                f"<tr><td><b>{f['ticker'].replace('.AX','')}</b></td>"
                f"<td>{f['name']}</td>"
                f"<td>{f['list']}</td>"
                f"<td>${f['data']['price']:.2f}</td>"
                f"<td>${f['data']['low']:.2f}</td>"
                f"<td>{f['dist']:.1f}%</td>"
                f"<td>{'HOT' if f['dist'] <= 5 else 'WARM'}</td></tr>"
                for f in all_wally
            )
            wally_html += f"""<table>
<tr><th>Ticker</th><th>Name</th><th>List</th><th>Price</th><th>52w Low</th><th>Dist</th><th>Flag</th></tr>
{rows}</table>"""
        else:
            wally_html += '<p class="all-clear">No stocks near 52-week lows.</p>'
    else:
        wally_html += '<p class="all-clear">No Wally report found for this week.</p>'

    # --- Sally section ---
    sally_html = '<h2 style="border-left-color:#44cc44">&#x1F4C8; Sally - 52-Week High Watch</h2>\n'
    if sally_data:
        sally_date = sally_data.get("date", "")
        sally_total = sally_data.get("total", 0)
        sally_html += f'<p class="detail">Last run: {sally_date} - {sally_total} stocks screened</p>\n'
        all_sally = sally_hot + sally_warm
        if all_sally:
            rows = "\n".join(
                f"<tr><td><b>{f['ticker'].replace('.AX','')}</b></td>"
                f"<td>{f['name']}</td>"
                f"<td>{f['list']}</td>"
                f"<td>${f['data']['price']:.2f}</td>"
                f"<td>${f['data']['high']:.2f}</td>"
                f"<td>{f['dist_to_high']:.1f}%</td>"
                f"<td>{f['pos']:.0f}%</td>"
                f"<td>{'HOT' if f['dist_to_high'] <= 5 else 'WARM'}</td></tr>"
                for f in sorted(all_sally, key=lambda x: x.get("pos", 0), reverse=True)
            )
            sally_html += f"""<table>
<tr><th>Ticker</th><th>Name</th><th>List</th><th>Price</th><th>52w High</th><th>Below High</th><th>Range Pos</th><th>Flag</th></tr>
{rows}</table>"""
        else:
            sally_html += '<p class="all-clear">No stocks near 52-week highs.</p>'
    else:
        sally_html += '<p class="all-clear">No Sally report found for this week.</p>'

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>{_CSS}</style></head>
<body>
<h1>&#x1F9E0; Master Engine - Weekly Investor Briefing</h1>
<div class="meta">{today_str} . {gen_time} . {total_flags} total flags across all agents</div>
{priority_bar}
{action_html}
<hr>
{bob_html}
<hr>
{wally_html}
<hr>
{sally_html}
<hr>
<div class="footer">
  Master Engine aggregates Bob (announcements), Wally (52w lows), and Sally (52w highs).
  Reads JSON reports from the last 7 days. Not financial advice.
</div>
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
    print(f"\nMaster Engine - {date.today().isoformat()}")
    print("=" * 55)

    REPORTS_DIR.mkdir(exist_ok=True)

    print("Loading agent reports...")
    wally_data     = find_latest("wally")
    sally_data     = find_latest("sally")
    bob_reports    = load_bob_reports()
    bob_highlights = extract_bob_highlights(bob_reports)

    print(f"  Wally: {'found ' + wally_data['date'] if wally_data else 'not found'}")
    print(f"  Sally: {'found ' + sally_data['date'] if sally_data else 'not found'}")
    print(f"  Bob:   {len(bob_reports)} reports, {len(bob_highlights)} highlights")

    html = build_html(wally_data, sally_data, bob_highlights)

    # Save HTML
    out_path = REPORTS_DIR / f"master_{date.today().isoformat()}.html"
    out_path.write_text(html)
    print(f"OK HTML saved -> {out_path}")

    # Build subject
    wally_hot  = len([f for f in (wally_data.get("flagged", []) if wally_data else [])
                      if f.get("dist", 999) <= 5])
    sally_hot  = len(sally_data.get("hot", []) if sally_data else [])
    plain = f"Master Engine Weekly Briefing - {date.today().isoformat()}\n"
    plain += f"Wally HOT: {wally_hot} | Sally HOT: {sally_hot} | Bob highlights: {len(bob_highlights)}"

    subject = f"Master Engine - Wally {wally_hot} HOT, Sally {sally_hot} HOT ({date.today().isoformat()})"
    send_email(subject, html, plain)


if __name__ == "__main__":
    run()
