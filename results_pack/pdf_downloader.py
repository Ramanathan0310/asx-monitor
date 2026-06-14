# -*- coding: utf-8 -*-
# results_pack/pdf_downloader.py
# Extracts real PDF URLs from marketindex announcement pages, then downloads.
from __future__ import annotations
import io
import json
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
}

ASX_BASE = "https://www.asx.com.au"


def _resolve_pdf_url(page_url: str) -> Optional[str]:
    """
    Visit a marketindex announcement page with Playwright and extract
    the real ASX PDF download URL.
    """
    try:
        from playwright.sync_api import sync_playwright
        from playwright_stealth import Stealth
    except ImportError:
        return None

    stealth = Stealth()
    pdf_url = None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=_HEADERS["User-Agent"],
        )
        stealth.apply_stealth_sync(context)
        page = context.new_page()

        try:
            page.goto(page_url, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(2000)

            # Look for ASX PDF links in priority order
            selectors = [
                "a[href*='asx.com.au'][href*='.pdf']",
                "a[href*='asx.com.au/asx/v2/statistics']",
                "a[href*='.pdf']",
                "a[href*='asxlisted']",
                "a[href*='announcement']",
            ]
            for sel in selectors:
                links = page.query_selector_all(sel)
                for link in links:
                    href = link.get_attribute("href") or ""
                    if href and ("asx.com.au" in href or href.endswith(".pdf")):
                        if not href.startswith("http"):
                            href = f"{ASX_BASE}{href}"
                        pdf_url = href
                        break
                if pdf_url:
                    break

            # Also check for redirect via network intercept
            if not pdf_url:
                # Try clicking the main download/view button
                btn = page.query_selector("a.btn, a.button, a[class*='download'], a[class*='view']")
                if btn:
                    href = btn.get_attribute("href") or ""
                    if href:
                        if not href.startswith("http"):
                            href = f"{ASX_BASE}{href}"
                        pdf_url = href

        except Exception as e:
            print(f"    [resolve] Error: {e}")
        finally:
            context.close()
            browser.close()

    return pdf_url


def _download_pdf(url: str) -> Optional[bytes]:
    """Download a PDF from a direct URL."""
    try:
        import requests
        r = requests.get(url, headers=_HEADERS, timeout=30)
        r.raise_for_status()
        data = r.content
        if len(data) < 500:
            return None
        # Check it's actually a PDF
        if not data[:4] == b'%PDF' and "pdf" not in r.headers.get("content-type", "").lower():
            return None
        return data
    except Exception as e:
        print(f"    [download] Failed: {e}")
        return None


def _download_pdf_playwright(url: str) -> Optional[bytes]:
    """Use Playwright to download PDF (handles redirects/consent pages)."""
    try:
        from playwright.sync_api import sync_playwright
        from playwright_stealth import Stealth
    except ImportError:
        return None

    stealth = Stealth()
    pdf_bytes = None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=_HEADERS["User-Agent"],
            accept_downloads=True,
        )
        stealth.apply_stealth_sync(context)
        page = context.new_page()

        collected = []
        def on_response(response):
            ct = response.headers.get("content-type", "").lower()
            if "pdf" in ct and response.status == 200:
                try:
                    body = response.body()
                    if len(body) > 500:
                        collected.append(body)
                except Exception:
                    pass

        page.on("response", on_response)

        try:
            page.goto(url, wait_until="networkidle", timeout=30_000)
            page.wait_for_timeout(3000)
            if collected:
                pdf_bytes = collected[-1]
        except Exception as e:
            print(f"    [playwright_dl] Error: {e}")
        finally:
            context.close()
            browser.close()

    return pdf_bytes


def _extract_asx_doc_id(url: str):
    """Extract ASX document ID from marketindex URL e.g. 2A1671770."""
    import re
    m = re.search(r"-(\d[A-Z]\d+)$", url.rstrip("/"))
    return m.group(1) if m else None


def _asx_pdf_urls(doc_id: str, date_str: str) -> list:
    """Generate candidate ASX direct PDF URLs for a document ID."""
    import re
    # Parse date from DD/MM/YY or DD/MM/YYYY
    for fmt in ["%d/%m/%Y", "%d/%m/%y"]:
        try:
            import datetime as dt
            d = dt.datetime.strptime(date_str, fmt)
            ymd = d.strftime("%Y%m%d")
            break
        except Exception:
            ymd = ""

    urls = []
    if ymd:
        urls.append(f"https://www.asx.com.au/asxpdf/{ymd}/pdf/{doc_id}.pdf")
    urls.append(f"https://www.asx.com.au/asx/v2/statistics/downloadAnnexure.do?documentId={doc_id}&signedDocumentId=")
    urls.append(f"https://www.asx.com.au/asx/v2/statistics/announcements.do?documentId={doc_id}")
    return urls


def _fetch_pdf_for_announcement(ann: Announcement) -> Optional[bytes]:
    """Full pipeline: try ASX direct URLs first, then Playwright fallback."""
    url = ann.url

    # If it's already a direct PDF URL
    if url.lower().endswith(".pdf") or ("asx.com.au" in url and "pdf" in url.lower()):
        print(f"    [pdf] Direct PDF URL")
        data = _download_pdf(url)
        if data:
            return data

    # Try to extract ASX document ID from marketindex URL
    doc_id = _extract_asx_doc_id(url)
    if doc_id:
        print(f"    [pdf] Doc ID: {doc_id}")
        for asx_url in _asx_pdf_urls(doc_id, ann.date):
            print(f"    [pdf] Trying: {asx_url[:80]}")
            data = _download_pdf(asx_url)
            if data:
                print(f"    [pdf] Direct ASX download worked!")
                return data

    # Fall back to Playwright page resolution
    print(f"    [pdf] Trying Playwright resolution...")
    pdf_url = _resolve_pdf_url(url)
    if pdf_url:
        print(f"    [pdf] Resolved: {pdf_url[:80]}")
        data = _download_pdf(pdf_url)
        if data:
            return data
        return _download_pdf_playwright(pdf_url)

    # Last resort: Playwright on original page
    print(f"    [pdf] Playwright direct on page...")
    return _download_pdf_playwright(url)


def download_pack_pdfs(
    pack: ResultPack,
    output_folder: Path,
    dry_run: bool = False,
    max_bytes: int = 50 * 1024 * 1024,
) -> int:
    downloaded = 0
    for ann in pack.announcements:
        if not ann.url:
            print(f"  [pdf] No URL: {ann.title[:60]}")
            continue
        if dry_run:
            print(f"  [pdf] [DRY-RUN] {ann.title[:60]}")
            continue

        print(f"  [pdf] Fetching: {ann.title[:60]}")

        data = _fetch_pdf_for_announcement(ann)
        if data:
            if len(data) > max_bytes:
                print(f"  [pdf] Too large ({len(data)//1024}KB) - skipping")
                continue
            ann.pdf_bytes = data
            safe = "".join(c if c.isalnum() or c in ".-_ " else "_" for c in ann.title[:60])
            pdf_path = output_folder / f"{safe}.pdf"
            pdf_path.write_bytes(data)
            ann.pdf_path = str(pdf_path)
            downloaded += 1
            print(f"  [pdf] OK ({len(data)//1024}KB)")
        else:
            print(f"  [pdf] Failed")

        time.sleep(2)

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
                "pdf_downloaded": a.pdf_bytes is not None,
                "pdf_size_kb": len(a.pdf_bytes) // 1024 if a.pdf_bytes else 0,
            }
            for a in pack.announcements
        ],
    }
    path = output_folder / f"{pack.file_prefix}-metadata.json"
    path.write_text(json.dumps(meta, indent=2))
    return path
