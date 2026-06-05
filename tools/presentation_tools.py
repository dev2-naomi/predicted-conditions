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


_AUS_VERBS = {
    "must include": "Confirm",
    "must show": "Verify",
    "must be": "Confirm",
    "must provide": "Obtain",
    "must cover": "Confirm",
    "must identify": "Verify",
    "must confirm": "Confirm",
    "must contain": "Verify",
    "must display": "Verify",
}


def _aus_restyle_spec(spec: str) -> str:
    """Restyle a raw specification into AUS-inspired language."""
    s = spec.strip()
    lower = s.lower()
    for prefix, verb in _AUS_VERBS.items():
        if lower.startswith(prefix):
            remainder = s[len(prefix):].strip()
            if remainder and remainder[0].isupper():
                remainder = remainder[0].lower() + remainder[1:]
            styled = f"{verb} {remainder}"
            if not styled.endswith("."):
                styled += "."
            return styled
    if not s.endswith("."):
        s += "."
    return s


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
              reason_for_requirement, review_notes, satisfied_requirements
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

    for dr in doc_requests:
        display = dr.get("display")
        if not isinstance(display, dict):
            continue

        # --- Fix heading: must match the canonical document_type exactly ---
        # Only apply when display has real content (LLM actually styled it).
        canonical_dt = (dr.get("document_type") or "").strip()
        if canonical_dt and display.get("document_heading"):
            display["document_heading"] = canonical_dt

        # --- Fallback: auto-populate satisfied_requirements from raw data ---
        sat_specs = dr.get("satisfied_specifications") or []
        existing_sat = display.get("satisfied_requirements") or []
        if sat_specs and not existing_sat:
            styled_sats = []
            for item in sat_specs:
                raw = item.get("specification", "") if isinstance(item, dict) else str(item)
                if raw:
                    styled_sats.append(
                        f"{_aus_restyle_spec(raw).rstrip('.')} — confirmed by submitted document."
                    )
            display["satisfied_requirements"] = styled_sats

        # --- Clear documentation_requirements when no specs remain ---
        remaining_specs = dr.get("specifications") or []
        if not remaining_specs:
            display["documentation_requirements"] = []

        # --- Dedup: strip documentation_requirements that overlap satisfied ---
        sat_items = display.get("satisfied_requirements") or []
        if sat_items:
            doc_reqs = display.get("documentation_requirements") or []
            if doc_reqs:
                sat_keywords = {
                    _normalize_key(s.split("—")[0]) for s in sat_items
                }
                filtered = []
                for req in doc_reqs:
                    req_norm = _normalize_key(req)
                    is_dup = any(
                        SequenceMatcher(None, req_norm, sk).ratio() > 0.70
                        for sk in sat_keywords
                    )
                    if not is_dup:
                        filtered.append(req)
                display["documentation_requirements"] = filtered

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
