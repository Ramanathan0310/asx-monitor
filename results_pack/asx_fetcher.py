# -*- coding: utf-8 -*-
# results_pack/asx_fetcher.py
# Fetches ASX announcements from the ASX website using Playwright.
# Mimics Jimmy's shared/asx_simple_fetcher approach.
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

# ASX v2 statistics endpoint - serves HTML with announcement list
_ASX_URL = "https://www.asx.com.au/asx/v2/statistics/todayAnnouncementsForCode.do?asxCode={ticker}&timeframe=Y"


def fetch_announcements(ticker: str, max_announcements: int = 50) -> List[Announcement]:
    """Fetch announcements from ASX website using Playwright."""
    try:
        from playwright.sync_api import sync_playwright
        from playwright_stealth import Stealth
    except ImportError:
        print("  [fetcher] playwright not available")
        return []

    ticker = ticker.upper().strip()
    url = _ASX_URL.format(ticker=ticker)
    announcements = []
    stealth = Stealth()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=_UA,
            locale="en-AU",
            timezone_id="Australia/Sydney",
            viewport={"width": 1920, "height": 1080},
            extra_http_headers={"Accept-Encoding": "identity"},
        )
        stealth.apply_stealth_sync(context)
        page = context.new_page()

        try:
            print(f"  [fetcher] Loading ASX page for {ticker}...")
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(3000)

            # Parse the announcements table
            rows = page.query_selector_all("tr")
            for row in rows[:max_announcements]:
                cells = row.query_selector_all("td")
                if len(cells) < 4:
                    continue

                date_text  = cells[0].inner_text().strip()
                time_text  = cells[1].inner_text().strip()
                title_el   = cells[2]
                title_text = title_el.inner_text().strip()

                # Get the PDF link
                link = title_el.query_selector("a[href]")
                href = ""
                pdf_url = None
                if link:
                    raw = link.get_attribute("href") or ""
                    if raw.startswith("/"):
                        href = f"https://www.asx.com.au{raw}"
                    elif raw.startswith("http"):
                        href = raw
                    else:
                        href = raw

                    # ASX links are usually displayAnnouncement.do which serves PDF directly
                    if "displayAnnouncement" in href or href.lower().endswith(".pdf"):
                        pdf_url = href

                if not title_text or not date_text:
                    continue

                # Parse date - ASX uses DD/MM/YYYY
                date_fmt = date_text
                for fmt in ["%d/%m/%Y", "%d/%m/%y"]:
                    try:
                        d = dt.datetime.strptime(date_text, fmt)
                        date_fmt = d.strftime("%d/%m/%Y")
                        break
                    except Exception:
                        pass

                announcements.append(Announcement(
                    ticker=ticker,
                    title=title_text,
                    date=date_fmt,
                    time=time_text,
                    url=href or url,
                    pdf_url=pdf_url,
                ))

        except Exception as e:
            print(f"  [fetcher] ASX fetch error: {e}")
            print(f"  [fetcher] Falling back to marketindex...")
            context.close()
            browser.close()
            return _fetch_from_marketindex(ticker, max_announcements)
        finally:
            try:
                context.close()
                browser.close()
            except Exception:
                pass

    if not announcements:
        print(f"  [fetcher] No announcements from ASX, trying marketindex...")
        return _fetch_from_marketindex(ticker, max_announcements)

    print(f"  [fetcher] Got {len(announcements)} announcements from ASX for {ticker}")
    return announcements


def _fetch_from_marketindex(ticker: str, max_announcements: int = 50) -> List[Announcement]:
    """Fallback: scrape from marketindex."""
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
            user_agent=_UA,
            locale="en-AU",
            timezone_id="Australia/Sydney",
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
            print(f"  [fetcher] Marketindex error: {e}")
        finally:
            context.close()
            browser.close()

    print(f"  [fetcher] Got {len(announcements)} from marketindex for {ticker}")
    return announcements
