"""
property_tools.py — Tools for STEP_05: Property & Appraisal Conditions Engine.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command
from typing_extensions import Annotated


@tool
def generate_property_conditions(
    conditions: List[Dict],
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
    state: Annotated[dict, InjectedState] = None,
) -> Command:
    """
    Store the property and appraisal conditions you generated after reasoning
    over the scenario_summary, submitted documents, and NQMF guideline sections.

    Each condition must conform to the standard schema.

    Args:
        conditions: List of condition dicts conforming to the standard schema.
    """
    for c in conditions:
        c.setdefault("tags", [])
        for tag in ("property", "appraisal"):
            if tag not in c["tags"]:
                c["tags"].append(tag)

    titles = [c.get("title", "?") for c in conditions]
    return Command(update={
        "module_outputs": {"05": {"conditions": conditions}},
        "current_step": "STEP_05",
        "messages": [ToolMessage(
            f"Stored {len(conditions)} property/appraisal condition(s): {titles}",
            tool_call_id=tool_call_id,
        )],
    })
