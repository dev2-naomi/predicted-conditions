"""
title_tools.py — Tools for STEP_06: Title & Closing Document Requests.
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
def generate_title_document_requests(
    document_requests: List[Dict],
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
    state: Annotated[dict, InjectedState] = None,
) -> Command:
    """
    Store the title and closing document requests you generated after reasoning
    over the scenario_summary, submitted documents, and NQMF guideline sections.

    Args:
        document_requests: List of document request dicts conforming to the
                           standard document_request schema.
    """
    document_requests = normalize_all(document_requests, default_category="Title")

    for dr in document_requests:
        dr.setdefault("tags", [])
        if "title" not in dr["tags"]:
            dr["tags"].append("title")

    names = [dr.get("document_type", "?") for dr in document_requests]
    return Command(update={
        "module_outputs": {"06": {"document_requests": document_requests}},
        "current_step": "STEP_06",
        "messages": [ToolMessage(
            f"Stored {len(document_requests)} title document request(s): {names}",
            tool_call_id=tool_call_id,
        )],
    })
