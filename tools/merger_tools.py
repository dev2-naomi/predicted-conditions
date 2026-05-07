"""
merger_tools.py — Tools for STEP_08: Merger, De-Duper, Ranker (v2 Document-Centric).

Three tools:
  1. merge_document_requests   — collect & merge doc requests from modules 01-07
  2. rank_document_requests    — sort by severity/priority/category and assign status
  3. generate_final_output     — assemble the final output JSON with stats
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command
from typing_extensions import Annotated

from tools.shared.normalize import normalize_all


# ---------------------------------------------------------------------------
# Ranking helpers
# ---------------------------------------------------------------------------

_SEVERITY_RANK = {"HARD-STOP": 0, "SOFT-STOP": 1}
_PRIORITY_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
_CATEGORY_RANK = {
    "Program Eligibility": 0, "Cross-Cutting": 0,
    "Compliance": 1, "Credit": 2, "Income": 3,
    "Assets": 4, "Property": 5, "Appraisal": 5,
    "Title": 6, "Closing": 6, "Other": 7,
}


def _severity_val(s: str) -> int:
    return _SEVERITY_RANK.get(s, 1)


def _priority_val(p: str) -> int:
    return _PRIORITY_RANK.get(p, 3)


def _category_val(c: str) -> int:
    return _CATEGORY_RANK.get(c, 7)


def _sort_key(dr: dict) -> tuple:
    return (
        _severity_val(dr.get("severity", "SOFT-STOP")),
        _priority_val(dr.get("priority", "P3")),
        _category_val(dr.get("document_category", "Other")),
    )


# ---------------------------------------------------------------------------
# Merge-key helpers
# ---------------------------------------------------------------------------

_CANONICAL_NAMES: dict[str, str] = {
    "executed lease agreement": "lease agreement",
    "executed lease": "lease agreement",
    "lease": "lease agreement",
    "rent loss insurance evidence": "rent loss insurance",
    "evidence of rent loss insurance": "rent loss insurance",
    "hazard insurance declaration page": "hazard insurance",
    "hazard insurance": "hazard insurance",
    "property insurance": "hazard insurance",
    "property insurance / hazard insurance": "hazard insurance",
    "flood determination / flood certificate": "flood determination",
    "flood certificate": "flood determination",
    "flood cert": "flood determination",
    "mortgage payment history": "mortgage payment history",
    "verification of mortgage": "verification of mortgage",
    "verification of mortgage (vom)": "verification of mortgage",
    "vom": "verification of mortgage",
    "verification of rent": "verification of rent",
    "verification of rent (vor)": "verification of rent",
    "vor": "verification of rent",
    "primary residence verification": "primary residence verification",
    "proof of primary residence ownership": "primary residence verification",
    "proof of primary residence": "primary residence verification",
    "rent loss insurance": "rent loss insurance",
    "rent loss insurance evidence": "rent loss insurance",
    "government-issued photo id": "government id",
    "government id": "government id",
    "drivers license": "government id",
    "passport": "government id",
    "borrower authorization form": "borrower authorization",
    "borrower authorization": "borrower authorization",
    "occupancy certification / investor certification": "occupancy certification",
    "occupancy certification": "occupancy certification",
    "investor certification": "occupancy certification",
}


def _canonical_doc_type(name: str) -> str:
    key = name.strip().lower()
    return _CANONICAL_NAMES.get(key, key)


def _merge_key(dr: dict) -> str:
    """Compute a merge key from canonical document_type + document_context fields."""
    raw_type = dr.get("document_type") or dr.get("document_name") or ""
    doc_type = _canonical_doc_type(raw_type)
    ctx = dr.get("document_context")
    if isinstance(ctx, dict):
        ctx_parts = []
        for field in ("borrower", "employer", "account", "property", "business", "tax_year"):
            val = ctx.get(field)
            if val:
                ctx_parts.append(f"{field}={str(val).strip().lower()}")
        ctx_str = "|".join(ctx_parts) if ctx_parts else ""
    else:
        ctx_str = (str(ctx) if ctx else "").strip().lower()
    return f"{doc_type}|{ctx_str}"


# ---------------------------------------------------------------------------
# Collection helpers
# ---------------------------------------------------------------------------

def _as_list(val: Any) -> list:
    if val is None:
        return []
    if isinstance(val, list):
        return val
    if isinstance(val, dict):
        return [val]
    if isinstance(val, str):
        return [val] if val else []
    return [val]


def _union_strings(a: list, b: list) -> list:
    """Union two lists, deduplicating by string representation."""
    seen: set[str] = set()
    result: list = []
    for item in a + b:
        key = str(item)
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _union_by_field(a: list, b: list, field: str) -> list:
    """Union two lists of dicts, deduplicating by a key field."""
    a = [x if isinstance(x, dict) else {"value": x} for x in _as_list(a)]
    b = [x if isinstance(x, dict) else {"value": x} for x in _as_list(b)]
    seen: set[str] = set()
    result: list[dict] = []
    for item in a + b:
        k = str(item.get(field, item))
        if k not in seen:
            seen.add(k)
            result.append(item)
    return result


def _union_specs(a: list, b: list) -> list:
    """Union specifications, deduplicating by spec_id or text similarity."""
    seen_ids: set[str] = set()
    seen_text: set[str] = set()
    result: list = []
    for spec in _as_list(a) + _as_list(b):
        if isinstance(spec, dict):
            sid = spec.get("spec_id", "")
            if sid and sid in seen_ids:
                continue
            text_key = (spec.get("text") or spec.get("description") or "").strip().lower()
            if text_key and text_key in seen_text:
                continue
            if sid:
                seen_ids.add(sid)
            if text_key:
                seen_text.add(text_key)
            result.append(spec)
        else:
            key = str(spec).strip().lower()
            if key and key not in seen_text:
                seen_text.add(key)
                result.append(spec)
    return result


def _union_reasons(a: list, b: list) -> list:
    """Union reasons_needed, deduplicating by reason_id or text."""
    seen_ids: set[str] = set()
    seen_text: set[str] = set()
    result: list = []
    for reason in _as_list(a) + _as_list(b):
        if isinstance(reason, dict):
            rid = reason.get("reason_id", "")
            if rid and rid in seen_ids:
                continue
            text_key = (reason.get("text") or reason.get("reason") or "").strip().lower()
            if text_key and text_key in seen_text:
                continue
            if rid:
                seen_ids.add(rid)
            if text_key:
                seen_text.add(text_key)
            result.append(reason)
        else:
            key = str(reason).strip().lower()
            if key and key not in seen_text:
                seen_text.add(key)
                result.append(reason)
    return result


def _higher_severity(a: str, b: str) -> str:
    return a if _severity_val(a) <= _severity_val(b) else b


def _higher_priority(a: str, b: str) -> str:
    return a if _priority_val(a) <= _priority_val(b) else b


def _merge_two(base: dict, other: dict) -> dict:
    """Merge two document requests that share the same merge key."""
    merged = dict(base)

    merged["severity"] = _higher_severity(
        base.get("severity", "SOFT-STOP"),
        other.get("severity", "SOFT-STOP"),
    )
    merged["priority"] = _higher_priority(
        base.get("priority", "P3"),
        other.get("priority", "P3"),
    )

    merged["specifications"] = _union_specs(
        base.get("specifications", []),
        other.get("specifications", []),
    )
    merged["reasons_needed"] = _union_reasons(
        base.get("reasons_needed", []),
        other.get("reasons_needed", []),
    )
    merged["evidence_found"] = _union_strings(
        _as_list(base.get("evidence_found", [])),
        _as_list(other.get("evidence_found", [])),
    )
    merged["tags"] = _union_strings(
        _as_list(base.get("tags", [])),
        _as_list(other.get("tags", [])),
    )
    merged["guideline_trace"] = _union_by_field(
        base.get("guideline_trace", []),
        other.get("guideline_trace", []),
        "section",
    )
    merged["overlay_trace"] = _union_by_field(
        base.get("overlay_trace", []),
        other.get("overlay_trace", []),
        "overlay_id",
    )

    return merged


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@tool
def merge_document_requests(
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
    state: Annotated[dict, InjectedState] = None,
) -> Command:
    """
    Collect document_requests from all modules (01-07), merge requests that
    refer to the same real-world document need (same document_type + context),
    aggregate specifications and reasons, and store the merged list.
    """
    s = state or {}
    module_outputs: dict = s.get("module_outputs", {})

    all_requests: list[dict] = []
    source_counts: dict[str, int] = {}

    _MODULE_CATEGORY = {
        "01": "Cross-Cutting", "02": "Income", "03": "Assets",
        "04": "Credit", "05": "Property", "06": "Title", "07": "Compliance",
    }

    for mod_key in ["01", "02", "03", "04", "05", "06", "07"]:
        mod = module_outputs.get(mod_key, {})
        raw_requests = _as_list(mod.get("document_requests", []))
        requests = normalize_all(raw_requests, default_category=_MODULE_CATEGORY.get(mod_key, "Other"))
        source_counts[mod_key] = len(requests)
        for dr in requests:
            dr.setdefault("source_module", mod_key)
        all_requests.extend(requests)

    groups: dict[str, dict] = {}
    for dr in all_requests:
        key = _merge_key(dr)
        if key in groups:
            groups[key] = _merge_two(groups[key], dr)
        else:
            groups[key] = dict(dr)

    merged = list(groups.values())

    sources_summary = ", ".join(
        f"{k}: {v}" for k, v in source_counts.items() if v > 0
    )
    msg = (
        f"Collected {len(all_requests)} document requests from modules "
        f"({sources_summary}) → {len(merged)} after merging by "
        f"document_type + context (de-duped {len(all_requests) - len(merged)})."
    )

    return Command(update={
        "module_outputs": {
            "08": {"merged_document_requests": merged},
        },
        "messages": [ToolMessage(msg, tool_call_id=tool_call_id)],
    })


@tool
def rank_document_requests(
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
    state: Annotated[dict, InjectedState] = None,
) -> Command:
    """
    Rank the merged document requests by severity (HARD-STOP first),
    priority (P0 first), then category order. Assign a status to each
    based on whether the document already exists in document_inventory.
    """
    s = state or {}
    mo = s.get("module_outputs", {})
    merged: list[dict] = _as_list(mo.get("08", {}).get("merged_document_requests", []))

    # Build a lookup of existing documents from document_inventory
    inventory: list[dict] = _as_list(s.get("document_inventory", []))
    inventory_types: set[str] = set()
    for doc in inventory:
        doc_type = (doc.get("document_type") or doc.get("doc_type") or "").strip().lower()
        if doc_type:
            inventory_types.add(doc_type)
        name = (doc.get("name") or doc.get("label") or "").strip().lower()
        if name:
            inventory_types.add(name)

    for dr in merged:
        doc_type_lower = (dr.get("document_type") or "").strip().lower()
        if doc_type_lower and doc_type_lower in inventory_types:
            dr["status"] = "satisfied_but_review_required"
        else:
            dr["status"] = "needed"

    ranked = sorted(merged, key=_sort_key)

    status_counts: dict[str, int] = {}
    for dr in ranked:
        st = dr.get("status", "unknown")
        status_counts[st] = status_counts.get(st, 0) + 1

    status_summary = ", ".join(f"{k}: {v}" for k, v in status_counts.items())
    msg = (
        f"Ranked {len(ranked)} document requests. "
        f"Status breakdown: {status_summary}."
    )

    return Command(update={
        "module_outputs": {"08": {"ranked_document_requests": ranked}},
        "messages": [ToolMessage(msg, tool_call_id=tool_call_id)],
    })


@tool
def generate_final_output(
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
    state: Annotated[dict, InjectedState] = None,
) -> Command:
    """
    Assemble the final output JSON: scenario_summary, seen_conflicts,
    ranked document_requests, and aggregate stats.
    """
    s = state or {}
    mo = s.get("module_outputs", {})

    document_requests: list[dict] = _as_list(
        mo.get("08", {}).get("ranked_document_requests", [])
    )
    if not document_requests:
        document_requests = _as_list(
            mo.get("08", {}).get("merged_document_requests", [])
        )

    scenario_summary = s.get("scenario_summary", {})
    clean_summary: dict[str, Any] = {
        k: v for k, v in scenario_summary.items()
        if not k.startswith("_")
    }

    seen_conflicts = _as_list(s.get("seen_conflicts", []))

    # Stats
    total = len(document_requests)
    hard_stops = sum(
        1 for dr in document_requests if dr.get("severity") == "HARD-STOP"
    )
    by_category: dict[str, int] = {}
    by_priority: dict[str, int] = {}
    by_status: dict[str, int] = {}
    for dr in document_requests:
        cat = dr.get("document_category", "Other")
        pri = dr.get("priority", "P3")
        st = dr.get("status", "unknown")
        by_category[cat] = by_category.get(cat, 0) + 1
        by_priority[pri] = by_priority.get(pri, 0) + 1
        by_status[st] = by_status.get(st, 0) + 1

    stats = {
        "total_document_requests": total,
        "hard_stop_documents": hard_stops,
        "by_category": by_category,
        "by_priority": by_priority,
        "by_status": by_status,
    }

    final: dict[str, Any] = {
        "scenario_summary": clean_summary,
        "seen_conflicts": seen_conflicts,
        "document_requests": document_requests,
        "stats": stats,
    }

    msg = (
        f"Final output generated: {total} document requests, "
        f"{hard_stops} hard-stop(s). "
        f"By priority: {by_priority}. By status: {by_status}."
    )

    return Command(update={
        "final_output": final,
        "current_step": "STEP_08",
        "messages": [ToolMessage(msg, tool_call_id=tool_call_id)],
    })
