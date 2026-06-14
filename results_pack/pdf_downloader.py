# -*- coding: utf-8 -*-
# results_pack/pdf_downloader.py
# Uses Playwright to intercept PDF network responses from marketindex pages.
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


def _fetch_pdf_playwright(url: str) -> Optional[bytes]:
    """
    Load the marketindex announcement page with Playwright and intercept
    any PDF response from the network. This is the only reliable method
    since the PDF is loaded dynamically via JavaScript.
    """
    try:
        from playwright.sync_api import sync_playwright
        from playwright_stealth import Stealth
    except ImportError:
        print("    [pdf] playwright not available")
        return None

    stealth = Stealth()
    collected = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=_UA,
            viewport={"width": 1920, "height": 1080},
            locale="en-AU",
            timezone_id="Australia/Sydney",
            accept_downloads=True,
        )
        stealth.apply_stealth_sync(context)
        page = context.new_page()

        def on_response(response):
            ct = response.headers.get("content-type", "").lower()
            url_r = response.url.lower()
            if response.status == 200 and ("pdf" in ct or url_r.endswith(".pdf")):
                try:
                    body = response.body()
                    if len(body) > 1000 and body[:4] == b"%PDF":
                        print(f"    [pdf] Intercepted PDF: {response.url[:80]} ({len(body)//1024}KB)")
                        collected.append(body)
                except Exception as e:
                    print(f"    [pdf] Body read error: {e}")

        page.on("response", on_response)

        try:
            print(f"    [pdf] Loading: {url[:80]}")
            page.goto(url, wait_until="networkidle", timeout=45_000)
            page.wait_for_timeout(5000)

            # If no PDF yet, scroll to trigger lazy loading
            if not collected:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(3000)

        except Exception as e:
            print(f"    [pdf] Page load error: {e}")
        finally:
            context.close()
            browser.close()

    return collected[-1] if collected else None


def _fetch_pdf_for_announcement(ann: Announcement) -> Optional[bytes]:
    """Fetch PDF using Playwright network interception."""
    url = ann.url
    if not url:
        return None

    # Try direct download first if URL looks like a PDF
    if url.lower().endswith(".pdf"):
        try:
            import requests
            r = requests.get(url, headers={"User-Agent": _UA}, timeout=30)
            r.raise_for_status()
            data = r.content
            if len(data) > 1000 and data[:4] == b"%PDF":
                return data
        except Exception:
            pass

    # Use Playwright to intercept the PDF from the page
    return _fetch_pdf_playwright(url)


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
