# results_pack/claude_runner.py - adapted from Jimmy's version (Claude instead of OpenAI)
from __future__ import annotations
import base64
import io
import json
import os
from pathlib import Path
from typing import Dict, List, Optional
from .models import Announcement, ResultPack
from .prompts import ARTIFACT_SUFFIX, PROMPT_REGISTRY

CLAUDE_MODEL     = "claude-sonnet-4-6"
CLAUDE_MAX_TOKENS = 4096  # Reduced to avoid overload errors
MAX_PDF_BYTES    = 30 * 1024 * 1024  # 30MB per PDF


def _extract_pdf_text(raw: bytes) -> str:
    """Extract text from PDF bytes - tries pymupdf first, then pdfplumber, then pypdf."""
    # Try pymupdf (best for ASX PDFs with custom fonts)
    try:
        import fitz  # pymupdf
        doc = fitz.open(stream=raw, filetype="pdf")
        pages = []
        for page in doc:
            text = page.get_text()
            if text.strip():
                pages.append(text)
        doc.close()
        result = "\n".join(pages)
        if len(result.strip()) > 200:
            print(f"    [pdf_text] pymupdf extracted {len(result)} chars")
            return result
    except Exception as e:
        print(f"    [pdf_text] pymupdf failed: {e}")

    # Try pdfplumber
    try:
        import pdfplumber
        pages = []
        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            for page in pdf.pages[:30]:
                text = page.extract_text() or ""
                if text.strip():
                    pages.append(text)
        result = "\n".join(pages)
        if len(result.strip()) > 200:
            print(f"    [pdf_text] pdfplumber extracted {len(result)} chars")
            return result
    except Exception as e:
        print(f"    [pdf_text] pdfplumber failed: {e}")

    # Fall back to pypdf
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(raw))
        pages = []
        for page in reader.pages[:30]:
            text = page.extract_text() or ""
            if text.strip():
                pages.append(text)
        result = "\n".join(pages)
        if len(result.strip()) > 200:
            print(f"    [pdf_text] pypdf extracted {len(result)} chars")
            return result
    except Exception as e:
        print(f"    [pdf_text] pypdf failed: {e}")

    print(f"    [pdf_text] all extractors failed - will send as base64")
    return ""


def _call_claude(system_prompt: str, text_context: str, pdf_items: List[Announcement]) -> str:
    import anthropic
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return "__LLM_FAILED__"

    content: List[Dict] = []
    attached = 0
    text_fallbacks = []

    # Filter: skip dividend notices, prioritise substantive docs
    useful_items = [
        a for a in pdf_items
        if a.pdf_bytes and len(a.pdf_bytes) > 0
        and not any(x in a.title.lower() for x in ["dividend", "distribution"])
    ]
    if not useful_items:
        useful_items = [a for a in pdf_items if a.pdf_bytes]

    # Cap total payload to ~15MB across all PDFs to avoid context limit
    MAX_TOTAL_BYTES = 15 * 1024 * 1024
    total_bytes = 0
    capped_items = []
    for ann in sorted(useful_items, key=lambda a: len(a.pdf_bytes or b"")):
        sz = len(ann.pdf_bytes or b"")
        if total_bytes + sz > MAX_TOTAL_BYTES:
            print(f"    [claude] Skipping {ann.title[:50]} ({sz//1024}KB) - would exceed 15MB cap")
            break
        capped_items.append(ann)
        total_bytes += sz
    useful_items = capped_items
    print(f"    [claude] Total PDF payload: {total_bytes//1024}KB across {len(useful_items)} docs")

    for ann in useful_items:
        raw = ann.pdf_bytes
        if not raw or len(raw) > MAX_PDF_BYTES:
            continue

        # Try text extraction first
        extracted = _extract_pdf_text(raw)
        if extracted and len(extracted) > 200:
            # Send as text block - more reliable than base64 PDF
            print(f"    [claude] Text extracted from PDF: {len(extracted)} chars")
            text_fallbacks.append(
                f"=== {ann.title} ===\n{extracted[:20000]}\n"
            )
            attached += 1
        else:
            # Fall back to base64 PDF (for image-based/scanned PDFs)
            print(f"    [claude] Sending as base64 PDF: {ann.title[:50]}")
            content.append({
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": base64.standard_b64encode(raw).decode("utf-8"),
                },
                "title": ann.title[:200],
            })
            attached += 1

    if attached == 0:
        print("  [claude] No PDFs attached")
        return "__NO_PDFS__"

    # Combine text context with any text-extracted PDF content
    combined_text = text_context[:10_000]
    if text_fallbacks:
        combined_text += "\n\n=== EXTRACTED PDF CONTENT ===\n" + "\n".join(text_fallbacks)

    content.append({"type": "text", "text": combined_text[:60_000]})

    try:
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=CLAUDE_MAX_TOKENS,
            system=system_prompt,
            messages=[{"role": "user", "content": content}],
        )
        return (resp.content[0].text or "").strip()
    except Exception as e:
        err = str(e)
        print(f"  [claude] API call failed: {err[:200]}")
        # If overloaded, wait and retry once
        if "overloaded" in err.lower() or "529" in err:
            import time
            print(f"  [claude] API overloaded - waiting 30s and retrying...")
            time.sleep(30)
            try:
                client = anthropic.Anthropic(api_key=api_key)
                resp = client.messages.create(
                    model=CLAUDE_MODEL,
                    max_tokens=CLAUDE_MAX_TOKENS,
                    system=system_prompt,
                    messages=[{"role": "user", "content": content}],
                )
                return (resp.content[0].text or "").strip()
            except Exception as e2:
                print(f"  [claude] Retry also failed: {e2}")
        return "__LLM_FAILED__"


def _build_text_context(pack: ResultPack) -> str:
    titles = "\n".join(f"  - {a.title}" for a in pack.announcements)
    return (
        f"Ticker: {pack.ticker}\n"
        f"Company: {pack.company_name}\n"
        f"Announcement date: {pack.result_date}\n"
        f"Result type: {pack.result_type}\n"
        f"Documents in pack: {len(pack.announcements)} ({pack.pdfs_downloaded} PDFs attached)\n\n"
        f"Document titles:\n{titles}\n"
    )


def run_prompts(
    pack: ResultPack,
    output_folder: Path,
    prompts_to_run: Optional[List[str]] = None,
    include_strawman: bool = True,
) -> Dict[str, str]:
    if prompts_to_run is None:
        prompts_to_run = ["management_report", "equity_report"]
        if include_strawman:
            prompts_to_run.append("strawman_post")

    text_context = _build_text_context(pack)
    artifacts: Dict[str, str] = {}

    for prompt_key in prompts_to_run:
        system_prompt = PROMPT_REGISTRY.get(prompt_key)
        if not system_prompt:
            continue
        suffix   = ARTIFACT_SUFFIX.get(prompt_key, f"{prompt_key}.md")
        out_file = output_folder / f"{pack.file_prefix}-{suffix}"

        print(f"  [claude] Running '{prompt_key}' for {pack.ticker}...")
        response = _call_claude(system_prompt, text_context, pack.announcements)

        if response in ("__LLM_FAILED__", "__NO_PDFS__"):
            out_file.write_text(f"# {suffix}\n\nAnalysis failed: {response}\n", encoding="utf-8")
        else:
            out_file.write_text(response, encoding="utf-8")
            print(f"  [claude] Saved -> {out_file.name}")

        artifacts[prompt_key] = str(out_file)

    # Save debug context
    ctx_path = output_folder / f"{pack.file_prefix}-Claude-Context.json"
    ctx_path.write_text(json.dumps({
        "ticker": pack.ticker,
        "result_date": pack.result_date,
        "result_type": pack.result_type,
        "text_context": text_context,
        "prompts_run": prompts_to_run,
    }, indent=2), encoding="utf-8")

    return artifacts
