# results_pack/models.py - unchanged from Jimmy's version
from __future__ import annotations
import datetime as dt
from dataclasses import dataclass, field
from typing import Dict, List, Optional

@dataclass
class Announcement:
    ticker: str
    title: str
    date: str
    time: str
    url: str
    pdf_url: Optional[str] = None
    pdf_bytes: Optional[bytes] = None
    pdf_path: Optional[str] = None

@dataclass
class ResultPack:
    ticker: str
    company_name: str
    result_date: str
    result_type: str
    announcements: List[Announcement] = field(default_factory=list)

    @property
    def date_prefix(self) -> str:
        # Handle both DD/MM/YY and DD/MM/YYYY formats
        for fmt in ["%d/%m/%Y", "%d/%m/%y"]:
            try:
                d = dt.datetime.strptime(self.result_date, fmt)
                break
            except Exception:
                pass
        else:
            d = dt.datetime.now()
        return d.strftime("%y%m%d")

    @property
    def folder_name(self) -> str:
        return f"{self.date_prefix}-{self.ticker}-{self.result_type}-Results-Pack"

    @property
    def file_prefix(self) -> str:
        return f"{self.date_prefix}-{self.ticker}-{self.result_type}"

    @property
    def pdfs_downloaded(self) -> int:
        return sum(1 for a in self.announcements if a.pdf_bytes is not None)

@dataclass
class RunSummary:
    ticker: str
    result_date: str
    result_type: str
    pdfs_downloaded: int
    prompts_run: List[str]
    local_folder: str
    artifacts: Dict[str, str] = field(default_factory=dict)
    failure_reason: Optional[str] = None
    failure_message: Optional[str] = None

    @property
    def success(self) -> bool:
        return self.failure_reason is None

    def print_summary(self) -> None:
        print("\n" + "=" * 60)
        print("  Results Pack Agent -- Run Summary")
        print("=" * 60)
        print(f"  Ticker      : {self.ticker}")
        print(f"  Result date : {self.result_date}")
        print(f"  Result type : {self.result_type}")
        if self.failure_reason:
            print(f"  Status      : FAILED ({self.failure_reason})")
            if self.failure_message:
                print(f"  Reason      : {self.failure_message}")
        else:
            print(f"  PDFs        : {self.pdfs_downloaded}")
            print(f"  Prompts     : {', '.join(self.prompts_run)}")
            print(f"  Folder      : {self.local_folder}")
            if self.artifacts:
                for name, path in self.artifacts.items():
                    print(f"  {name}: {path}")
        print("=" * 60 + "\n")
