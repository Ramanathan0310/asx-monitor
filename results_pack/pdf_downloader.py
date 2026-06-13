# results_pack/pdf_downloader.py
from __future__ import annotations
import io
import json
from pathlib import Path
from typing import Optional
import requests
from .models import Announcement, ResultPack

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/pdf,*/*",
}

def _fetch_pdf(url: str) -> Optional[bytes]:
    if not url:
        return None
    try:
        r = requests.get(url, headers=_HEADERS, timeout=30)
        r.raise_for_status()
        ct = r.headers.get("content-type", "").lower()
        if "pdf" not in ct and not url.lower().endswith(".pdf"):
            return None
        data = r.content
        if len(data) < 1000:
            return None
        return data
    except Exception as e:
        print(f"  [pdf] Failed ({url[:60]}): {e}")
        return None

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
            continue
        if dry_run:
            print(f"  [pdf] [DRY-RUN] Would download: {ann.title[:60]}")
            continue
        print(f"  [pdf] Downloading: {ann.title[:60]}")
        data = _fetch_pdf(url)
        if data:
            if len(data) > max_bytes:
                print(f"  [pdf] Skipping oversized PDF ({len(data)//1024}KB)")
                continue
            ann.pdf_bytes = data
            # Save to disk
            safe_name = "".join(c if c.isalnum() or c in ".-_ " else "_" for c in ann.title[:60])
            pdf_path = output_folder / f"{safe_name}.pdf"
            pdf_path.write_bytes(data)
            ann.pdf_path = str(pdf_path)
            downloaded += 1
            print(f"  [pdf] OK ({len(data)//1024}KB)")
        else:
            print(f"  [pdf] Failed or empty")
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
            }
            for a in pack.announcements
        ],
    }
    path = output_folder / f"{pack.file_prefix}-metadata.json"
    path.write_text(json.dumps(meta, indent=2))
    return path
