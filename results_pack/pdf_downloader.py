# -*- coding: utf-8 -*-
# results_pack/pdf_downloader.py
# Uses Playwright download handler to get real PDF bytes from marketindex.
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


def _build_api_url(page_url: str) -> Optional[str]:
    """Build marketindex data-api PDF URL from announcement page URL."""
    m = re.search(r'/asx/([^/]+)/announcements/[^/]+-([0-9][A-Z][0-9]+)$', page_url.rstrip('/'))
    if m:
        ticker = m.group(1).upper()
        doc_id = m.group(2)
        return f"https://data-api.marketindex.com.au/api/v1/announcements/XASX:{ticker}:{doc_id}/pdf/"
    return None


def _fetch_via_playwright_download(url: str) -> Optional[bytes]:
    """
    Use Playwright's download interception to get the actual PDF file.
    This triggers the browser's native download mechanism which gives
    us the real uncompressed PDF bytes.
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

        # Track all responses to find the PDF
        pdf_bytes_list = []

        def on_response(response):
            try:
                rurl = response.url
                ct = response.headers.get("content-type", "").lower()
                status = response.status

                if status != 200:
                    return

                # Check if this looks like a PDF response
                is_pdf_url = any(x in rurl.lower() for x in ['/pdf', '.pdf', 'pdf/'])
                is_pdf_ct = 'pdf' in ct or 'octet-stream' in ct

                if is_pdf_url or is_pdf_ct:
                    try:
                        body = response.body()
                        print(f"    [pdf] Response: {rurl[:80]}")
                        print(f"    [pdf] Size: {len(body)//1024}KB, CT: {ct}, First4: {body[:4]}")

                        # Try to handle various encodings
                        import gzip
                        import zlib

                        # Standard PDF
                        if body[:4] == b"%PDF":
                            print(f"    [pdf] Standard PDF detected")
                            pdf_bytes_list.append(body)
                            return

                        # Try gzip
                        try:
                            decompressed = gzip.decompress(body)
                            if decompressed[:4] == b"%PDF":
                                print(f"    [pdf] Gzip PDF, decompressed: {len(decompressed)//1024}KB")
                                pdf_bytes_list.append(decompressed)
                                return
                        except Exception:
                            pass

                        # Try zlib deflate
                        try:
                            decompressed = zlib.decompress(body)
                            if decompressed[:4] == b"%PDF":
                                print(f"    [pdf] Zlib PDF, decompressed: {len(decompressed)//1024}KB")
                                pdf_bytes_list.append(decompressed)
                                return
                        except Exception:
                            pass

                        # Try zlib with negative wbits (raw deflate)
                        try:
                            decompressed = zlib.decompress(body, -15)
                            if decompressed[:4] == b"%PDF":
                                print(f"    [pdf] Raw deflate PDF: {len(decompressed)//1024}KB")
                                pdf_bytes_list.append(decompressed)
                                return
                        except Exception:
                            pass

                        # Try brotli if available
                        try:
                            import brotli
                            decompressed = brotli.decompress(body)
                            if decompressed[:4] == b"%PDF":
                                print(f"    [pdf] Brotli PDF: {len(decompressed)//1024}KB")
                                pdf_bytes_list.append(decompressed)
                                return
                        except Exception:
                            pass

                        # Store raw bytes as last resort
                        if len(body) > 10000:
                            print(f"    [pdf] Storing raw bytes (unrecognised encoding)")
                            pdf_bytes_list.append(body)

                    except Exception as e:
                        print(f"    [pdf] Body error: {e}")
            except Exception:
                pass

        page.on("response", on_response)

        # Also handle downloads
        def on_download(download):
            try:
                import tempfile, os
                tmp = tempfile.mktemp(suffix=".pdf")
                download.save_as(tmp)
                with open(tmp, "rb") as f:
                    data = f.read()
                os.unlink(tmp)
                print(f"    [pdf] Download intercepted: {len(data)//1024}KB")
                if data[:4] == b"%PDF":
                    pdf_bytes_list.append(data)
            except Exception as e:
                print(f"    [pdf] Download error: {e}")

        page.on("download", on_download)

        try:
            print(f"    [pdf] Loading: {url[:80]}")
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(8000)

            # Try clicking download/view buttons
            if not pdf_bytes_list:
                for sel in ["a[href*='download']", "a[href*='pdf']", "button[class*='download']",
                            "a[class*='download']", "[data-testid*='download']"]:
                    try:
                        els = page.query_selector_all(sel)
                        for el in els[:2]:
                            el.click()
                            page.wait_for_timeout(3000)
                            if pdf_bytes_list:
                                break
                        if pdf_bytes_list:
                            break
                    except Exception:
                        continue

        except Exception as e:
            print(f"    [pdf] Page error: {e}")
        finally:
            context.close()
            browser.close()

    return pdf_bytes_list[-1] if pdf_bytes_list else None


def _fetch_pdf_for_announcement(ann: Announcement) -> Optional[bytes]:
    """Fetch PDF via Playwright with full encoding support."""
    url = ann.url
    if not url:
        return None
    return _fetch_via_playwright_download(url)


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
            print(f"  [pdf] Got {len(data)//1024}KB but not a valid PDF (starts: {data[:8]})")
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
