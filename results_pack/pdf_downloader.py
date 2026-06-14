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

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

_ASX_HEADERS = {
    "User-Agent": _UA,
    "Accept": "application/pdf,application/octet-stream,*/*",
    "Accept-Encoding": "identity",  # No compression - get raw PDF bytes
    "Referer": "https://www.asx.com.au/",
    "Origin": "https://www.asx.com.au",
}


def _extract_doc_id(url: str) -> Optional[str]:
    """Extract ASX document ID from marketindex URL."""
    m = re.search(r'-([0-9][A-Z][0-9]+)$', url.rstrip('/'))
    return m.group(1) if m else None


def _extract_date_from_ann(ann: Announcement) -> str:
    """Convert announcement date to YYYYMMDD for ASX URL."""
    import datetime as dt
    for fmt in ["%d/%m/%Y", "%d/%m/%y"]:
        try:
            d = dt.datetime.strptime(ann.date, fmt)
            return d.strftime("%Y%m%d")
        except Exception:
            pass
    return ""


def _try_asx_direct(doc_id: str, ymd: str) -> Optional[bytes]:
    """Try downloading directly from ASX announcements platform."""
    import requests

    urls = []
    if ymd:
        urls.append(f"https://announcements.asx.com.au/asxpdf/{ymd}/pdf/{doc_id}.pdf")
    urls.append(f"https://www.asx.com.au/asx/v2/statistics/downloadAnnexure.do?documentId={doc_id}&signedDocumentId=")

    for url in urls:
        try:
            print(f"    [pdf] Trying ASX: {url[:90]}")
            r = requests.get(url, headers=_ASX_HEADERS, timeout=30, allow_redirects=True)
            print(f"    [pdf] Status: {r.status_code}, CT: {r.headers.get('content-type','')[:50]}, Size: {len(r.content)//1024}KB")
            data = r.content
            if r.status_code == 200 and len(data) > 1000 and data[:4] == b"%PDF":
                print(f"    [pdf] ASX direct OK!")
                return data
        except Exception as e:
            print(f"    [pdf] ASX failed: {e}")

    return None


def _try_playwright_intercept(url: str) -> Optional[bytes]:
    """
    Use Playwright to load the page and intercept the PDF network response.
    Tries both marketindex and ASX viewer pages.
    """
    try:
        from playwright.sync_api import sync_playwright
        from playwright_stealth import Stealth
    except ImportError:
        return None

    stealth = Stealth()
    pdf_bytes_list = []

    # Also try ASX viewer URL
    doc_id = _extract_doc_id(url)
    urls_to_try = [url]
    if doc_id:
        urls_to_try.append(f"https://www.asx.com.au/markets/trade-our-cash-market/announcements/{doc_id}")

    for page_url in urls_to_try:
        if pdf_bytes_list:
            break

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=_UA,
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
                    # ONLY accept from the marketindex PDF API endpoint
                    if "data-api.marketindex.com.au/api/v1/announcements/" not in rurl:
                        return
                    if "/pdf" not in rurl.lower():
                        return
                    body = response.body()
                    print(f"    [pdf] Intercepted: {rurl[:80]}")
                    print(f"    [pdf] Size: {len(body)//1024}KB, First8: {body[:8].hex()}")
                    if len(body) > 1000 and body[:4] == b"%PDF":
                        print(f"    [pdf] Valid PDF confirmed")
                        pdf_bytes_list.append(body)
                    else:
                        print(f"    [pdf] Not a valid PDF bytes ({body[:4]}) - skipping")
                except Exception as e:
                    if "No data found" not in str(e):
                        print(f"    [pdf] Response error: {e}")

            page.on("response", on_response)

            def on_download(download):
                try:
                    import tempfile, os
                    tmp = tempfile.mktemp(suffix=".pdf")
                    download.save_as(tmp)
                    with open(tmp, "rb") as f:
                        data = f.read()
                    os.unlink(tmp)
                    print(f"    [pdf] Download: {len(data)//1024}KB, First4: {data[:4]}")
                    if data[:4] == b"%PDF":
                        pdf_bytes_list.append(data)
                except Exception as e:
                    print(f"    [pdf] Download error: {e}")

            page.on("download", on_download)

            try:
                print(f"    [pdf] Loading: {page_url[:90]}")
                page.goto(page_url, wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_timeout(8000)
            except Exception as e:
                print(f"    [pdf] Page error: {e}")
            finally:
                context.close()
                browser.close()

    return pdf_bytes_list[-1] if pdf_bytes_list else None


def _fetch_pdf_for_announcement(ann: Announcement) -> Optional[bytes]:
    """Full pipeline: ASX direct first, then Playwright."""
    url = ann.url
    if not url:
        return None

    doc_id = _extract_doc_id(url)
    ymd = _extract_date_from_ann(ann)

    # 1. Try ASX direct download (clean PDF, no compression)
    if doc_id:
        data = _try_asx_direct(doc_id, ymd)
        if data:
            return data

    # 2. Playwright interception fallback
    print(f"    [pdf] Falling back to Playwright...")
    return _try_playwright_intercept(url)


def download_pack_pdfs(
    pack: ResultPack,
    output_folder: Path,
    dry_run: bool = False,
    max_bytes: int = 50 * 1024 * 1024,
) -> int:
    downloaded = 0
    for ann in pack.announcements:
        if not ann.url:
            continue
        if dry_run:
            print(f"  [pdf] [DRY-RUN] {ann.title[:60]}")
            continue

        print(f"  [pdf] Fetching: {ann.title[:60]}")
        data = _fetch_pdf_for_announcement(ann)

        if data and (data[:3] == b"%PD" or len(data) > 10000):
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
        elif data:
            print(f"  [pdf] Invalid PDF bytes: {data[:8].hex()}")
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
                "pdf_downloaded": a.pdf_bytes is not None,
                "pdf_size_kb": len(a.pdf_bytes) // 1024 if a.pdf_bytes else 0,
            }
            for a in pack.announcements
        ],
    }
    path = output_folder / f"{pack.file_prefix}-metadata.json"
    path.write_text(json.dumps(meta, indent=2))
    return path
