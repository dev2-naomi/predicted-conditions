"""
compliance_tools.py — Tools for STEP_07: Compliance Conditions Engine.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command
from typing_extensions import Annotated


@tool
def generate_compliance_conditions(
    conditions: List[Dict],
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
    state: Annotated[dict, InjectedState] = None,
) -> Command:
    """
    Store the compliance conditions you generated after reasoning over the
    scenario_summary, submitted documents, and NQMF guideline sections.

    Each condition must conform to the standard schema.

    Args:
        conditions: List of condition dicts conforming to the standard schema.
    """
    for c in conditions:
        c["category"] = "Compliance"
        c.setdefault("tags", [])
        if "compliance" not in c["tags"]:
            c["tags"].append("compliance")

    titles = [c.get("title", "?") for c in conditions]
    return Command(update={
        "module_outputs": {"07": {"conditions": conditions}},
        "current_step": "STEP_07",
        "messages": [ToolMessage(
            f"Stored {len(conditions)} compliance condition(s): {titles}",
            tool_call_id=tool_call_id,
        )],
    })
