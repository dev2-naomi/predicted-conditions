"""
property_tools.py — Tools for STEP_05: Property & Appraisal Document Requests.
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
def generate_property_document_requests(
    document_requests: List[Dict],
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
    state: Annotated[dict, InjectedState] = None,
) -> Command:
    """
    Store the property and appraisal document requests you generated after
    reasoning over the scenario_summary, submitted documents, and NQMF
    guideline sections.

    Args:
        document_requests: List of document request dicts conforming to the
                           standard document_request schema.
    """
    normalize_all(document_requests, default_category="Property")

    for dr in document_requests:
        dr.setdefault("tags", [])
        if "property" not in dr["tags"]:
            dr["tags"].append("property")

    names = [dr.get("document_type", "?") for dr in document_requests]
    return Command(update={
        "module_outputs": {"05": {"document_requests": document_requests}},
        "current_step": "STEP_05",
        "messages": [ToolMessage(
            f"Stored {len(document_requests)} property document request(s): {names}",
            tool_call_id=tool_call_id,
        )],
    })
