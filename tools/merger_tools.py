"""
merger_tools.py — Tools for STEP_08: Merger, De-Duper, Conflict Resolver, Ranker.

Merges outputs from modules 01-07, de-duplicates by condition_family_id,
resolves conflicts, ranks by priority, and generates the final output JSON.
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import InjectedToolArg, tool
from langgraph.types import Command
from typing_extensions import Annotated


# ---------------------------------------------------------------------------
# Priority ranking helpers
# ---------------------------------------------------------------------------

_SEVERITY_RANK = {"HARD-STOP": 0, "SOFT-STOP": 1}
_PRIORITY_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
_CATEGORY_RANK = {
    "Program Eligibility": 0,
    "Compliance": 1,
    "Credit": 2,
    "Income": 3,
    "Assets": 4,
    "Property": 5,
    "Appraisal": 6,
    "Title": 7,
    "Other": 8,
}


def _sort_key(c: dict) -> tuple:
    return (
        _PRIORITY_RANK.get(c.get("priority", "P3"), 3),
        _SEVERITY_RANK.get(c.get("severity", "SOFT-STOP"), 1),
        _CATEGORY_RANK.get(c.get("category", "Other"), 8),
    )


def _choose_strictest(a: dict, b: dict) -> dict:
    """Return the stricter of two conditions in the same family."""
    if _SEVERITY_RANK.get(a.get("severity"), 1) <= _SEVERITY_RANK.get(b.get("severity"), 1):
        base = a
        other = b
    else:
        base = b
        other = a

    if _PRIORITY_RANK.get(a.get("priority"), 3) < _PRIORITY_RANK.get(b.get("priority"), 3):
        base = a
        other = b

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
# Tools
# ---------------------------------------------------------------------------


@tool
def merge_conditions(
    state: Annotated[dict, InjectedToolArg] = None,
) -> Command:
    """
    Merge and de-duplicate conditions from modules 01-07.
    Groups by condition_family_id, picks the strictest requirement per family.
    Also collects all seen_conflicts.
    Returns merged_conditions and all_seen_conflicts in module_outputs["08_merge"].
    """
    s = state or {}
    module_outputs: dict = s.get("module_outputs", {})

    all_conditions: list[dict] = []
    all_seen_conflicts: list[dict] = []

    for module_key in ["01", "02", "03", "04", "05", "06", "07"]:
        mod = module_outputs.get(module_key, {})
        all_conditions.extend(mod.get("conditions", []))
        all_seen_conflicts.extend(mod.get("seen_conflicts", []))

    # Group by condition_family_id
    families: dict[str, dict] = {}
    orphans: list[dict] = []

    for cond in all_conditions:
        fid = cond.get("condition_family_id")
        if not fid:
            orphans.append(cond)
            continue
        if fid in families:
            families[fid] = _choose_strictest(families[fid], cond)
        else:
            families[fid] = cond

    merged = list(families.values()) + orphans

    # Handle overlay conflict resolution
    resolved: list[dict] = []
    for cond in merged:
        overlay_trace = cond.get("overlay_trace", [])
        if not overlay_trace:
            resolved.append(cond)
            continue
        # Check for illegal relaxation overlays
        conflict_overlays = []
        for ot in overlay_trace:
            oid = ot.get("overlay_id", "")
            exceptions = (s.get("module_outputs", {})
                          .get("01_overlay_conflicts", {})
                          .get("illegal_overlay_ids", []))
            if oid in exceptions:
                conflict_overlays.append(oid)
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

    return Command(update={
        "module_outputs": {
            "08_merge": {
                "merged_conditions": resolved,
                "seen_conflicts": unique_conflicts,
            }
        }
    })


@tool
def rank_conditions(
    state: Annotated[dict, InjectedToolArg] = None,
) -> Command:
    """
    Apply final priority ranking to the merged conditions.
    Order: P0 HARD-STOP → P1 HARD-STOP → P1 SOFT-STOP → P2 → P3.
    Within same band: Program Eligibility, Compliance, Credit, Income,
    Assets, Property/Appraisal, Title/Closing.
    Returns ranked_conditions in module_outputs["08_rank"].
    """
    s = state or {}
    merged: list[dict] = (
        s.get("module_outputs", {}).get("08_merge", {}).get("merged_conditions", [])
    )

    ranked = sorted(merged, key=_sort_key)

    return Command(update={
        "module_outputs": {"08_rank": {"ranked_conditions": ranked}},
    })


@tool
def generate_final_output(
    state: Annotated[dict, InjectedToolArg] = None,
) -> Command:
    """
    Assemble the final output JSON per the schema in 08_MergerRanker.md and
    the orchestrator prompt.

    Returns the final_output dict in state, also stored in step_reports["STEP_08"].
    """
    s = state or {}
    mo = s.get("module_outputs", {})

    scenario_summary = s.get("scenario_summary", {})
    # Strip internal parsing keys from the public output
    clean_summary = {
        k: v for k, v in scenario_summary.items()
        if not k.startswith("_")
    }

    seen_conflicts = mo.get("08_merge", {}).get("seen_conflicts", [])
    conditions: list[dict] = mo.get("08_rank", {}).get("ranked_conditions", [])

    if not conditions:
        conditions = mo.get("08_merge", {}).get("merged_conditions", [])

    # Stats
    hard_stops = sum(1 for c in conditions if c.get("severity") == "HARD-STOP")
    by_category: dict[str, int] = {}
    by_priority: dict[str, int] = {}
    for c in conditions:
        cat = c.get("category", "Other")
        pri = c.get("priority", "P3")
        by_category[cat] = by_category.get(cat, 0) + 1
        by_priority[pri] = by_priority.get(pri, 0) + 1

    final: dict[str, Any] = {
        "scenario_summary": clean_summary,
        "seen_conflicts": seen_conflicts,
        "conditions": conditions,
        "stats": {
            "total_conditions": len(conditions),
            "hard_stops": hard_stops,
            "by_category": by_category,
            "by_priority": by_priority,
        },
    }

    return Command(update={
        "final_output": final,
        "current_step": "STEP_08",
    })
