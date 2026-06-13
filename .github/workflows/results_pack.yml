# -*- coding: utf-8 -*-
#!/usr/bin/env python3
"""
Results Pack Agent
Detects HY/FY results announcements, downloads PDFs, runs deep Claude analysis.
Triggered automatically when Bob finds a RESULTS announcement, or manually.

Usage:
  python results_pack_agent.py --ticker WTC
  python results_pack_agent.py --ticker WTC --report-type FY
  python results_pack_agent.py --ticker WTC --date 2026-02-20
  python results_pack_agent.py  # scans all portfolio stocks
"""

import argparse
import datetime as dt
import json
import os
import smtplib
import ssl
import sys
from email.message import EmailMessage
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# .env loader
# ---------------------------------------------------------------------------

def _load_dotenv():
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        k = key.strip()
        v = value.strip().strip('"').strip("'")
        if not os.environ.get(k):
            os.environ[k] = v

_load_dotenv()

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

BASE_DIR       = Path(__file__).parent
COMPANIES_FILE = BASE_DIR / "companies.json"
REPORTS_DIR    = BASE_DIR / "reports"
RESULTS_DIR    = BASE_DIR / "results_packs"

# ---------------------------------------------------------------------------
# Company name lookup
# ---------------------------------------------------------------------------

def _load_companies() -> dict:
    if not COMPANIES_FILE.exists():
        return {}
    data = json.loads(COMPANIES_FILE.read_text())
    return {c["ticker"].upper(): c.get("name", c["ticker"]) for c in data.get("companies", [])}


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def _send_email(subject: str, body_md: str, attachments: list[Path]) -> bool:
    to_addr   = os.environ.get("EMAIL_TO")
    from_addr = os.environ.get("EMAIL_FROM")
    host      = os.environ.get("SMTP_HOST")
    port      = int(os.environ.get("SMTP_PORT", "587"))
    user      = os.environ.get("SMTP_USER")
    password  = os.environ.get("SMTP_PASSWORD")

    if not all([to_addr, from_addr, host, user, password]):
        print("  (email skipped - SMTP not configured)")
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"]    = from_addr
    msg["To"]      = to_addr
    msg.set_content(body_md)

    for path in attachments:
        if path.exists():
            data = path.read_bytes()
            maintype = "text"
            subtype  = "markdown" if path.suffix == ".md" else "plain"
            msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=path.name)

    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=60) as s:
                s.login(user, password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=60) as s:
                s.starttls(context=ssl.create_default_context())
                s.login(user, password)
                s.send_message(msg)
        print(f"  OK Emailed to {to_addr}")
        return True
    except Exception as e:
        print(f"  Email failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Core run function
# ---------------------------------------------------------------------------

def run_results_pack(
    ticker: str,
    report_type: Optional[str] = None,
    target_date: Optional[str] = None,
    include_strawman: bool = True,
    send_email: bool = True,
) -> bool:
    """
    Run the full results pack pipeline for a single ticker.
    Returns True on success.
    """
    from results_pack.asx_fetcher import fetch_announcements
    from results_pack.pack_builder import build_result_pack, find_nearest_result_dates
    from results_pack.pdf_downloader import download_pack_pdfs, save_pack_metadata
    from results_pack.claude_runner import run_prompts
    from results_pack.models import RunSummary

    ticker = ticker.upper().strip()
    companies = _load_companies()
    company_name = companies.get(ticker, ticker)

    print(f"\nResults Pack Agent -- {ticker} ({company_name})")
    print("=" * 55)

    RESULTS_DIR.mkdir(exist_ok=True)

    # Parse target date
    target_dt = None
    if target_date:
        try:
            target_dt = dt.datetime.strptime(target_date, "%Y-%m-%d").date()
        except Exception:
            print(f"  Invalid date format: {target_date} (use YYYY-MM-DD)")

    # 1. Fetch announcements
    print(f"  Fetching announcements for {ticker}...")
    announcements = fetch_announcements(ticker)
    if not announcements:
        print(f"  No announcements found for {ticker}")
        return False

    # 2. Detect results pack
    print(f"  Detecting result pack...")
    pack = build_result_pack(announcements, report_type=report_type, target_date=target_dt)

    if pack is None:
        nearest = find_nearest_result_dates(announcements, report_type=report_type, n=5)
        print(f"  No result pack found for {ticker}")
        if nearest:
            print(f"  Nearest result dates: {', '.join(nearest)}")
        return False

    pack.company_name = company_name
    print(f"  Found: {pack.result_type} results on {pack.result_date} -- {len(pack.announcements)} docs")

    # 3. Create output folder
    output_folder = RESULTS_DIR / pack.folder_name
    output_folder.mkdir(parents=True, exist_ok=True)
    print(f"  Output folder: {output_folder}")

    # 4. Download PDFs
    print(f"  Downloading PDFs...")
    pdfs_downloaded = download_pack_pdfs(pack, output_folder)
    print(f"  Downloaded {pdfs_downloaded} PDFs")

    if pdfs_downloaded == 0:
        print("  WARNING: No PDFs downloaded -- Claude analysis will be limited")

    # Save metadata
    save_pack_metadata(pack, output_folder)

    # 5. Run Claude prompts
    prompts = ["management_report", "equity_report"]
    if include_strawman:
        prompts.append("strawman_post")

    print(f"  Running Claude analysis: {prompts}")
    artifacts = run_prompts(pack, output_folder, prompts_to_run=prompts)

    # 6. Build email body + send
    if send_email:
        today = dt.date.today().isoformat()
        subject = (
            f"Results Pack -- {ticker} {pack.result_type} Results "
            f"{pack.result_date} -- {pdfs_downloaded} PDFs"
        )

        body = f"""Results Pack Agent -- {company_name} ({ticker})
{pack.result_type} Results | {pack.result_date}
PDFs downloaded: {pdfs_downloaded}/{len(pack.announcements)}
Prompts run: {', '.join(prompts)}

Documents in pack:
""" + "\n".join(f"  - {a.title}" for a in pack.announcements)

        # Attach the markdown reports
        attach_paths = [Path(p) for p in artifacts.values() if p.endswith(".md")]
        _send_email(subject, body, attach_paths)

    summary = RunSummary(
        ticker=ticker,
        result_date=pack.result_date,
        result_type=pack.result_type,
        pdfs_downloaded=pdfs_downloaded,
        prompts_run=prompts,
        local_folder=str(output_folder),
        artifacts=artifacts,
    )
    summary.print_summary()
    return True


# ---------------------------------------------------------------------------
# Scan all portfolio stocks for recent results
# ---------------------------------------------------------------------------

def scan_portfolio(days_back: int = 7) -> list[str]:
    """
    Scan all portfolio stocks for results announcements in the last N days.
    Returns list of tickers that had results.
    """
    from results_pack.asx_fetcher import fetch_announcements
    from results_pack.pack_builder import _is_trigger

    companies = _load_companies()
    cutoff = dt.date.today() - dt.timedelta(days=days_back)
    found = []

    print(f"\nScanning {len(companies)} portfolio stocks for results (last {days_back} days)...")

    for ticker, name in companies.items():
        print(f"  Checking {ticker}...", end=" ")
        anns = fetch_announcements(ticker)
        recent_results = [
            a for a in anns
            if _is_trigger(a.title)
            and _parse_date_safe(a.date) >= cutoff
        ]
        if recent_results:
            print(f"RESULTS FOUND: {recent_results[0].title[:60]}")
            found.append(ticker)
        else:
            print("clear")

    return found


def _parse_date_safe(date_str: str) -> dt.date:
    for fmt in ["%d/%m/%Y", "%Y-%m-%d", "%d %b %Y"]:
        try:
            return dt.datetime.strptime(date_str, fmt).date()
        except Exception:
            pass
    return dt.date.min


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="ASX Results Pack Agent")
    parser.add_argument("--ticker", default=None, help="ASX ticker (e.g. WTC). Omit to scan all portfolio.")
    parser.add_argument("--report-type", dest="report_type", choices=["HY", "FY"], default=None)
    parser.add_argument("--date", default=None, metavar="YYYY-MM-DD", help="Target a specific result date")
    parser.add_argument("--no-strawman", dest="no_strawman", action="store_true", help="Skip Strawman post")
    parser.add_argument("--no-email", dest="no_email", action="store_true", help="Skip email")
    parser.add_argument("--scan", action="store_true", help="Scan all portfolio stocks for recent results")
    parser.add_argument("--days-back", dest="days_back", type=int, default=7, help="Days back for --scan (default 7)")
    args = parser.parse_args()

    if args.scan or args.ticker is None:
        # Scan mode
        tickers_with_results = scan_portfolio(days_back=args.days_back)
        if tickers_with_results:
            print(f"\nFound results for: {', '.join(tickers_with_results)}")
            for ticker in tickers_with_results:
                run_results_pack(
                    ticker=ticker,
                    report_type=args.report_type,
                    include_strawman=not args.no_strawman,
                    send_email=not args.no_email,
                )
        else:
            print("\nNo recent results found across portfolio.")
        return

    # Single ticker mode
    success = run_results_pack(
        ticker=args.ticker,
        report_type=args.report_type,
        target_date=args.date,
        include_strawman=not args.no_strawman,
        send_email=not args.no_email,
    )
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
