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

from tools.doc_completeness_tools import _matches_manifest_name


# ---------------------------------------------------------------------------
# Priority ranking helpers
# ---------------------------------------------------------------------------

_SEVERITY_RANK = {"HARD-STOP": 0, "SOFT-STOP": 1, "INFO": 2}
_PRIORITY_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
_CATEGORY_RANK = {
    "Document Completeness": 0,
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

# ---------------------------------------------------------------------------
# Normalization maps — coerce LLM-variant values to canonical forms
# ---------------------------------------------------------------------------

_PRIORITY_NORMALIZE: dict[str, str] = {
    "P0": "P0", "P1": "P1", "P2": "P2", "P3": "P3",
    "0": "P0", "1": "P1", "2": "P2", "3": "P3", "4": "P3", "5": "P3",
    "6": "P3", "7": "P3", "8": "P3", "9": "P3",
    "HIGH": "P1", "MEDIUM": "P2", "LOW": "P3",
    "CRITICAL": "P0", "URGENT": "P0",
}

_SEVERITY_NORMALIZE: dict[str, str] = {
    "HARD-STOP": "HARD-STOP", "HARD_STOP": "HARD-STOP", "HARDSTOP": "HARD-STOP",
    "SOFT-STOP": "SOFT-STOP", "SOFT_STOP": "SOFT-STOP", "SOFTSTOP": "SOFT-STOP",
    "INFO": "INFO", "INFORMATION": "INFO", "WARNING": "SOFT-STOP",
}

_CATEGORY_NORMALIZE: dict[str, str] = {
    "document completeness": "Document Completeness",
    "document_completeness": "Document Completeness",
    "program eligibility": "Program Eligibility",
    "program_eligibility": "Program Eligibility",
    "compliance": "Compliance",
    "compliance & legal": "Compliance",
    "credit": "Credit",
    "credit analysis": "Credit",
    "income": "Income",
    "income documentation": "Income",
    "income verification": "Income",
    "assets": "Assets",
    "asset": "Assets",
    "asset documentation": "Assets",
    "asset verification": "Assets",
    "property": "Property",
    "property_appraisal": "Property",
    "property/appraisal": "Property",
    "appraisal": "Property",
    "title": "Title",
    "title_closing": "Title",
    "title/closing": "Title",
    "title & closing": "Title",
    "other": "Other",
}


def _normalize_priority(raw: Any) -> str:
    if isinstance(raw, int):
        return _PRIORITY_NORMALIZE.get(str(raw), "P3")
    s = str(raw).strip().upper()
    return _PRIORITY_NORMALIZE.get(s, "P3")


def _normalize_severity(raw: Any) -> str:
    s = str(raw).strip().upper().replace(" ", "-")
    return _SEVERITY_NORMALIZE.get(s, "SOFT-STOP")


def _normalize_category(raw: Any) -> str:
    s = str(raw).strip()
    return _CATEGORY_NORMALIZE.get(s.lower(), s if s else "Other")


_FIELD_ALIASES: dict[str, str] = {
    "condition_name": "title",
    "condition_text": "description",
    "detail": "description",
    "requirement": "description",
    "family": "condition_family_id",
    "id": "condition_id",
}


def _normalize_condition(c: dict | str) -> dict:
    """Normalize priority, severity, category, and field names to canonical values."""
    if isinstance(c, str):
        c = {"title": c, "description": c}
    if not isinstance(c, dict):
        c = {"title": str(c), "description": str(c)}
    for alias, canonical in _FIELD_ALIASES.items():
        if alias in c and c[alias]:
            existing = c.get(canonical)
            if not existing or existing == "Untitled condition":
                c[canonical] = c.pop(alias)
            else:
                del c[alias]

    c["priority"] = _normalize_priority(c.get("priority", "P3"))
    c["severity"] = _normalize_severity(c.get("severity", "SOFT-STOP"))
    c["category"] = _normalize_category(c.get("category", "Other"))
    if not c.get("title") or c["title"] == "Untitled condition":
        c["title"] = (c.get("description") or "Untitled condition")[:80]

    for list_field in ("overlay_trace", "guideline_trace", "required_documents",
                       "required_data_elements", "triggers", "evidence_found",
                       "resolution_criteria", "dependencies", "tags"):
        val = c.get(list_field)
        if isinstance(val, str):
            c[list_field] = [val] if val else []
    return c


def _sort_key(c: dict) -> tuple:
    return (
        _PRIORITY_RANK.get(c.get("priority", "P3"), 3),
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


def _union(a: list | dict | str, b: list | dict | str) -> list:
    if isinstance(a, dict):
        a = [a]
    elif isinstance(a, str):
        a = [a]
    if isinstance(b, dict):
        b = [b]
    elif isinstance(b, str):
        b = [b]
    seen: set = set()
    result = []
    for item in a + b:
        key = str(item)
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _union_by_key(a: list | dict | str, b: list | dict | str, key: str) -> list[dict]:
    if isinstance(a, dict):
        a = [a]
    elif isinstance(a, str):
        a = [{"value": a}]
    if isinstance(b, dict):
        b = [b]
    elif isinstance(b, str):
        b = [{"value": b}]
    seen: set = set()
    result = []
    for item in a + b:
        if isinstance(item, str):
            item = {"value": item}
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
# Title-based document satisfaction mapping
# ---------------------------------------------------------------------------
# Maps condition title keywords to manifest category names they reference.
# If a condition title matches a key and the manifest contains ANY of the
# corresponding category names, the condition is considered doc-satisfied.
_TITLE_TO_DOC_CATEGORIES: dict[str, list[str]] = {
    "bank statement": ["bank statement"],
    "asset statement": ["bank statement", "investment account statement"],
    "short funds to close": ["bank statement", "investment account statement"],
    "reserves": ["bank statement", "investment account statement"],
    "lease": ["lease agreement", "executed lease", "lease", "rental agreement"],
    "rent schedule": ["rent schedule", "1007", "1025", "appraisal report"],
}


def _is_doc_satisfied_by_title(cond: dict, manifest_categories: set[str]) -> bool:
    """Check if a condition's required document is already satisfied by a
    manifest category, based on the condition title rather than required_documents."""
    if not manifest_categories:
        return False
    title = (cond.get("title") or "").lower()
    for keyword, doc_cats in _TITLE_TO_DOC_CATEGORIES.items():
        if keyword in title:
            if any(dc in manifest_categories for dc in doc_cats):
                return True
    return False


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

    for module_key in ["00", "00b", "01", "02", "03", "04", "05", "06", "07", "08"]:
        mod = module_outputs.get(module_key, {})
        all_conditions.extend(mod.get("conditions", []))
        all_seen_conflicts.extend(mod.get("seen_conflicts", []))

    # Phase 0: Normalize all conditions to canonical priority/severity/category
    all_conditions = [_normalize_condition(c) for c in all_conditions]

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

    # Phase 1b: Strip already-submitted docs from each condition's
    # required_documents.  If the list becomes empty after stripping,
    # remove the condition entirely (all its requirements are met).
    step00b = module_outputs.get("00b", {})
    satisfied_docs = step00b.get("satisfied_documents", [])
    satisfied_labels: set[str] = {
        d["label"].strip().lower()
        for d in satisfied_docs
        if isinstance(d, dict) and d.get("label")
    }
    submitted_names: set[str] = set()
    for d in satisfied_docs:
        if isinstance(d, dict) and d.get("matched_manifest_name"):
            submitted_names.add(d["matched_manifest_name"].strip().lower())

    all_known: set[str] = satisfied_labels | submitted_names
    removed_doc_satisfied = 0
    if all_known:
        after_doc_filter: list[dict] = []
        for cond in filtered:
            if cond.get("category") == "Document Completeness":
                after_doc_filter.append(cond)
                continue

            req_docs = cond.get("required_documents", [])
            if not isinstance(req_docs, list) or not req_docs:
                after_doc_filter.append(cond)
                continue

            remaining = [
                doc for doc in req_docs
                if not any(
                    _matches_manifest_name(str(doc), label)
                    for label in all_known
                )
            ]

            if not remaining:
                removed_doc_satisfied += 1
                continue

            cond["required_documents"] = remaining
            after_doc_filter.append(cond)
        filtered = after_doc_filter

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
        if isinstance(overlay_trace, str):
            overlay_trace = [{"overlay_id": overlay_trace}]
            cond["overlay_trace"] = overlay_trace
        if not overlay_trace:
            resolved.append(cond)
            continue
        for ot in overlay_trace:
            if isinstance(ot, str):
                ot = {"overlay_id": ot}
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
        f"{removed_doc_satisfied} doc-satisfied, "
        f"de-duped {len(all_conditions) - removed_negative - removed_speculative - removed_doc_satisfied - len(resolved)} cross-module). "
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

    _SUMMARY_KEYS = (
        "program", "purpose", "occupancy",
        "property", "numbers", "loan_terms", "credit",
        "borrowers", "income_profile",
        "eligible_programs", "ineligible_programs",
    )
    clean_summary: dict[str, Any] = {}
    for k in _SUMMARY_KEYS:
        if k in scenario_summary:
            val = scenario_summary[k]
            if k == "property" and isinstance(val, dict):
                val = {pk: pv for pk, pv in val.items()
                       if pk in ("address", "state", "county", "city", "zip",
                                 "units", "property_type")}
            if k == "credit" and isinstance(val, dict):
                val = {ck: cv for ck, cv in val.items()
                       if ck in ("fico", "fico_source")}
            if k == "borrowers" and isinstance(val, list):
                val = [{"name": b.get("name"), "self_employed": b.get("self_employed"),
                         "citizenship": b.get("citizenship")} for b in val if isinstance(b, dict)]
            clean_summary[k] = val

    seen_conflicts = mo.get("09_merge", {}).get("seen_conflicts", [])

    # Run masterlist matching automatically
    ranked_conds = mo.get("09_rank", {}).get("ranked_conditions", [])
    if not ranked_conds:
        ranked_conds = mo.get("09_merge", {}).get("merged_conditions", [])

    ml_output = mo.get("09_masterlist", {})
    if not ml_output.get("matched_conditions") and not ml_output.get("passthrough_conditions"):
        from tools.masterlist_tools import run_masterlist_matching
        ml_output = run_masterlist_matching(ranked_conds, scenario_summary)

    ml_matched = ml_output.get("matched_conditions", [])
    ml_unmatched = ml_output.get("unmatched_conditions", [])
    ml_passthrough = ml_output.get("passthrough_conditions", [])
    ml_stats = ml_output.get("match_stats", {})

    if ml_matched or ml_passthrough:
        conditions = ml_passthrough + ml_matched
    else:
        conditions = ranked_conds

    # Post-masterlist context filter: remove conditions whose subject matter
    # is already satisfied by documents in the manifest.
    submitted_docs: list[dict] = scenario_summary.get("_submitted_docs", [])
    manifest_cats: set[str] = set()
    for d in submitted_docs:
        for field in ("category_name", "name"):
            val = d.get(field, "")
            if val:
                manifest_cats.add(val.strip().lower())
    if manifest_cats:
        pre_count = len(conditions)
        conditions = [
            c for c in conditions
            if c.get("category") == "Document Completeness"
            or not _is_doc_satisfied_by_title(c, manifest_cats)
        ]
        doc_title_removed = pre_count - len(conditions)
    else:
        doc_title_removed = 0

    # Stats
    hard_stops = sum(1 for c in conditions if c.get("severity") == "HARD-STOP")
    by_category: dict[str, int] = {}
    by_priority: dict[str, int] = {}
    for c in conditions:
        cat = c.get("category", "Other")
        pri = c.get("priority", "P3")
        by_category[cat] = by_category.get(cat, 0) + 1
        by_priority[pri] = by_priority.get(pri, 0) + 1

    _KEEP_FIELDS = ("category", "severity", "priority", "title", "description",
                     "required_documents", "required_data_elements")
    distilled = []
    for c in conditions:
        d = {}
        for k in _KEEP_FIELDS:
            val = c.get(k)
            if k in ("required_documents", "required_data_elements"):
                d[k] = val if isinstance(val, list) else []
            else:
                d[k] = val
        # Additive masterlist fields — only present when matched
        for mk in ("masterlist_id", "for_role", "prior_to",
                    "masterlist_documents", "match_confidence"):
            mv = c.get(mk)
            if mv is not None:
                d[mk] = mv
        distilled.append(d)

    final: dict[str, Any] = {
        "scenario_summary": clean_summary,
        "conditions": distilled,
        "conditions_full": conditions,
        "stats": {
            "total_conditions": len(conditions),
            "hard_stops": hard_stops,
            "by_category": by_category,
            "by_priority": by_priority,
        },
    }
    if ml_stats:
        final["masterlist_stats"] = ml_stats

    filter_note = ""
    if doc_title_removed:
        filter_note = f" ({doc_title_removed} removed: docs already in manifest)"

    return Command(update={
        "final_output": final,
        "current_step": "STEP_09",
        "messages": [ToolMessage(
            f"Final output generated: {len(conditions)} conditions, "
            f"{hard_stops} hard-stop(s). By priority: {by_priority}{filter_note}",
            tool_call_id=tool_call_id,
        )],
    })
