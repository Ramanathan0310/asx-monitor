# results_pack/asx_fetcher.py
# Fetch ASX announcements using marketindex.com.au (same source as monitor.py)
from __future__ import annotations
import time
from typing import List
from .models import Announcement

def fetch_announcements(ticker: str, max_pages: int = 3) -> List[Announcement]:
    """Fetch recent announcements for a ticker from marketindex.com.au."""
    try:
        from playwright.sync_api import sync_playwright
        from playwright_stealth import Stealth
    except ImportError:
        print("  [fetcher] playwright/playwright-stealth not installed")
        return []

    url = f"https://www.marketindex.com.au/asx/{ticker.lower()}/announcements"
    announcements = []
    stealth = Stealth()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
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

                link = row.query_selector("a[href*='announcement'], a[href*='pdf'], a[href*='.pdf']")
                href = ""
                if link:
                    raw = link.get_attribute("href") or ""
                    href = raw if raw.startswith("http") else f"https://www.marketindex.com.au{raw}"

                if title_text and date_text:
                    # Convert date format from marketindex (e.g. "13 Jun 2026") to DD/MM/YYYY
                    import datetime as dt
                    date_fmt = date_text  # keep as-is, models.py handles both formats

                    announcements.append(Announcement(
                        ticker=ticker.upper(),
                        title=title_text,
                        date=date_fmt,
                        time=time_text,
                        url=href,
                        pdf_url=href if href.lower().endswith(".pdf") else None,
                    ))
        except Exception as e:
            print(f"  [fetcher] Error fetching {ticker}: {e}")
        finally:
            context.close()
            browser.close()

    print(f"  [fetcher] Found {len(announcements)} announcements for {ticker}")
    return announcements
