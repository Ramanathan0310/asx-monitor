# -*- coding: utf-8 -*-
# results_pack/asx_fetcher.py
# Fetches ASX announcements directly from ASX API and downloads clean PDFs.
from __future__ import annotations
import datetime as dt
import re
import time
from typing import List, Optional
from .models import Announcement

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.asx.com.au/",
    "Origin": "https://www.asx.com.au",
}


def _parse_asx_date(date_str: str) -> str:
    """Convert ASX API date to DD/MM/YYYY format."""
    # ASX API returns: "2026-05-13T00:00:00+00:00" or "13/05/2026"
    for fmt in ["%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"]:
        try:
            d = dt.datetime.strptime(date_str[:19], fmt[:len(fmt)])
            return d.strftime("%d/%m/%Y")
        except Exception:
            pass
    # Already in DD/MM/YYYY
    if re.match(r'\d{2}/\d{2}/\d{4}', date_str):
        return date_str
    return date_str


def fetch_announcements(ticker: str, max_announcements: int = 50) -> List[Announcement]:
    """
    Fetch recent announcements from ASX API directly.
    Returns list of Announcement objects with direct PDF URLs.
    """
    import requests

    ticker = ticker.upper().strip()
    announcements = []

    # ASX v2 API - returns JSON with announcement list including PDF URLs
    url = (
        f"https://www.asx.com.au/asx/1/company/{ticker}/announcements"
        f"?count={max_announcements}&market=ASX"
    )

    print(f"  [fetcher] Fetching from ASX API: {url[:80]}")

    try:
        r = requests.get(url, headers=_HEADERS, timeout=30)
        r.raise_for_status()
        data = r.json()
        items = data.get("data", [])
        print(f"  [fetcher] Got {len(items)} announcements from ASX API")

        for item in items:
            # Extract fields
            title    = item.get("header", "").strip()
            date_raw = item.get("date_time", "") or item.get("announcement_date", "")
            doc_id   = item.get("id", "") or item.get("document_release_date", "")

            # Build date
            date_fmt = _parse_asx_date(date_raw) if date_raw else ""
            time_str = ""
            if "T" in date_raw:
                try:
                    t = dt.datetime.fromisoformat(date_raw.replace("Z", "+00:00"))
                    # Convert UTC to AEST
                    aest = t + dt.timedelta(hours=10)
                    time_str = aest.strftime("%H:%M")
                except Exception:
                    pass

            # Build PDF URL - ASX serves PDFs at a predictable URL
            pdf_url = item.get("url", "") or item.get("document_url", "")
            if not pdf_url and doc_id:
                # Try constructing from announcement date + ID
                date_for_url = ""
                if date_raw:
                    try:
                        d = dt.datetime.fromisoformat(date_raw[:10])
                        date_for_url = d.strftime("%Y%m%d")
                    except Exception:
                        pass
                if date_for_url:
                    pdf_url = f"https://announcements.asx.com.au/asxpdf/{date_for_url}/pdf/{doc_id}.pdf"

            if title and date_fmt:
                announcements.append(Announcement(
                    ticker=ticker,
                    title=title,
                    date=date_fmt,
                    time=time_str,
                    url=pdf_url or "",
                    pdf_url=pdf_url if pdf_url else None,
                ))

    except Exception as e:
        print(f"  [fetcher] ASX API failed: {e}")
        print(f"  [fetcher] Falling back to marketindex scraping...")
        return _fetch_from_marketindex(ticker)

    if not announcements:
        print(f"  [fetcher] No announcements from ASX API, trying marketindex...")
        return _fetch_from_marketindex(ticker)

    return announcements


def _fetch_from_marketindex(ticker: str) -> List[Announcement]:
    """Fallback: scrape from marketindex (used by monitor.py)."""
    try:
        from playwright.sync_api import sync_playwright
        from playwright_stealth import Stealth
    except ImportError:
        return []

    url = f"https://www.marketindex.com.au/asx/{ticker.lower()}/announcements"
    announcements = []
    stealth = Stealth()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=_HEADERS["User-Agent"],
            locale="en-AU",
            timezone_id="Australia/Sydney",
        )
        stealth.apply_stealth_sync(context)
        page = context.new_page()

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(3000)

            rows = page.query_selector_all("table tbody tr")
            for row in rows[:50]:
                cells = row.query_selector_all("td")
                if len(cells) < 3:
                    continue
                date_text  = cells[0].inner_text().strip()
                time_text  = cells[1].inner_text().strip() if len(cells) > 1 else ""
                title_text = cells[2].inner_text().strip()

                link = row.query_selector("a[href]")
                href = ""
                if link:
                    raw = link.get_attribute("href") or ""
                    href = raw if raw.startswith("http") else f"https://www.marketindex.com.au{raw}"

                if title_text and date_text:
                    # Parse marketindex date format
                    date_fmt = date_text
                    for fmt in ["%d %b %Y", "%d/%m/%Y", "%d/%m/%y"]:
                        try:
                            d = dt.datetime.strptime(date_text, fmt)
                            date_fmt = d.strftime("%d/%m/%Y")
                            break
                        except Exception:
                            pass

                    announcements.append(Announcement(
                        ticker=ticker.upper(),
                        title=title_text,
                        date=date_fmt,
                        time=time_text,
                        url=href,
                        pdf_url=None,  # No direct PDF URL from marketindex
                    ))
        except Exception as e:
            print(f"  [fetcher] Marketindex fallback error: {e}")
        finally:
            context.close()
            browser.close()

    print(f"  [fetcher] Got {len(announcements)} from marketindex")
    return announcements
