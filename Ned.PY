# -*- coding: utf-8 -*-
#!/usr/bin/env python3
"""
Ned - News Context Agent
Pulls news for portfolio + watchlist stocks from:
  - Yahoo Finance headlines (no API key)
  - Google News RSS (per company)
  - Livewire Markets RSS
  - Stock Analysis RSS
  - YouTube channels (requires YOUTUBE_API_KEY secret)
Runs daily alongside Bob. Sends one HTML email digest.
"""

import json
import os
import re
import smtplib
import ssl
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone, timedelta
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import quote

import requests

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

LOOKBACK_HOURS = 48
MAX_ITEMS_PER_TICKER = 5
MAX_YT_RESULTS = 3

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}

YOUTUBE_CHANNELS = [
    {"handle": "market-index",              "label": "Market Index"},
    {"handle": "ASXSmallCapWrap",           "label": "ASX Small Cap Wrap"},
    {"handle": "FoolAu",                    "label": "Motley Fool Australia"},
    {"handle": "foragerfundsmanagement5034","label": "Forager Funds"},
    {"handle": "NWRCommunications",         "label": "NWR Communications"},
    {"handle": "TheStockNetwork",           "label": "The Stock Network"},
]

STATIC_RSS = [
    {"url": "https://www.livewiremarkets.com/feed",    "label": "Livewire"},
    {"url": "https://stockanalysis.com/news/feed/",    "label": "Stock Analysis"},
]

# ---------------------------------------------------------------------------
# Load tickers
# ---------------------------------------------------------------------------

def load_all_tickers() -> list[dict]:
    tickers = []
    seen = set()
    if COMPANIES_FILE.exists():
        for c in json.loads(COMPANIES_FILE.read_text()).get("companies", []):
            t = c["ticker"].upper()
            if t not in seen:
                seen.add(t)
                tickers.append({"ticker": t, "name": c.get("name", t), "list": "Portfolio"})
    if WATCHLIST_FILE.exists():
        for c in json.loads(WATCHLIST_FILE.read_text()).get("watchlist", []):
            t = c["ticker"].upper()
            if t not in seen:
                seen.add(t)
                tickers.append({"ticker": t, "name": c.get("name", t), "list": "Watchlist"})
    return tickers

# ---------------------------------------------------------------------------
# Yahoo Finance news
# ---------------------------------------------------------------------------

def fetch_yahoo_news(ticker: str, name: str) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    results = []
    for query in [f"{ticker}.AX", name]:
        try:
            url = f"https://query1.finance.yahoo.com/v1/finance/search?q={quote(query)}"
            r = requests.get(url, headers=_HEADERS, timeout=15)
            r.raise_for_status()
            data = r.json()
            for item in data.get("news", [])[:MAX_ITEMS_PER_TICKER]:
                pub = item.get("providerPublishTime", 0)
                pub_dt = datetime.fromtimestamp(pub, tz=timezone.utc) if pub else None
                if pub_dt and pub_dt < cutoff:
                    continue
                title = item.get("title", "").strip()
                if not title:
                    continue
                # Dedupe by title
                if any(r2["title"] == title for r2 in results):
                    continue
                results.append({
                    "title":     title,
                    "source":    item.get("publisher", "Yahoo Finance"),
                    "url":       item.get("link", ""),
                    "published": pub_dt.strftime("%d %b %H:%M") if pub_dt else "",
                    "channel":   "Yahoo Finance",
                })
        except Exception as e:
            print(f"    Yahoo news failed ({query}): {e}")
        if results:
            break
    return results[:MAX_ITEMS_PER_TICKER]


# ---------------------------------------------------------------------------
# Google News RSS (per company)
# ---------------------------------------------------------------------------

def fetch_google_news(ticker: str, name: str) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    results = []
    query = f"{name} ASX {ticker}"
    url = f"https://news.google.com/rss/search?q={quote(query)}&hl=en-AU&gl=AU&ceid=AU:en"
    try:
        r = requests.get(url, headers=_HEADERS, timeout=15)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        ns = {"media": "http://search.yahoo.com/mrss/"}
        for item in root.findall(".//item")[:10]:
            title  = (item.findtext("title") or "").strip()
            link   = (item.findtext("link")  or "").strip()
            pubstr = (item.findtext("pubDate") or "").strip()
            source_el = item.find("source")
            source = source_el.text if source_el is not None else "Google News"
            if not title:
                continue
            pub_dt = None
            if pubstr:
                try:
                    pub_dt = datetime.strptime(pubstr, "%a, %d %b %Y %H:%M:%S %Z").replace(tzinfo=timezone.utc)
                except Exception:
                    pass
            if pub_dt and pub_dt < cutoff:
                continue
            # Filter: must mention ticker or company name
            combined = title.lower()
            if ticker.lower() not in combined and name.lower().split()[0].lower() not in combined:
                continue
            results.append({
                "title":     title,
                "source":    source,
                "url":       link,
                "published": pub_dt.strftime("%d %b %H:%M") if pub_dt else "",
                "channel":   "Google News",
            })
            if len(results) >= MAX_ITEMS_PER_TICKER:
                break
    except Exception as e:
        print(f"    Google News RSS failed ({ticker}): {e}")
    return results


# ---------------------------------------------------------------------------
# Static RSS feeds (Livewire, Stock Analysis) - filter by company name
# ---------------------------------------------------------------------------

_static_cache: dict[str, list] = {}

def _fetch_static_rss(url: str, label: str) -> list[dict]:
    if url in _static_cache:
        return _static_cache[url]
    items = []
    try:
        r = requests.get(url, headers=_HEADERS, timeout=20)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
        for item in root.findall(".//item"):
            title  = (item.findtext("title") or "").strip()
            link   = (item.findtext("link")  or "").strip()
            pubstr = (item.findtext("pubDate") or "").strip()
            desc   = (item.findtext("description") or "").strip()
            if not title:
                continue
            pub_dt = None
            if pubstr:
                for fmt in ["%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z"]:
                    try:
                        pub_dt = datetime.strptime(pubstr, fmt)
                        if pub_dt.tzinfo is None:
                            pub_dt = pub_dt.replace(tzinfo=timezone.utc)
                        break
                    except Exception:
                        pass
            if pub_dt and pub_dt < cutoff:
                continue
            items.append({
                "title":     title,
                "source":    label,
                "url":       link,
                "published": pub_dt.strftime("%d %b %H:%M") if pub_dt else "",
                "channel":   label,
                "content":   (title + " " + desc).lower(),
            })
    except Exception as e:
        print(f"    Static RSS failed ({label}): {e}")
    _static_cache[url] = items
    return items

def fetch_static_news(ticker: str, name: str) -> list[dict]:
    results = []
    keywords = [ticker.lower(), name.lower().split()[0]]
    for feed in STATIC_RSS:
        items = _fetch_static_rss(feed["url"], feed["label"])
        for item in items:
            content = item.get("content", item["title"].lower())
            if any(kw in content for kw in keywords):
                if not any(r2["title"] == item["title"] for r2 in results):
                    results.append({k: v for k, v in item.items() if k != "content"})
    return results[:MAX_ITEMS_PER_TICKER]


# ---------------------------------------------------------------------------
# YouTube search per company
# ---------------------------------------------------------------------------

_yt_channel_ids: dict[str, str] = {}

def _resolve_channel_id(handle: str, api_key: str) -> str | None:
    if handle in _yt_channel_ids:
        return _yt_channel_ids[handle]
    try:
        url = (
            f"https://www.googleapis.com/youtube/v3/search"
            f"?part=snippet&q={quote('@' + handle)}&type=channel&key={api_key}"
        )
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        items = r.json().get("items", [])
        if items:
            cid = items[0]["snippet"]["channelId"]
            _yt_channel_ids[handle] = cid
            return cid
    except Exception as e:
        print(f"    YT channel resolve failed ({handle}): {e}")
    return None

def fetch_youtube_news(ticker: str, name: str, api_key: str) -> list[dict]:
    if not api_key:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    results = []
    query = f"{name} {ticker} ASX"
    for ch in YOUTUBE_CHANNELS:
        channel_id = _resolve_channel_id(ch["handle"], api_key)
        if not channel_id:
            continue
        try:
            url = (
                f"https://www.googleapis.com/youtube/v3/search"
                f"?part=snippet&channelId={channel_id}"
                f"&q={quote(query)}&type=video&order=date"
                f"&publishedAfter={cutoff}&maxResults={MAX_YT_RESULTS}"
                f"&key={api_key}"
            )
            r = requests.get(url, timeout=10)
            r.raise_for_status()
            for item in r.json().get("items", []):
                vid_id  = item["id"].get("videoId", "")
                snippet = item.get("snippet", {})
                title   = snippet.get("title", "").strip()
                pub     = snippet.get("publishedAt", "")
                if not title or not vid_id:
                    continue
                pub_dt = None
                if pub:
                    try:
                        pub_dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
                    except Exception:
                        pass
                results.append({
                    "title":     title,
                    "source":    ch["label"],
                    "url":       f"https://www.youtube.com/watch?v={vid_id}",
                    "published": pub_dt.strftime("%d %b %H:%M") if pub_dt else "",
                    "channel":   f"YouTube - {ch['label']}",
                })
        except Exception as e:
            print(f"    YT search failed ({ch['label']}): {e}")
        time.sleep(0.3)  # be gentle with quota
    return results[:MAX_ITEMS_PER_TICKER]


# ---------------------------------------------------------------------------
# Combine + dedupe all sources
# ---------------------------------------------------------------------------

def fetch_all_news(ticker: str, name: str, yt_api_key: str) -> list[dict]:
    all_items = []
    seen_titles = set()

    def add(items: list[dict]):
        for item in items:
            t = item["title"].lower().strip()
            # Fuzzy dedupe - skip if >80% title overlap with existing
            if any(
                len(set(t.split()) & set(s.split())) / max(len(t.split()), 1) > 0.7
                for s in seen_titles
            ):
                continue
            seen_titles.add(t)
            all_items.append(item)

    add(fetch_yahoo_news(ticker, name))
    add(fetch_google_news(ticker, name))
    add(fetch_static_news(ticker, name))
    add(fetch_youtube_news(ticker, name, yt_api_key))

    return all_items


# ---------------------------------------------------------------------------
# HTML builder
# ---------------------------------------------------------------------------

_CSS = """
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       font-size: 15px; line-height: 1.5; color: #e0e0e0;
       background: #0a0a18; max-width: 820px; margin: 24px auto; padding: 0 20px; }
h1 { font-size: 22px; color: #fff; border-bottom: 2px solid #2a2a4e; padding-bottom: 8px; }
h2 { font-size: 15px; color: #aaaadd; margin-top: 26px; text-transform: uppercase;
     letter-spacing: 1px; }
.meta { color: #444466; font-size: 13px; margin-bottom: 20px; }
.company-block { background: #10101e; border: 1px solid #1e1e3a; border-radius: 8px;
                 padding: 14px 16px; margin: 10px 0; }
.company-header { font-size: 16px; font-weight: 700; color: #fff; margin-bottom: 10px; }
.company-name { color: #666688; font-size: 13px; margin-left: 6px; font-weight: 400; }
.list-tag { font-size: 11px; color: #333366; margin-left: 8px; }
.news-item { border-left: 3px solid #2a2a4e; padding: 6px 10px; margin: 6px 0;
             background: #0c0c1a; border-radius: 0 4px 4px 0; }
.news-item:hover { border-left-color: #4444aa; }
.news-title a { color: #8888ff; text-decoration: none; font-size: 14px; }
.news-title a:hover { text-decoration: underline; color: #aaaaff; }
.news-meta { font-size: 11px; color: #444466; margin-top: 2px; }
.channel-yt  { color: #ff4444; }
.channel-gn  { color: #4488ff; }
.channel-lw  { color: #44aaff; }
.channel-yf  { color: #aa44ff; }
.channel-sa  { color: #44ffaa; }
.no-news { color: #2a2a4a; font-size: 13px; font-style: italic; padding: 4px 0; }
.summary-bar { display: flex; gap: 16px; flex-wrap: wrap; margin: 14px 0; }
.summary-item { background: #10101e; border: 1px solid #1e1e3a; border-radius: 6px;
                padding: 10px 14px; text-align: center; }
.summary-val { font-size: 22px; font-weight: 700; color: #8888ff; }
.summary-lbl { font-size: 11px; color: #444466; text-transform: uppercase; }
hr { border: none; border-top: 1px solid #1a1a30; margin: 24px 0; }
"""

def channel_class(channel: str) -> str:
    c = channel.lower()
    if "youtube" in c: return "channel-yt"
    if "google"  in c: return "channel-gn"
    if "livewire" in c: return "channel-lw"
    if "yahoo"   in c: return "channel-yf"
    return "channel-sa"

def build_html(results: list[tuple]) -> str:
    today_str = date.today().strftime("%A, %d %B %Y")
    gen_time  = datetime.now().strftime("%H:%M AEST")

    with_news  = [(e, items) for e, items in results if items]
    total_news = sum(len(items) for _, items in with_news)
    quiet      = [e for e, items in results if not items]

    summary = f"""
<div class="summary-bar">
  <div class="summary-item"><div class="summary-val">{len(results)}</div>
    <div class="summary-lbl">Stocks Scanned</div></div>
  <div class="summary-item"><div class="summary-val">{len(with_news)}</div>
    <div class="summary-lbl">With News</div></div>
  <div class="summary-item"><div class="summary-val">{total_news}</div>
    <div class="summary-lbl">Total Articles</div></div>
  <div class="summary-item"><div class="summary-val">{len(quiet)}</div>
    <div class="summary-lbl">No Coverage</div></div>
</div>"""

    blocks = ""
    for entry, items in with_news:
        ticker   = entry["ticker"]
        name     = entry["name"]
        list_tag = entry["list"]

        news_html = ""
        for item in items:
            ch_cls = channel_class(item["channel"])
            pub    = f" - {item['published']}" if item.get("published") else ""
            link   = item.get("url", "#")
            news_html += f"""
<div class="news-item">
  <div class="news-title"><a href="{link}" target="_blank">{item['title']}</a></div>
  <div class="news-meta">
    <span class="{ch_cls}">{item['channel']}</span>
    <span style="color:#333355"> | {item['source']}{pub}</span>
  </div>
</div>"""

        blocks += f"""
<div class="company-block">
  <div class="company-header">
    {ticker}
    <span class="company-name">{name}</span>
    <span class="list-tag">{list_tag}</span>
  </div>
  {news_html}
</div>"""

    quiet_html = ""
    if quiet:
        quiet_names = ", ".join(f"{e['ticker']}" for e in quiet)
        quiet_html  = f'<p class="no-news">No recent coverage: {quiet_names}</p>'

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>{_CSS}</style></head>
<body>
<h1>&#x1F4F0; Ned - News Digest</h1>
<div class="meta">{today_str} . {gen_time} . Last {LOOKBACK_HOURS}h . Sources: Yahoo Finance, Google News, Livewire, Stock Analysis, YouTube</div>
{summary}
{blocks}
{quiet_html}
<hr>
<p style="color:#1a1a30;font-size:12px">
  Ned scans Yahoo Finance, Google News RSS, Livewire Markets, Stock Analysis, and YouTube
  for news published in the last {LOOKBACK_HOURS} hours. Not financial advice.
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
    print(f"\nNed - News Digest - {date.today().isoformat()}")
    print("=" * 55)

    REPORTS_DIR.mkdir(exist_ok=True)

    yt_api_key = os.environ.get("YOUTUBE_API_KEY", "")
    if not yt_api_key:
        print("  Note: YOUTUBE_API_KEY not set - YouTube results skipped")

    tickers = load_all_tickers()
    print(f"Scanning {len(tickers)} stocks ({LOOKBACK_HOURS}h lookback)\n")

    results = []
    for i, entry in enumerate(tickers):
        ticker = entry["ticker"]
        name   = entry["name"]
        print(f"  [{i+1:3d}/{len(tickers)}] {ticker:<8} {name}")
        items = fetch_all_news(ticker, name, yt_api_key)
        print(f"    {len(items)} articles found")
        results.append((entry, items))
        time.sleep(0.5)  # be polite

    with_news  = sum(1 for _, items in results if items)
    total_news = sum(len(items) for _, items in results)
    print(f"\nDone: {with_news}/{len(tickers)} stocks have news, {total_news} total articles")

    # Save JSON
    out_data = {
        "date":  date.today().isoformat(),
        "total": len(tickers),
        "results": [
            {"ticker": e["ticker"], "name": e["name"], "list": e["list"], "count": len(items),
             "items": items}
            for e, items in results
        ]
    }
    json_path = REPORTS_DIR / f"ned_{date.today().isoformat()}.json"
    json_path.write_text(json.dumps(out_data, indent=2))
    print(f"OK JSON saved -> {json_path}")

    html = build_html(results)
    html_path = REPORTS_DIR / f"ned_{date.today().isoformat()}.html"
    html_path.write_text(html)
    print(f"OK HTML saved -> {html_path}")

    plain = f"Ned News Digest - {date.today().isoformat()}\n"
    plain += f"{with_news}/{len(tickers)} stocks with news, {total_news} total articles\n\n"
    for entry, items in results:
        if items:
            plain += f"\n{entry['ticker']} - {entry['name']}\n"
            for item in items:
                plain += f"  - {item['title']} ({item['channel']})\n"

    subject = f"Ned - {total_news} articles across {with_news} stocks ({date.today().isoformat()})"
    send_email(subject, html, plain)


if __name__ == "__main__":
    run()
