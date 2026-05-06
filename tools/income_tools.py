"""
income_tools.py — Tools for STEP_02: Income Document Requests.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command
from typing_extensions import Annotated

from tools.shared.guidelines import load_sections
from tools.shared.normalize import normalize_all


@tool
def load_guideline_sections(
    section_names: List[str],
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
    state: Annotated[dict, InjectedState] = None,
) -> str:
    """
    Load NQMF guideline content for the given section names.
    Returns the actual text content extracted from guidelines.md
    so you can reason over the rules and determine which document requests apply.

    Args:
        section_names: List of guideline section headings to load
                       (e.g. ["FULL DOCUMENTATION", "EMPLOYMENT"]).
    """
    return load_sections(section_names)


@tool
def generate_income_document_requests(
    document_requests: List[Dict],
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
    state: Annotated[dict, InjectedState] = None,
) -> Command:
    """
    Store the income document requests you generated after reasoning over the
    scenario_summary, submitted documents, and NQMF guideline sections.

    Args:
        document_requests: List of document request dicts conforming to the
                           standard document_request schema.
    """
    normalize_all(document_requests, default_category="Income")

    for dr in document_requests:
        dr.setdefault("tags", [])
        if "income" not in dr["tags"]:
            dr["tags"].append("income")

    names = [dr.get("document_type", "?") for dr in document_requests]
    return Command(update={
        "module_outputs": {"02": {"document_requests": document_requests}},
        "current_step": "STEP_02",
        "messages": [ToolMessage(
            f"Stored {len(document_requests)} income document request(s): {names}",
            tool_call_id=tool_call_id,
        )],
    })
