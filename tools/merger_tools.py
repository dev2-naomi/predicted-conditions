"""
merger_tools.py — Tools for STEP_09: Merger, De-Duper, Conflict Resolver, Ranker.

Merges outputs from modules 01-07, de-duplicates by condition_family_id,
resolves conflicts, ranks by priority, and generates the final output JSON.
"""

from __future__ import annotations

import re
from typing import Any

from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command
from typing_extensions import Annotated


# ---------------------------------------------------------------------------
# Priority ranking helpers
# ---------------------------------------------------------------------------

_SEVERITY_RANK = {"HARD-STOP": 0, "SOFT-STOP": 1, "INFO": 2}
_PRIORITY_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
_CATEGORY_RANK = {
    "Program Eligibility": 0,
    "Compliance": 1,
    "Credit": 2,
    "Income": 3,
    "Assets": 4,
    "Property": 5,
    "Appraisal": 6,
    "property_appraisal": 5,
    "title_closing": 7,
    "Title": 7,
    "Other": 8,
    "income": 3,
    "assets": 4,
    "credit": 2,
    "compliance": 1,
}


def _sort_key(c: dict) -> tuple:
    pri = c.get("priority", "P3")
    if isinstance(pri, int):
        pri_rank = pri
    else:
        pri_rank = _PRIORITY_RANK.get(pri, 3)
    return (
        pri_rank,
        _SEVERITY_RANK.get(c.get("severity", "SOFT-STOP"), 1),
        _CATEGORY_RANK.get(c.get("category", "Other"), 8),
    )


def _choose_strictest(a: dict, b: dict) -> dict:
    """Return the stricter of two conditions in the same family."""
    if _SEVERITY_RANK.get(a.get("severity"), 1) <= _SEVERITY_RANK.get(b.get("severity"), 1):
        base, other = a, b
    else:
        base, other = b, a

    a_pri = _PRIORITY_RANK.get(a.get("priority"), 3) if isinstance(a.get("priority"), str) else a.get("priority", 3)
    b_pri = _PRIORITY_RANK.get(b.get("priority"), 3) if isinstance(b.get("priority"), str) else b.get("priority", 3)
    if a_pri < b_pri:
        base, other = a, b
    elif b_pri < a_pri:
        base, other = b, a

    merged = dict(base)
    merged["required_documents"] = _union(
        base.get("required_documents", []), other.get("required_documents", [])
    )
    merged["required_data_elements"] = _union(
        base.get("required_data_elements", []), other.get("required_data_elements", [])
    )
    merged["triggers"] = _union(base.get("triggers", []), other.get("triggers", []))
    merged["evidence_found"] = _union(base.get("evidence_found", []), other.get("evidence_found", []))
    merged["guideline_trace"] = _union_by_key(
        base.get("guideline_trace", []), other.get("guideline_trace", []), "section"
    )
    merged["overlay_trace"] = _union_by_key(
        base.get("overlay_trace", []), other.get("overlay_trace", []), "overlay_id"
    )
    merged["resolution_criteria"] = _union(
        base.get("resolution_criteria", []), other.get("resolution_criteria", [])
    )
    merged["dependencies"] = _union(base.get("dependencies", []), other.get("dependencies", []))
    merged["tags"] = _union(base.get("tags", []), other.get("tags", []))
    return merged


def _union(a: list, b: list) -> list:
    seen: set = set()
    result = []
    for item in a + b:
        key = str(item)
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _union_by_key(a: list[dict], b: list[dict], key: str) -> list[dict]:
    seen: set = set()
    result = []
    for item in a + b:
        k = item.get(key, str(item))
        if k not in seen:
            seen.add(k)
            result.append(item)
    return result


# ---------------------------------------------------------------------------
# Cross-module de-duplication
# ---------------------------------------------------------------------------

def _normalize_family_id(fid: str) -> str:
    """Normalize a condition_family_id for cross-module matching.
    Strips module prefixes, lowercases, collapses separators."""
    fid = fid.upper().strip()
    fid = re.sub(r"^(INC|AST|CRD|PROP|TTL|CMP|COMP)[_-]?\d*[_-]?", "", fid)
    fid = re.sub(r"[^A-Z0-9]", "_", fid)
    fid = re.sub(r"_+", "_", fid).strip("_")
    return fid


_CONCEPT_SYNONYMS: dict[str, str] = {
    "ENTITY_VESTING_NOT_PERMITTED": "ENTITY_VESTING_RESTRICTION",
    "ENTITY_VESTING_PROHIBITED": "ENTITY_VESTING_RESTRICTION",
    "ENTITY_VESTING_ITIN_PROHIBITED": "ENTITY_VESTING_RESTRICTION",
    "ENTITY_VESTING_ITIN": "ENTITY_VESTING_RESTRICTION",
    "ITIN_ENTITY_VESTING_PROHIBITED": "ENTITY_VESTING_RESTRICTION",
    "ITIN_ENTITY_VESTING": "ENTITY_VESTING_RESTRICTION",
    "VESTING_INDIVIDUAL_NAMES_ITIN": "ENTITY_VESTING_RESTRICTION",
    "ENTITY_VESTING_ITIN_PROHIBITION": "ENTITY_VESTING_RESTRICTION",
    "VESTING_VERIFICATION": "ENTITY_VESTING_RESTRICTION",
    "OFAC_VERIFICATION": "OFAC_SCREENING",
    "OFAC_EXCLUSIONARY_LIST": "OFAC_SCREENING",
    "FRAUD_ALERT_OFAC_VERIFICATION": "OFAC_SCREENING",
    "PATRIOT_ACT_OFAC_VERIFICATION": "OFAC_SCREENING",
    "OFAC_EXCLUSIONARY_LIST_SCREENING": "OFAC_SCREENING",
    "FRAUD_ALERT_OFAC": "OFAC_SCREENING",
    "CIP_VERIFICATION": "CIP_IDENTITY",
    "CIP_IDENTITY_VERIFICATION": "CIP_IDENTITY",
    "CUSTOMER_IDENTIFICATION_PROGRAM": "CIP_IDENTITY",
    "GOVERNMENT_ID_VERIFICATION": "GOVERNMENT_ID",
    "GOVERNMENT_PHOTO_ID_REQUIRED": "GOVERNMENT_ID",
    "GOVERNMENT_PHOTO_ID": "GOVERNMENT_ID",
    "HAZARD_INSURANCE": "PROPERTY_INSURANCE",
    "PROPERTY_INSURANCE_REQUIRED": "PROPERTY_INSURANCE",
    "FLOOD_INSURANCE": "FLOOD_DETERMINATION",
    "FLOOD_ZONE_DETERMINATION": "FLOOD_DETERMINATION",
    "FLOOD_DETERMINATION_AND_INSURANCE": "FLOOD_DETERMINATION",
}


def _canonical_family(fid: str) -> str:
    """Get canonical family for cross-module matching."""
    norm = _normalize_family_id(fid)
    return _CONCEPT_SYNONYMS.get(norm, norm)


# ---------------------------------------------------------------------------
# Post-merge quality filters
# ---------------------------------------------------------------------------

_TITLE_NEGATIVE_PATTERNS = [
    "not applicable",
    "not required",
    "not needed",
    "exemption applies",
    "does not apply",
    "n/a ",
    "- n/a",
    "waived",
]

_TITLE_SPECULATIVE_PATTERNS = [
    "if applicable",
    "if any",
    "as needed",
]


def _is_negative_condition(c: dict) -> bool:
    """True if this condition exists only to say something doesn't apply."""
    title = (c.get("title") or "").lower()

    if any(sig in title for sig in _TITLE_NEGATIVE_PATTERNS):
        return True

    sev = (c.get("severity") or "").upper()
    if sev not in ("HARD-STOP", "SOFT-STOP"):
        if any(sig in title for sig in ["exempt", "not required", "not applicable", "n/a"]):
            return True

    return False


def _is_speculative_condition(c: dict) -> bool:
    """True if the condition title explicitly marks it as hypothetical."""
    title = (c.get("title") or "").lower()
    return any(sig in title for sig in _TITLE_SPECULATIVE_PATTERNS)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@tool
def merge_conditions(
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
    state: Annotated[dict, InjectedState] = None,
) -> Command:
    """
    Merge and de-duplicate conditions from modules 01-07.
    Groups by condition_family_id (with cross-module normalization),
    picks the strictest requirement per family.
    Filters out 'not applicable' / speculative conditions.
    Returns merged_conditions and all_seen_conflicts in module_outputs["08_merge"].
    """
    s = state or {}
    module_outputs: dict = s.get("module_outputs", {})

    all_conditions: list[dict] = []
    all_seen_conflicts: list[dict] = []

    for module_key in ["01", "02", "03", "04", "05", "06", "07", "08"]:
        mod = module_outputs.get(module_key, {})
        all_conditions.extend(mod.get("conditions", []))
        all_seen_conflicts.extend(mod.get("seen_conflicts", []))

    # Phase 1: Filter out negative/speculative conditions
    filtered: list[dict] = []
    removed_negative = 0
    removed_speculative = 0
    for cond in all_conditions:
        if _is_negative_condition(cond):
            removed_negative += 1
            continue
        if _is_speculative_condition(cond):
            removed_speculative += 1
            continue
        filtered.append(cond)

    # Phase 2: Group by canonical family_id (cross-module aware)
    families: dict[str, dict] = {}
    orphans: list[dict] = []

    for cond in filtered:
        fid = cond.get("condition_family_id")
        if not fid:
            orphans.append(cond)
            continue
        canonical = _canonical_family(fid)
        if canonical in families:
            families[canonical] = _choose_strictest(families[canonical], cond)
        else:
            families[canonical] = cond

    merged = list(families.values()) + orphans

    # Phase 3: Handle overlay conflict resolution
    resolved: list[dict] = []
    for cond in merged:
        overlay_trace = cond.get("overlay_trace", [])
        if not overlay_trace:
            resolved.append(cond)
            continue
        for ot in overlay_trace:
            oid = ot.get("overlay_id", "")
            exceptions = (s.get("module_outputs", {})
                          .get("01_overlay_conflicts", {})
                          .get("illegal_overlay_ids", []))
            if oid in exceptions:
                if "overlay_conflict" not in cond.get("tags", []):
                    cond.setdefault("tags", []).append("overlay_conflict")
                all_seen_conflicts.append({
                    "type": "GUIDELINE_OVERLAY_CONFLICT",
                    "details": (
                        f"Overlay '{oid}' conflicts with guideline in condition "
                        f"'{cond.get('condition_id')}' (family: {cond.get('condition_family_id')})."
                    ),
                    "guideline_section": (
                        cond["guideline_trace"][0]["section"]
                        if cond.get("guideline_trace") else None
                    ),
                    "overlay_id": oid,
                })
        resolved.append(cond)

    # De-duplicate seen_conflicts
    unique_conflicts: list[dict] = []
    seen_conflict_keys: set[str] = set()
    for sc in all_seen_conflicts:
        key = f"{sc.get('type')}|{sc.get('overlay_id')}|{sc.get('details', '')[:60]}"
        if key not in seen_conflict_keys:
            seen_conflict_keys.add(key)
            unique_conflicts.append(sc)

    msg = (
        f"Merged {len(all_conditions)} raw conditions → {len(resolved)} final "
        f"(removed {removed_negative} negative, {removed_speculative} speculative, "
        f"de-duped {len(all_conditions) - removed_negative - removed_speculative - len(resolved)} cross-module). "
        f"{len(unique_conflicts)} conflict(s)."
    )
    return Command(update={
        "module_outputs": {
            "09_merge": {
                "merged_conditions": resolved,
                "seen_conflicts": unique_conflicts,
            }
        },
        "messages": [ToolMessage(msg, tool_call_id=tool_call_id)],
    })


@tool
def rank_conditions(
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
    state: Annotated[dict, InjectedState] = None,
) -> Command:
    """
    Apply final priority ranking to the merged conditions.
    Order: P0 HARD-STOP → P1 HARD-STOP → P1 SOFT-STOP → P2 → P3.
    Within same band: Program Eligibility, Compliance, Credit, Income,
    Assets, Property/Appraisal, Title/Closing.
    Returns ranked_conditions in module_outputs["09_rank"].
    """
    s = state or {}
    merged: list[dict] = (
        s.get("module_outputs", {}).get("09_merge", {}).get("merged_conditions", [])
    )

    ranked = sorted(merged, key=_sort_key)

    return Command(update={
        "module_outputs": {"09_rank": {"ranked_conditions": ranked}},
        "messages": [ToolMessage(
            f"Ranked {len(ranked)} conditions by priority and severity.",
            tool_call_id=tool_call_id,
        )],
    })


@tool
def generate_final_output(
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
    state: Annotated[dict, InjectedState] = None,
) -> Command:
    """
    Assemble the final output JSON per the schema in 08_MergerRanker.md and
    the orchestrator prompt.

    Returns the final_output dict in state, also stored in step_reports["STEP_09"].
    """
    s = state or {}
    mo = s.get("module_outputs", {})

    scenario_summary = s.get("scenario_summary", {})
    clean_summary = {
        k: v for k, v in scenario_summary.items()
        if not k.startswith("_")
    }

    seen_conflicts = mo.get("09_merge", {}).get("seen_conflicts", [])
    conditions: list[dict] = mo.get("09_rank", {}).get("ranked_conditions", [])

    if not conditions:
        conditions = mo.get("09_merge", {}).get("merged_conditions", [])

    # Stats
    hard_stops = sum(1 for c in conditions if c.get("severity") == "HARD-STOP")
    by_category: dict[str, int] = {}
    by_priority: dict[str, int] = {}
    for c in conditions:
        cat = c.get("category", "Other")
        pri = c.get("priority", "P3")
        by_category[cat] = by_category.get(cat, 0) + 1
        by_priority[pri] = by_priority.get(pri, 0) + 1

    # Distilled conditions: only the fields an underwriter needs to act on
    _KEEP_FIELDS = ("category", "severity", "title", "description", "required_documents", "required_data_elements")
    distilled = []
    for c in conditions:
        distilled.append({k: c.get(k) for k in _KEEP_FIELDS})

    final: dict[str, Any] = {
        "scenario_summary": clean_summary,
        "seen_conflicts": seen_conflicts,
        "conditions": distilled,
        "conditions_full": conditions,
        "stats": {
            "total_conditions": len(conditions),
            "hard_stops": hard_stops,
            "by_category": by_category,
            "by_priority": by_priority,
        },
    }

    return Command(update={
        "final_output": final,
        "current_step": "STEP_09",
        "messages": [ToolMessage(
            f"Final output generated: {len(conditions)} conditions, "
            f"{hard_stops} hard-stop(s). By priority: {by_priority}",
            tool_call_id=tool_call_id,
        )],
    })
