"""
presentation_tools.py — Tool for STEP_09: AUS-Like Presentation Styler.

One tool:
  1. style_document_requests — merge LLM-generated display blocks into final_output
"""

from __future__ import annotations

from difflib import SequenceMatcher
from typing import Any

from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command
from typing_extensions import Annotated


def _normalize_key(s: str) -> str:
    return s.strip().lower().replace("-", " ").replace("_", " ")


def _best_match(name: str, candidates: list[str], threshold: float = 0.55) -> str | None:
    """Find the best fuzzy match for *name* among *candidates*."""
    norm = _normalize_key(name)
    best_score, best_candidate = 0.0, None
    for c in candidates:
        score = SequenceMatcher(None, norm, _normalize_key(c)).ratio()
        if score > best_score:
            best_score, best_candidate = score, c
    if best_score >= threshold:
        return best_candidate
    return None


@tool
def style_document_requests(
    styled_displays: list[dict],
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
    state: Annotated[dict, InjectedState] = None,
) -> Command:
    """
    Attach AUS-styled display blocks to the final output document requests.
    This tool merges incrementally — call it multiple times for batches and
    previously attached display blocks are preserved.

    Args:
        styled_displays: A list of objects, each with:
            - document_type: str matching an existing document_request
            - display: dict with document_heading, documentation_requirements,
              reason_for_requirement, review_notes
    """
    s = state or {}
    final_output: dict = dict(s.get("final_output") or {})
    doc_requests: list[dict] = list(final_output.get("document_requests", []))

    # Build exact-match lookup first
    display_map: dict[str, dict] = {}
    for entry in styled_displays:
        dt = (entry.get("document_type") or "").strip()
        display = entry.get("display")
        if dt and isinstance(display, dict):
            display_map[dt.lower()] = display

    # Collect existing document_type names for fuzzy fallback
    doc_type_names = [(dr.get("document_type") or "").strip() for dr in doc_requests]

    matched = 0
    unmatched: list[str] = []
    for dr in doc_requests:
        dt = (dr.get("document_type") or "").strip().lower()
        if dt in display_map:
            dr["display"] = display_map[dt]
            matched += 1
        elif not dr.get("display") or not dr["display"].get("document_heading"):
            # Don't overwrite an existing valid display from a previous batch
            dr.setdefault("display", {})

    # Fuzzy-match any leftover display entries that didn't match exactly
    matched_keys = {(dr.get("document_type") or "").strip().lower() for dr in doc_requests if dr.get("display") and dr["display"].get("document_heading")}
    for entry in styled_displays:
        dt = (entry.get("document_type") or "").strip()
        if dt.lower() in matched_keys:
            continue
        display = entry.get("display")
        if not dt or not isinstance(display, dict):
            continue
        candidate = _best_match(dt, doc_type_names)
        if candidate and candidate.lower() not in matched_keys:
            for dr in doc_requests:
                if (dr.get("document_type") or "").strip().lower() == candidate.lower():
                    dr["display"] = display
                    matched += 1
                    matched_keys.add(candidate.lower())
                    break
        else:
            unmatched.append(dt)

    final_output["document_requests"] = doc_requests
    styled_total = sum(1 for dr in doc_requests if dr.get("display") and dr["display"].get("document_heading"))

    parts = [f"Styled {matched} document requests in this batch ({styled_total}/{len(doc_requests)} total have display blocks)."]
    if unmatched:
        parts.append(f"Unmatched display entries (no document_request found): {unmatched}")
        remaining = [
            dr.get("document_type") for dr in doc_requests
            if not dr.get("display") or not dr["display"].get("document_heading")
        ]
        if remaining:
            parts.append(f"Documents still needing display blocks: {remaining}")
    msg = " ".join(parts)

    return Command(update={
        "final_output": final_output,
        "messages": [ToolMessage(msg, tool_call_id=tool_call_id)],
    })
