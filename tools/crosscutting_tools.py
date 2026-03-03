"""
crosscutting_tools.py — Tools for STEP_01: Cross-Cutting Gatekeeper.

Generates conditions for missing core variables, contradictions,
overlay conflicts, and universal compliance prerequisites.

Cross-cutting conditions are structural (missing data, contradictions)
so they remain deterministic, but guideline_trace is populated from
the actual guidelines.md content.
"""

from __future__ import annotations

import uuid
from typing import Any

from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command
from typing_extensions import Annotated

from tools.shared.guidelines import build_guideline_trace


def _make_condition(
    condition_id: str,
    family_id: str,
    category: str,
    title: str,
    description: str,
    severity: str,
    priority: str,
    confidence: float,
    triggers: list[str],
    required_documents: list[str] | None = None,
    required_data_elements: list[str] | None = None,
    guideline_trace: list[dict] | None = None,
    overlay_trace: list[dict] | None = None,
    resolution_criteria: list[str] | None = None,
    tags: list[str] | None = None,
    owner: str = "Processor",
    evidence_found: list[str] | None = None,
    dependencies: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "condition_id": condition_id,
        "condition_family_id": family_id,
        "category": category,
        "title": title,
        "description": description,
        "required_documents": required_documents or [],
        "required_data_elements": required_data_elements or [],
        "owner": owner,
        "severity": severity,
        "priority": priority,
        "confidence": confidence,
        "triggers": triggers,
        "evidence_found": evidence_found or [],
        "guideline_trace": guideline_trace or [],
        "overlay_trace": overlay_trace or [],
        "resolution_criteria": resolution_criteria or [],
        "dependencies": dependencies or [],
        "tags": tags or ["crosscutting"],
    }


@tool
def check_overlay_conflicts(
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
    state: Annotated[dict, InjectedState] = None,
) -> Command:
    """
    Identify overlays that attempt to relax NQMF guidelines without
    exception_allowed=true and surface them as seen_conflicts.
    """
    s = state or {}
    ss = s.get("scenario_summary", {})
    entities: dict = (ss.get("_extracted_entities") or {}) if isinstance(ss, dict) else {}
    overlays: list[dict] = entities.get("overlays", [])

    seen_conflicts = []
    illegal_overlays = []

    for overlay in overlays:
        rule = overlay.get("rule_text", "").lower()
        exception_allowed = overlay.get("exception_allowed", False)

        relax_signals = [
            "waive", "exempt", "not required", "reduce", "allow", "permit",
            "override", "less than", "fewer than", "no need",
        ]
        is_relaxation = any(sig in rule for sig in relax_signals)

        if is_relaxation and not exception_allowed:
            seen_conflicts.append({
                "type": "OVERLAY_ILLEGAL_RELAXATION",
                "details": (
                    f"Overlay '{overlay.get('overlay_id')}' from '{overlay.get('source')}' "
                    f"appears to relax a guideline requirement without exception_allowed=true."
                ),
                "guideline_section": None,
                "overlay_id": overlay.get("overlay_id"),
            })
            illegal_overlays.append(overlay.get("overlay_id"))

    msg = f"Found {len(seen_conflicts)} overlay conflict(s), {len(illegal_overlays)} illegal relaxation(s)."
    return Command(update={
        "module_outputs": {
            "01_overlay_conflicts": {
                "seen_conflicts": seen_conflicts,
                "illegal_overlay_ids": illegal_overlays,
            }
        },
        "messages": [ToolMessage(msg, tool_call_id=tool_call_id)],
    })


@tool
def generate_crosscutting_conditions(
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
    state: Annotated[dict, InjectedState] = None,
) -> Command:
    """
    Generate cross-cutting conditions:
    - Missing core loan scenario variables (P0 HARD-STOP)
    - Discrepancy/contradiction resolution conditions
    - Illegal overlay relaxation conditions
    Returns conditions and seen_conflicts in module_outputs["01"].
    """
    s = state or {}
    ss = s.get("scenario_summary", {}) or {}
    missing: list[str] = s.get("missing_core_variables", [])
    contradictions: list[dict] = s.get("contradictions_detected", [])
    overlay_conflicts = (
        s.get("module_outputs", {}).get("01_overlay_conflicts", {})
    )
    illegal_overlay_ids: list[str] = overlay_conflicts.get("illegal_overlay_ids", [])
    prior_conflicts: list[dict] = overlay_conflicts.get("seen_conflicts", [])

    conditions: list[dict] = []
    seen_conflicts: list[dict] = list(prior_conflicts)

    # A) Missing core variables
    if missing:
        desc_lines = []
        for var in missing:
            reason_map = {
                "purpose": "required to determine transaction type and eligible programs.",
                "occupancy": "required to apply occupancy-specific guidelines and LTV limits.",
                "property_state": "required for state-specific compliance and guideline sections.",
                "loan_amount": "required for LTV calculation and eligibility limits.",
                "LTV": "required to verify against program maximum LTV.",
                "FICO": "required to determine credit eligibility.",
                "program": "required to scope applicable underwriting guidelines.",
                "income_documentation_type": "required to determine which income documentation standard applies.",
            }
            desc_lines.append(
                f"  - {var}: {reason_map.get(var, 'required for underwriting.')}"
            )
        conditions.append(_make_condition(
            condition_id="missing_core_loan_scenario_variables",
            family_id="CORE_SCENARIO_MISSING",
            category="Program Eligibility",
            title="Missing Core Loan Scenario Variables",
            description=(
                "The following required loan scenario variables are missing or unknown. "
                "Underwriting cannot proceed until these are resolved:\n"
                + "\n".join(desc_lines)
            ),
            severity="HARD-STOP",
            priority="P0",
            confidence=0.95,
            triggers=[f"missing: {v}" for v in missing],
            required_data_elements=missing,
            guideline_trace=build_guideline_trace(
                ["GENERAL UNDERWRITING REQUIREMENTS"], "eligibility"
            ),
            resolution_criteria=[
                f"Provide and confirm the value for: {v}" for v in missing
            ],
            tags=["crosscutting", "hard-stop", "missing-variables"],
        ))

    # B) Discrepancy/contradiction conditions
    family_map = {
        "NAME_MISMATCH": ("IDENTITY_NAME_MISMATCH", "Borrower Name Discrepancy", "P1"),
        "ADDRESS_MISMATCH": ("SUBJECT_PROPERTY_ADDRESS_MISMATCH", "Subject Property Address Discrepancy", "P0"),
        "OCCUPANCY_MISMATCH": ("OCCUPANCY_INCONSISTENCY", "Occupancy Inconsistency", "P0"),
        "VALUE_MISMATCH": ("APPRAISED_VALUE_DISCREPANCY", "Appraised Value Discrepancy", "P1"),
        "INCOME_MISMATCH": ("INCOME_DISCREPANCY", "Income Figure Discrepancy", "P1"),
        "OTHER": ("OTHER_DISCREPANCY", "Data Discrepancy", "P2"),
    }

    grouped: dict[str, list[dict]] = {}
    for contradiction in contradictions:
        ctype = contradiction.get("type", "OTHER")
        grouped.setdefault(ctype, []).append(contradiction)

    for ctype, group in grouped.items():
        family_id, title, priority = family_map.get(ctype, family_map["OTHER"])
        details = "; ".join(c.get("details", "") for c in group)
        conditions.append(_make_condition(
            condition_id=f"discrepancy_{family_id.lower()}",
            family_id=family_id,
            category="Other",
            title=title,
            description=(
                f"A {ctype.replace('_', ' ').lower()} was detected between the loan file "
                f"and submitted documents. Details: {details}"
            ),
            severity="HARD-STOP" if priority == "P0" else "SOFT-STOP",
            priority=priority,
            confidence=0.95,
            triggers=[f"{ctype} detected"] + [c.get("details", "")[:80] for c in group],
            required_documents=["Corrected/updated documents resolving the discrepancy"],
            required_data_elements=["Corrected field values from both sources"],
            guideline_trace=build_guideline_trace(
                ["GENERAL UNDERWRITING REQUIREMENTS"], "discrepan"
            ),
            resolution_criteria=[
                "Provide a written explanation and corrected documentation resolving "
                f"the {ctype.replace('_', ' ').lower()}."
            ],
            tags=["crosscutting", "discrepancy", ctype.lower()],
        ))
        seen_conflicts.append({
            "type": "DATA_CONTRADICTION",
            "details": details,
            "guideline_section": "GENERAL UNDERWRITING REQUIREMENTS",
            "overlay_id": None,
        })

    # C) Illegal overlay relaxation conditions
    for oid in illegal_overlay_ids:
        conditions.append(_make_condition(
            condition_id=f"overlay_illegal_relaxation_{oid}",
            family_id="OVERLAY_ILLEGAL_RELAXATION",
            category="Compliance",
            title="Overlay Illegal Relaxation Review Required",
            description=(
                f"Overlay '{oid}' attempts to relax a guideline requirement without "
                f"exception_allowed=true. This overlay cannot be applied as submitted. "
                f"Internal review required to confirm eligibility."
            ),
            severity="SOFT-STOP",
            priority="P1",
            confidence=0.75,
            triggers=[f"overlay {oid} attempts relaxation without exception"],
            required_data_elements=["exception_allowed confirmation from overlay source"],
            guideline_trace=build_guideline_trace(
                ["GENERAL UNDERWRITING REQUIREMENTS"], "overlay"
            ),
            overlay_trace=[{"overlay_id": oid}],
            resolution_criteria=[
                f"Confirm overlay '{oid}' has exception_allowed=true or remove the overlay."
            ],
            tags=["crosscutting", "overlay-conflict"],
        ))

    module_output = {
        "conditions": conditions,
        "seen_conflicts": seen_conflicts,
    }

    titles = [c["title"] for c in conditions]
    msg = f"Generated {len(conditions)} crosscutting condition(s): {titles}" if conditions else "No crosscutting conditions generated."
    return Command(update={
        "module_outputs": {"01": module_output},
        "current_step": "STEP_01",
        "messages": [ToolMessage(msg, tool_call_id=tool_call_id)],
    })
