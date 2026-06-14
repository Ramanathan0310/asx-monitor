# -*- coding: utf-8 -*-
# results_pack/pdf_downloader.py
# Downloads PDFs directly from ASX announcements platform.
from __future__ import annotations
import json
import re
import time
from pathlib import Path
from typing import Optional
from .models import Announcement, ResultPack

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/pdf,application/octet-stream,*/*",
    "Accept-Encoding": "identity",
    "Referer": "https://www.asx.com.au/",
}


def _download_direct(url: str) -> Optional[bytes]:
    """Try direct HTTP download."""
    import requests
    try:
        r = requests.get(url, headers=_HEADERS, timeout=60, allow_redirects=True)
        r.raise_for_status()
        data = r.content
        if len(data) > 1000 and data[:4] == b"%PDF":
            print(f"    [pdf] Direct download OK ({len(data)//1024}KB)")
            return data
        print(f"    [pdf] Got {len(data)//1024}KB but not a PDF (first4: {data[:4]})")
    except Exception as e:
        print(f"    [pdf] Direct download failed: {e}")
    return None


def _download_via_playwright(url: str) -> Optional[bytes]:
    """Use Playwright to download PDF via network interception."""
    try:
        from playwright.sync_api import sync_playwright
        from playwright_stealth import Stealth
    except ImportError:
        return None

    stealth = Stealth()
    collected = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=_HEADERS["User-Agent"],
            viewport={"width": 1920, "height": 1080},
            locale="en-AU",
            timezone_id="Australia/Sydney",
            accept_downloads=True,
            extra_http_headers={"Accept-Encoding": "identity"},
        )
        stealth.apply_stealth_sync(context)
        page = context.new_page()

        def on_response(response):
            try:
                if response.status != 200:
                    return
                rurl = response.url
                ct = response.headers.get("content-type", "").lower()
                if "pdf" not in ct and not rurl.lower().endswith(".pdf"):
                    return
                body = response.body()
                if len(body) > 1000 and body[:4] == b"%PDF":
                    print(f"    [pdf] Intercepted clean PDF: {rurl[:70]} ({len(body)//1024}KB)")
                    collected.append(body)
            except Exception:
                pass

        page.on("response", on_response)

        try:
            page.goto(url, wait_until="networkidle", timeout=30_000)
            page.wait_for_timeout(3000)
        except Exception as e:
            if not collected:
                print(f"    [pdf] Playwright error: {e}")
        finally:
            context.close()
            browser.close()

    return collected[-1] if collected else None


def _fetch_pdf(ann: Announcement) -> Optional[bytes]:
    """Fetch PDF for an announcement - ASX direct URL first, then Playwright."""

    # 1. If we have a direct PDF URL from ASX API, use it
    if ann.pdf_url and ann.pdf_url.startswith("http"):
        print(f"    [pdf] Trying direct URL: {ann.pdf_url[:80]}")
        data = _download_direct(ann.pdf_url)
        if data:
            return data

    # 2. If URL is a marketindex page, use Playwright interception
    if ann.url and "marketindex.com.au" in ann.url:
        print(f"    [pdf] Using Playwright for marketindex page...")
        # Use Playwright but ONLY intercept from marketindex data-api
        return _download_marketindex_playwright(ann.url)

    # 3. Try the announcement URL directly
    if ann.url and ann.url.startswith("http"):
        print(f"    [pdf] Trying announcement URL: {ann.url[:80]}")
        data = _download_direct(ann.url)
        if data:
            return data
        data = _download_via_playwright(ann.url)
        if data:
            return data

    return None


def _download_marketindex_playwright(url: str) -> Optional[bytes]:
    """Playwright specifically for marketindex - only intercepts data-api PDF endpoint."""
    try:
        from playwright.sync_api import sync_playwright
        from playwright_stealth import Stealth
    except ImportError:
        return None

    stealth = Stealth()
    collected = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=_HEADERS["User-Agent"],
            viewport={"width": 1920, "height": 1080},
            locale="en-AU",
            timezone_id="Australia/Sydney",
        )
        stealth.apply_stealth_sync(context)
        page = context.new_page()

        def on_response(response):
            try:
                rurl = response.url
                if "data-api.marketindex.com.au/api/v1/announcements/" not in rurl:
                    return
                if "/pdf" not in rurl.lower():
                    return
                if response.status != 200:
                    return
                body = response.body()
                if len(body) > 1000 and body[:4] == b"%PDF":
                    print(f"    [pdf] Marketindex API PDF: {len(body)//1024}KB")
                    collected.append(body)
            except Exception:
                pass

        page.on("response", on_response)

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(8000)
        except Exception as e:
            if not collected:
                print(f"    [pdf] Error: {e}")
        finally:
            context.close()
            browser.close()

    return collected[-1] if collected else None


def download_pack_pdfs(
    pack: ResultPack,
    output_folder: Path,
    dry_run: bool = False,
    max_bytes: int = 50 * 1024 * 1024,
) -> int:
    downloaded = 0
    for ann in pack.announcements:
        if not ann.url and not ann.pdf_url:
            print(f"  [pdf] No URL: {ann.title[:60]}")
            continue
        if dry_run:
            print(f"  [pdf] [DRY-RUN] {ann.title[:60]}")
            continue

        print(f"  [pdf] Fetching: {ann.title[:60]}")
        data = _fetch_pdf(ann)

        if data:
            if len(data) > max_bytes:
                print(f"  [pdf] Too large ({len(data)//1024}KB) - skipping")
                continue
            # Diagnostic: check PDF structure
            print(f"  [pdf] First 20 bytes: {data[:20]}")
            print(f"  [pdf] Last 20 bytes: {data[-20:]}")
            # Count pages via simple regex
            import re
            page_count = len(re.findall(b"/Page\b", data))
            print(f"  [pdf] Approximate page count: {page_count}")
            # Check for content streams
            has_content = b"/Contents" in data or b"stream" in data[:5000]
            print(f"  [pdf] Has content streams: {has_content}")

            ann.pdf_bytes = data
            safe = "".join(c if c.isalnum() or c in ".-_ " else "_" for c in ann.title[:60])
            pdf_path = output_folder / f"{safe}.pdf"
            pdf_path.write_bytes(data)
            ann.pdf_path = str(pdf_path)
            downloaded += 1
            print(f"  [pdf] OK ({len(data)//1024}KB)")
        else:
            print(f"  [pdf] Failed")

        time.sleep(1)

    return downloaded


def save_pack_metadata(pack: ResultPack, output_folder: Path) -> Path:
    meta = {
        "ticker": pack.ticker,
        "company_name": pack.company_name,
        "result_date": pack.result_date,
        "result_type": pack.result_type,
        "announcements": [
            {
                "title": a.title,
                "date": a.date,
                "url": a.url,
                "pdf_url": a.pdf_url,
                "pdf_downloaded": a.pdf_bytes is not None,
                "pdf_size_kb": len(a.pdf_bytes) // 1024 if a.pdf_bytes else 0,
            }
            for a in pack.announcements
        ],
    }
    path = output_folder / f"{pack.file_prefix}-metadata.json"
    path.write_text(json.dumps(meta, indent=2))
    return path
