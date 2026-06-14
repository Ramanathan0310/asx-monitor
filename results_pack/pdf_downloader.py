# -*- coding: utf-8 -*-
# results_pack/pdf_downloader.py
# Uses Playwright download handler to get complete uncorrupted PDFs.
from __future__ import annotations
import json
import tempfile
import time
from pathlib import Path
from typing import Optional
from .models import Announcement, ResultPack

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


def _download_via_playwright(page_url: str) -> Optional[bytes]:
    """
    Navigate to marketindex announcement page and click the download button
    to trigger a proper browser download - giving us complete uncorrupted PDF.
    """
    try:
        from playwright.sync_api import sync_playwright
        from playwright_stealth import Stealth
    except ImportError:
        return None

    stealth = Stealth()
    result = [None]

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

        try:
            print(f"    [pdf] Loading page: {page_url[:80]}")
            page.goto(page_url, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(5000)

            # Try to find and click a download button
            download_selectors = [
                "a[download]",
                "a[href*='download']",
                "button[class*='download']",
                "a[class*='download']",
                "[aria-label*='download' i]",
                "[title*='download' i]",
                "a[href*='.pdf']",
            ]

            for sel in download_selectors:
                els = page.query_selector_all(sel)
                for el in els:
                    try:
                        with page.expect_download(timeout=30_000) as dl_info:
                            el.click()
                        download = dl_info.value
                        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                            tmp_path = tmp.name
                        download.save_as(tmp_path)
                        data = Path(tmp_path).read_bytes()
                        Path(tmp_path).unlink(missing_ok=True)
                        if data[:4] == b"%PDF":
                            print(f"    [pdf] Download button worked: {len(data)//1024}KB")
                            result[0] = data
                            return data
                    except Exception:
                        continue
                if result[0]:
                    break

            # If no download button worked, try fetching the data-api URL directly
            # using the browser's fetch (which has the right session/cookies)
            if not result[0]:
                print(f"    [pdf] No download button found, trying browser fetch...")
                # Extract the doc ID from page URL
                import re
                m = re.search(r'-([0-9][A-Z][0-9]+)$', page_url.rstrip('/'))
                if m:
                    doc_id = m.group(1)
                    # Extract ticker from URL
                    m2 = re.search(r'/asx/([^/]+)/announcements/', page_url)
                    ticker = m2.group(1).upper() if m2 else "ALL"
                    api_url = f"https://data-api.marketindex.com.au/api/v1/announcements/XASX:{ticker}:{doc_id}/pdf/"

                    # Use page.evaluate to fetch via browser (has correct cookies/headers)
                    js_result = page.evaluate(f"""
                        async () => {{
                            const resp = await fetch('{api_url}', {{
                                headers: {{
                                    'Accept': 'application/pdf,*/*',
                                    'Referer': 'https://www.marketindex.com.au/'
                                }}
                            }});
                            if (!resp.ok) return null;
                            const buf = await resp.arrayBuffer();
                            const bytes = new Uint8Array(buf);
                            return Array.from(bytes);
                        }}
                    """)

                    if js_result and isinstance(js_result, list):
                        data = bytes(js_result)
                        print(f"    [pdf] Browser fetch: {len(data)//1024}KB, first4: {data[:4]}")
                        if data[:4] == b"%PDF":
                            result[0] = data
                            return data

        except Exception as e:
            print(f"    [pdf] Playwright error: {e}")
        finally:
            context.close()
            browser.close()

    return result[0]


def _fetch_pdf(ann: Announcement) -> Optional[bytes]:
    url = ann.url or ann.pdf_url
    if not url:
        return None

    # Direct requests for known PDF URLs
    if url.lower().endswith(".pdf") and "marketindex" not in url:
        try:
            import requests
            r = requests.get(url, headers={"User-Agent": _UA, "Accept-Encoding": "identity"}, timeout=60)
            r.raise_for_status()
            if r.content[:4] == b"%PDF":
                print(f"    [pdf] Direct download OK ({len(r.content)//1024}KB)")
                return r.content
        except Exception as e:
            print(f"    [pdf] Direct failed: {e}")

    # Playwright download for marketindex pages
    return _download_via_playwright(url)


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

        if data and data[:4] == b"%PDF":
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
        elif data:
            print(f"  [pdf] Got data but not valid PDF: first4={data[:4]}")
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
