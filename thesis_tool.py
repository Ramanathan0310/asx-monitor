#!/usr/bin/env python3
"""
Thesis tool - manages theses.json (one entry per ticker in companies.json + watchlist.json).

Usage:
  python3 thesis_tool.py init
      Create theses.json, or add entries for any new tickers. Never changes existing entries.
  python3 thesis_tool.py draft TICKER "rough notes"
      Ask Claude to turn rough notes into 3-5 clean pillars. PRINTS only.
      It never writes theses.json - copy what you like in by hand.
"""

import json
import os
import re
import sys
from pathlib import Path

BASE_DIR       = Path(__file__).parent
COMPANIES_FILE = BASE_DIR / "companies.json"
WATCHLIST_FILE = BASE_DIR / "watchlist.json"
THESES_FILE    = BASE_DIR / "theses.json"
CLAUDE_MODEL   = "claude-sonnet-5-5"

# Filled in first, in this order (portfolio order follows after).
PRIORITY = ["ALL", "WTC", "CSL", "MQG"]


def _load_dotenv():
    env_path = BASE_DIR / ".env"
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


def load_universe() -> list[dict]:
    """Portfolio first ('holding'), then watchlist-only names ('watching')."""
    out, seen = [], set()
    for file, key, status in ((COMPANIES_FILE, "companies", "holding"),
                              (WATCHLIST_FILE, "watchlist", "watching")):
        if not file.exists():
            continue
        for c in json.loads(file.read_text()).get(key, []):
            t = c["ticker"].upper()
            if t not in seen:
                seen.add(t)
                out.append({"ticker": t, "name": c.get("name", t), "status": status})
    rank = {t: i for i, t in enumerate(PRIORITY)}
    out.sort(key=lambda e: rank.get(e["ticker"], len(PRIORITY)))  # stable: keeps file order
    return out


def blank_entry(name: str, status: str) -> dict:
    return {"name": name, "status": status, "one_line_thesis": "",
            "pillars": [], "last_reviewed": ""}


def cmd_init() -> None:
    theses = json.loads(THESES_FILE.read_text()) if THESES_FILE.exists() else {}
    added = []
    for e in load_universe():
        if e["ticker"] not in theses:
            theses[e["ticker"]] = blank_entry(e["name"], e["status"])
            added.append(e["ticker"])
    THESES_FILE.write_text(json.dumps(theses, indent=2) + "\n")
    print(f"theses.json: {len(theses)} tickers ({len(added)} added: {', '.join(added) or 'none'})")


PROMPT = """You are helping an investor turn rough notes into investment thesis pillars for {name} (ASX: {ticker}).

Rough notes:
{notes}

Write 3 to 5 pillars. Each pillar is one falsifiable claim about why the business should compound value, not a slogan.
Use ONLY what the notes support. Do not add facts, numbers or claims that are not in the notes. If the notes are thin, write fewer pillars.

Return ONLY a JSON object, no other text, in this shape:
{{"one_line_thesis": "...", "pillars": [{{"id": "p1", "statement": "...", "evidence_to_watch": "what data or events would confirm it", "would_break_it": "what would show it is wrong"}}]}}
Plain ASCII only: no em dashes, en dashes or smart quotes."""


def _response_text(resp) -> str:
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()


def parse_draft(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError("no JSON object in model reply")
    data = json.loads(m.group(0))
    if not isinstance(data.get("pillars"), list):
        raise ValueError("reply has no 'pillars' list")
    return data


def cmd_draft(ticker: str, notes: str) -> None:
    ticker = ticker.upper()
    universe = {e["ticker"]: e for e in load_universe()}
    if ticker not in universe:
        sys.exit(f"{ticker} is not in companies.json or watchlist.json")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set (put it in .env)")
    import anthropic
    resp = anthropic.Anthropic().messages.create(
        model=CLAUDE_MODEL, max_tokens=2000,
        messages=[{"role": "user", "content": PROMPT.format(
            name=universe[ticker]["name"], ticker=ticker, notes=notes)}],
    )
    try:
        draft = parse_draft(_response_text(resp))
    except Exception as e:
        sys.exit(f"Could not parse the draft ({e}). Raw reply:\n{_response_text(resp)}")
    print(f'\n"{ticker}": ' + json.dumps(draft, indent=2))
    print("\nNOT saved. Copy what you want into theses.json by hand.")


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["init"]:
        cmd_init()
    elif args[:1] == ["draft"] and len(args) >= 3:
        cmd_draft(args[1], " ".join(args[2:]))
    else:
        print(__doc__)
