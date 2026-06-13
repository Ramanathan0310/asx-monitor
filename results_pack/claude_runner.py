# results_pack/claude_runner.py - adapted from Jimmy's version (Claude instead of OpenAI)
from __future__ import annotations
import base64
import json
import os
from pathlib import Path
from typing import Dict, List, Optional
from .models import Announcement, ResultPack
from .prompts import ARTIFACT_SUFFIX, PROMPT_REGISTRY

CLAUDE_MODEL     = "claude-sonnet-4-6"
CLAUDE_MAX_TOKENS = 8000
MAX_PDF_BYTES    = 30 * 1024 * 1024  # 30MB per PDF


def _call_claude(system_prompt: str, text_context: str, pdf_items: List[Announcement]) -> str:
    import anthropic
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return "__LLM_FAILED__"

    content: List[Dict] = []
    attached = 0
    for ann in pdf_items:
        raw = ann.pdf_bytes
        if not raw or len(raw) > MAX_PDF_BYTES:
            continue
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

    content.append({"type": "text", "text": text_context[:30_000]})

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
        print(f"  [claude] API call failed: {e}")
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
