"""
crosscutting_tools.py — Tools for STEP_01: Cross-Cutting Gatekeeper.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command
from typing_extensions import Annotated

from tools.shared.normalize import normalize_all


@tool
def check_overlay_conflicts(
    overlays: List[Dict],
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
    state: Annotated[dict, InjectedState] = None,
) -> Command:
    """
    Check a list of overlays for illegal relaxations (overlays that attempt
    to relax a guideline requirement without exception_allowed=true).
    Stores any seen_conflicts in state for downstream use.

    Args:
        overlays: List of overlay dicts to check. Each should have at minimum
                  overlay_id, rule_text, and exception_allowed fields.
    """
    seen_conflicts: List[Dict] = []

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
                    f"Overlay '{overlay.get('overlay_id')}' from "
                    f"'{overlay.get('source')}' appears to relax a guideline "
                    f"requirement without exception_allowed=true."
                ),
                "overlay_id": overlay.get("overlay_id"),
            })

    msg = f"Checked {len(overlays)} overlay(s), found {len(seen_conflicts)} illegal relaxation(s)."
    return Command(update={
        "seen_conflicts": seen_conflicts,
        "messages": [ToolMessage(msg, tool_call_id=tool_call_id)],
    })


@tool
def generate_crosscutting_document_requests(
    document_requests: List[Dict],
    seen_conflicts: List[Dict] = None,
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
    state: Annotated[dict, InjectedState] = None,
) -> Command:
    """
    Store the cross-cutting document requests you generated after reasoning
    over missing core variables, contradictions, and overlay conflicts.

    Args:
        document_requests: List of document request dicts conforming to the
                           standard document_request schema.
        seen_conflicts: List of conflict dicts surfaced during cross-cutting
                        analysis (overlay conflicts, data contradictions, etc.).
    """
    document_requests = normalize_all(document_requests, default_category="Cross-Cutting")

    for dr in document_requests:
        dr.setdefault("tags", [])
        if "crosscutting" not in dr["tags"]:
            dr["tags"].append("crosscutting")

    names = [dr.get("document_type", "?") for dr in document_requests]
    msg = (
        f"Stored {len(document_requests)} crosscutting document request(s): {names}"
        if document_requests
        else "No crosscutting document requests generated."
    )
    return Command(update={
        "module_outputs": {"01": {
            "document_requests": document_requests,
            "seen_conflicts": seen_conflicts or [],
        }},
        "current_step": "STEP_01",
        "messages": [ToolMessage(msg, tool_call_id=tool_call_id)],
    })
