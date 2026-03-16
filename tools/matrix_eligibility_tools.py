"""
matrix_eligibility_tools.py — Tools for STEP_00c: Program Matrix Eligibility Check.

Two tools:
  1. load_program_matrix: loads the program-specific matrix section from
     program_matrices.md so the LLM can reason over the eligibility
     tables, LTV/FICO grids, and program-specific requirements.
  2. generate_matrix_conditions: the LLM passes in the conditions it
     generated after reasoning over the matrix vs. the loan scenario.
"""

from __future__ import annotations

from typing import Any, Dict, List

from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command
from typing_extensions import Annotated

from tools.shared.matrix_parser import get_program_matrix


@tool
def load_program_matrix(
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
    state: Annotated[dict, InjectedState] = None,
) -> str:
    """
    Load the program-specific eligibility matrix from program_matrices.md.

    Automatically reads the loan program from scenario_summary and returns
    the matching matrix section (LTV/FICO grids, credit requirements,
    income requirements, property requirements, etc.) plus the general
    requirements that apply to all programs.

    If the program cannot be resolved, returns a list of available programs.
    """
    s = state or {}
    ss = s.get("scenario_summary", {})
    program = ss.get("program") or "unknown"

    pm = get_program_matrix()
    canonical, section_text = pm.get_program_matrix(program)

    if not canonical or not section_text:
        available = pm.program_names
        return (
            f"Could not find program matrix for '{program}'. "
            f"Available programs: {available}. "
            f"Please verify the program name from the loan file."
        )

    return section_text


@tool
def generate_matrix_conditions(
    conditions: List[Dict],
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
    state: Annotated[dict, InjectedState] = None,
) -> Command:
    """
    Store the program eligibility conditions generated after reasoning
    over the program matrix, scenario_summary, and loan data.

    These conditions check whether the loan meets the program's
    specific requirements: LTV/FICO grid limits, property type eligibility,
    geographic restrictions, DTI caps, reserve requirements, income doc
    requirements, borrower eligibility, etc.

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
        if "matrix_eligibility" not in c["tags"]:
            c["tags"].append("matrix_eligibility")

    titles = [c.get("title", "?") for c in conditions]
    return Command(update={
        "module_outputs": {"00c": {"conditions": conditions}},
        "current_step": "STEP_00c",
        "messages": [ToolMessage(
            f"Stored {len(conditions)} program matrix condition(s): {titles}",
            tool_call_id=tool_call_id,
        )],
    })
