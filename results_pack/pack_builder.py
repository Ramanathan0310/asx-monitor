# results_pack/pack_builder.py - adapted from Jimmy's version
from __future__ import annotations
import datetime as dt
import re
from typing import List, Optional
from .models import Announcement, ResultPack

_TRIGGER_KEYWORDS = [
    "half year", "half-year", "full year", "full-year",
    "appendix 4d", "appendix 4e", "hy results", "fy results",
    "h1 results", "interim results", "results announcement",
    "financial results", "earnings release", "preliminary final",
    "1h fy", "1hfy", "2h fy", "2hfy",
]
_PACK_KEYWORDS = [
    "half year", "half-year", "full year", "full-year", "results",
    "presentation", "appendix 4d", "appendix 4e", "dividend",
    "distribution", "financial report", "annual report", "interim",
    "preliminary final", "earnings", "fy ",
]
_EXCLUDE_KEYWORDS = ["transcript", "webcast", "conference call"]
_HY_SIGNALS = ["half year", "half-year", "h1 ", "1h ", "1hfy", "interim", "appendix 4d"]
_FY_SIGNALS = ["full year", "full-year", "annual", "fy results", "appendix 4e"]


def _parse_date(date_str: str) -> dt.date:
    for fmt in ["%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d", "%d %b %Y"]:
        try:
            return dt.datetime.strptime(date_str, fmt).date()
        except Exception:
            pass
    raise ValueError(f"Cannot parse date: {date_str}")

def _is_trigger(title: str) -> bool:
    t = title.lower()
    if any(x in t for x in _EXCLUDE_KEYWORDS):
        return False
    if any(x in t for x in _TRIGGER_KEYWORDS):
        return True
    if re.search(r"\bfy\d{2,4}\b", t):
        return True
    return False

def _is_pack_doc(title: str) -> bool:
    t = title.lower()
    if any(x in t for x in _EXCLUDE_KEYWORDS):
        return False
    if any(x in t for x in _PACK_KEYWORDS):
        return True
    if re.search(r"\bfy\d{2,4}\b", t):
        return True
    return False

def _infer_result_type(announcements: List[Announcement]) -> str:
    combined = " ".join(a.title.lower() for a in announcements)
    hy = sum(1 for kw in _HY_SIGNALS if kw in combined)
    fy = sum(1 for kw in _FY_SIGNALS if kw in combined)
    if hy > fy: return "HY"
    if fy > hy: return "FY"
    return "FY"

def _ann_sort_key(a: Announcement) -> dt.date:
    try:
        return _parse_date(a.date)
    except Exception:
        return dt.date.min

def _type_matches(title: str, report_type: str) -> bool:
    t = title.lower()
    is_hy = any(k in t for k in _HY_SIGNALS)
    is_fy = any(k in t for k in _FY_SIGNALS)
    if report_type.upper() == "HY" and is_fy and not is_hy:
        return False
    if report_type.upper() == "FY" and is_hy and not is_fy:
        return False
    return True

def build_result_pack(
    announcements: List[Announcement],
    report_type: Optional[str] = None,
    target_date: Optional[dt.date] = None,
) -> Optional[ResultPack]:
    if not announcements:
        return None
    sorted_anns = sorted(announcements, key=_ann_sort_key, reverse=True)
    trigger_date: Optional[str] = None
    for ann in sorted_anns:
        if not _is_trigger(ann.title):
            continue
        if target_date is not None:
            try:
                if _parse_date(ann.date) != target_date:
                    continue
            except Exception:
                continue
        if report_type is not None and not _type_matches(ann.title, report_type):
            continue
        trigger_date = ann.date
        break
    if trigger_date is None:
        return None
    same_day = [a for a in announcements if a.date == trigger_date]
    pack_anns = [a for a in same_day if _is_pack_doc(a.title)]
    pack_urls = {a.url for a in pack_anns}
    for ann in same_day:
        if ann.url not in pack_urls and _is_trigger(ann.title):
            pack_anns.append(ann)
            pack_urls.add(ann.url)
    pack_anns.sort(key=lambda a: a.title)
    result_type = report_type.upper() if report_type else _infer_result_type(pack_anns)
    ticker = announcements[0].ticker if announcements else "UNKNOWN"
    return ResultPack(
        ticker=ticker,
        company_name=ticker,
        result_date=trigger_date,
        result_type=result_type,
        announcements=pack_anns,
    )

def find_nearest_result_dates(
    announcements: List[Announcement],
    report_type: Optional[str] = None,
    n: int = 5,
) -> List[str]:
    seen: set = set()
    result_dates: List[str] = []
    sorted_anns = sorted(announcements, key=_ann_sort_key, reverse=True)
    for ann in sorted_anns:
        if not _is_trigger(ann.title):
            continue
        if report_type is not None and not _type_matches(ann.title, report_type):
            continue
        date_key = ann.date
        if date_key not in seen:
            seen.add(date_key)
            try:
                iso = dt.datetime.strptime(date_key, "%d/%m/%Y").strftime("%Y-%m-%d")
            except Exception:
                iso = date_key
            result_dates.append(iso)
        if len(result_dates) >= n:
            break
    return result_dates
