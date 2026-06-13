# -*- coding: utf-8 -*-
# results_pack/pdf_downloader.py
# Uses Playwright to bypass ASX consent pages and download PDFs.
from __future__ import annotations
import io
import json
import tempfile
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

def _fetch_pdf_requests(url: str) -> Optional[bytes]:
    """Try direct requests download first (fast path)."""
    if not url:
        return None
    try:
        import requests
        r = requests.get(url, headers=_HEADERS, timeout=30)
        r.raise_for_status()
        ct = r.headers.get("content-type", "").lower()
        if "pdf" not in ct and not url.lower().endswith(".pdf"):
            return None
        data = r.content
        return data if len(data) > 1000 else None
    except Exception:
        return None


def _fetch_pdf_playwright(url: str) -> Optional[bytes]:
    """Use Playwright to bypass consent pages and get PDF."""
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

        try:
            # Intercept PDF responses
            pdf_data = []

            def handle_response(response):
                ct = response.headers.get("content-type", "").lower()
                if "pdf" in ct and response.status == 200:
                    try:
                        body = response.body()
                        if len(body) > 1000:
                            pdf_data.append(body)
                    except Exception:
                        pass

            page.on("response", handle_response)
            page.goto(url, wait_until="networkidle", timeout=30_000)
            page.wait_for_timeout(3000)

            # Check if page has a PDF link to click
            if not pdf_data:
                pdf_links = page.query_selector_all("a[href*='.pdf'], a[href*='pdf']")
                for link in pdf_links[:3]:
                    try:
                        href = link.get_attribute("href") or ""
                        if href:
                            if not href.startswith("http"):
                                href = f"https://www.asx.com.au{href}"
                            page.goto(href, wait_until="networkidle", timeout=20_000)
                            page.wait_for_timeout(2000)
                            if pdf_data:
                                break
                    except Exception:
                        continue

            if pdf_data:
                pdf_bytes = pdf_data[-1]

        except Exception as e:
            print(f"    [pdf] Playwright error: {e}")
        finally:
            context.close()
            browser.close()

    return pdf_bytes


def _fetch_pdf(url: str) -> Optional[bytes]:
    """Fetch PDF - try requests first, fall back to Playwright."""
    if not url:
        return None

    # Try direct download first
    data = _fetch_pdf_requests(url)
    if data:
        return data

    # Fall back to Playwright
    print(f"    [pdf] Direct download failed, trying Playwright...")
    data = _fetch_pdf_playwright(url)
    return data


def download_pack_pdfs(
    pack: ResultPack,
    output_folder: Path,
    dry_run: bool = False,
    max_bytes: int = 50 * 1024 * 1024,
) -> int:
    downloaded = 0
    for ann in pack.announcements:
        url = ann.pdf_url or ann.url
        if not url:
            print(f"  [pdf] No URL for: {ann.title[:60]}")
            continue
        if dry_run:
            print(f"  [pdf] [DRY-RUN] Would download: {ann.title[:60]}")
            continue

        print(f"  [pdf] Fetching: {ann.title[:60]}")
        print(f"    URL: {url[:80]}")

        data = _fetch_pdf(url)
        if data:
            if len(data) > max_bytes:
                print(f"  [pdf] Skipping oversized PDF ({len(data)//1024}KB)")
                continue
            ann.pdf_bytes = data
            safe = "".join(c if c.isalnum() or c in ".-_ " else "_" for c in ann.title[:60])
            pdf_path = output_folder / f"{safe}.pdf"
            pdf_path.write_bytes(data)
            ann.pdf_path = str(pdf_path)
            downloaded += 1
            print(f"  [pdf] OK ({len(data)//1024}KB)")
        else:
            print(f"  [pdf] Failed to download")

        time.sleep(1)  # be polite between downloads

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
