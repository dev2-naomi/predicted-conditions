"""
income_tools.py — Tools for STEP_02: Income Conditions Engine.

Two tools:
  - load_guideline_sections: loads actual guideline text from guidelines.md
    so the LLM can reason over the rules.
  - generate_income_conditions: thin storage tool — the LLM passes in the
    conditions it generated after reading the guidelines and scenario.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command
from typing_extensions import Annotated

from tools.shared.guidelines import load_sections


@tool
def load_guideline_sections(
    section_names: List[str],
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
    state: Annotated[dict, InjectedState] = None,
) -> str:
    """
    Load NQMF guideline content for the given section names.
    Returns the actual text content extracted from guidelines.md
    so you can reason over the rules and determine which conditions apply.

    Args:
        section_names: List of guideline section headings to load
                       (e.g. ["FULL DOCUMENTATION", "EMPLOYMENT"]).
    """
    return load_sections(section_names)


@tool
def generate_income_conditions(
    conditions: List[Dict],
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
    state: Annotated[dict, InjectedState] = None,
) -> Command:
    """
    Store the income conditions you generated after reasoning over the
    scenario_summary, submitted documents, and NQMF guideline sections.

    Each condition must conform to the standard schema with fields:
    condition_id, condition_family_id, category, title, description,
    required_documents, required_data_elements, owner, severity, priority,
    confidence, triggers, evidence_found, guideline_trace, overlay_trace,
    resolution_criteria, dependencies, tags.

    Args:
        conditions: List of condition dicts conforming to the standard schema.
    """
    for c in conditions:
        c.setdefault("tags", [])
        if "income" not in c["tags"]:
            c["tags"].append("income")

    titles = [c.get("title", "?") for c in conditions]
    return Command(update={
        "module_outputs": {"02": {"conditions": conditions}},
        "current_step": "STEP_02",
        "messages": [ToolMessage(
            f"Stored {len(conditions)} income condition(s): {titles}",
            tool_call_id=tool_call_id,
        )],
    })
