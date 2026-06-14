# -*- coding: utf-8 -*-
# results_pack/asx_fetcher.py
# Fetches ASX announcements from marketindex.com.au using Playwright.
from __future__ import annotations
import datetime as dt
import re
from typing import List
from .models import Announcement

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


def fetch_announcements(ticker: str, max_announcements: int = 50) -> List[Announcement]:
    """Fetch announcements from marketindex using Playwright."""
    try:
        from playwright.sync_api import sync_playwright
        from playwright_stealth import Stealth
    except ImportError:
        print("  [fetcher] playwright not available")
        return []

    url = f"https://www.marketindex.com.au/asx/{ticker.lower()}/announcements"
    announcements = []
    stealth = Stealth()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=_UA,
            locale="en-AU",
            timezone_id="Australia/Sydney",
            viewport={"width": 1920, "height": 1080},
        )
        stealth.apply_stealth_sync(context)
        page = context.new_page()

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(3000)

            rows = page.query_selector_all("table tbody tr")
            for row in rows[:max_announcements]:
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

                if not title_text or not date_text:
                    continue

                # Parse date - marketindex uses DD/MM/YY format
                date_fmt = date_text
                for fmt in ["%d/%m/%y", "%d/%m/%Y", "%d %b %Y"]:
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
                    pdf_url=None,
                ))

        except Exception as e:
            print(f"  [fetcher] Error: {e}")
        finally:
            context.close()
            browser.close()

    print(f"  [fetcher] Got {len(announcements)} announcements for {ticker}")
    return announcements
