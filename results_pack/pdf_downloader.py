# -*- coding: utf-8 -*-
# results_pack/pdf_downloader.py
# Downloads PDFs from ASX displayAnnouncement URLs or via Playwright.
from __future__ import annotations
import json
import time
from pathlib import Path
from typing import Optional
from .models import Announcement, ResultPack

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

_HEADERS = {
    "User-Agent": _UA,
    "Accept": "application/pdf,*/*",
    "Accept-Encoding": "identity",
    "Referer": "https://www.asx.com.au/",
}


def _download_direct(url: str) -> Optional[bytes]:
    """Download PDF via requests."""
    import requests
    try:
        r = requests.get(url, headers=_HEADERS, timeout=60, allow_redirects=True)
        r.raise_for_status()
        data = r.content
        if len(data) > 1000 and data[:4] == b"%PDF":
            print(f"    [pdf] Direct OK ({len(data)//1024}KB)")
            return data
        print(f"    [pdf] Not a PDF (first4: {data[:4]}, size: {len(data)//1024}KB)")
    except Exception as e:
        print(f"    [pdf] Direct failed: {e}")
    return None


def _download_playwright(url: str) -> Optional[bytes]:
    """Download PDF using Playwright - handles consent pages and redirects."""
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
            user_agent=_UA,
            viewport={"width": 1920, "height": 1080},
            accept_downloads=True,
            extra_http_headers={"Accept-Encoding": "identity"},
        )
        stealth.apply_stealth_sync(context)
        page = context.new_page()

        collected = []

        def on_response(response):
            try:
                if response.status != 200:
                    return
                ct = response.headers.get("content-type", "").lower()
                if "pdf" not in ct and "octet-stream" not in ct:
                    return
                body = response.body()
                if len(body) > 1000 and body[:4] == b"%PDF":
                    print(f"    [pdf] Intercepted: {response.url[:70]} ({len(body)//1024}KB)")
                    collected.append(body)
            except Exception:
                pass

        page.on("response", on_response)

        try:
            print(f"    [pdf] Playwright loading: {url[:80]}")
            page.goto(url, wait_until="networkidle", timeout=45_000)
            page.wait_for_timeout(3000)

            # Try clicking consent/accept buttons if present
            if not collected:
                for sel in ["button:has-text('Accept')", "button:has-text('I agree')",
                            "input[type='submit']", "a:has-text('View')"]:
                    try:
                        btn = page.query_selector(sel)
                        if btn:
                            btn.click()
                            page.wait_for_timeout(3000)
                            if collected:
                                break
                    except Exception:
                        continue

        except Exception as e:
            if not collected:
                print(f"    [pdf] Playwright error: {e}")
        finally:
            if collected:
                pdf_bytes = collected[-1]
            context.close()
            browser.close()

    return pdf_bytes


def _fetch_pdf(ann: Announcement) -> Optional[bytes]:
    """Fetch PDF - try direct download first, then Playwright."""
    # Use pdf_url if available (from ASX fetcher), otherwise fall back to url
    url = ann.pdf_url or ann.url
    if not url:
        return None

    # Direct download (works for ASX displayAnnouncement URLs)
    print(f"    [pdf] Trying: {url[:80]}")
    data = _download_direct(url)
    if data:
        return data

    # Playwright fallback
    print(f"    [pdf] Trying Playwright...")
    data = _download_playwright(url)
    if data:
        return data

    # If url != pdf_url, try the other one too
    other = ann.url if url == ann.pdf_url else ann.pdf_url
    if other and other != url:
        print(f"    [pdf] Trying alternate URL: {other[:80]}")
        data = _download_direct(other)
        if data:
            return data
        data = _download_playwright(other)
        if data:
            return data

    return None


def download_pack_pdfs(
    pack: ResultPack,
    output_folder: Path,
    dry_run: bool = False,
    max_bytes: int = 50 * 1024 * 1024,
) -> int:
    downloaded = 0
    for ann in pack.announcements:
        if not ann.url and not ann.pdf_url:
            continue
        if dry_run:
            print(f"  [pdf] [DRY-RUN] {ann.title[:60]}")
            continue

        print(f"  [pdf] Fetching: {ann.title[:60]}")
        data = _fetch_pdf(ann)

        if data:
            if len(data) > max_bytes:
                print(f"  [pdf] Too large ({len(data)//1024}KB)")
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
