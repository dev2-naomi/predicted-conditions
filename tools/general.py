"""
general.py — General-purpose tools available at every step.

These tools are always included regardless of the current step.
"""

from __future__ import annotations

import datetime
from typing import Any

from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command
from typing_extensions import Annotated


@tool
def write_todo(
    substep_id: str,
    name: str,
    status: str,
    note: str = "",
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
    state: Annotated[dict, InjectedState] = None,
) -> Command:
    """
    Track the status of a substep.

    Args:
        substep_id: The substep identifier (e.g. "0.1").
        name: Human-readable substep name.
        status: One of "pending", "in_progress", "completed", "skipped", "failed".
        note: Optional note about this substep's result or reason for status.
    """
    entry = {
        "substep_id": substep_id,
        "name": name,
        "status": status,
        "note": note,
        "updated_at": datetime.datetime.utcnow().isoformat(),
    }
    return Command(update={
        "todos": [entry],
        "messages": [ToolMessage(f"Todo '{substep_id}' set to {status}", tool_call_id=tool_call_id)],
    })


@tool
def add_flag(
    substep: str,
    title: str,
    severity: str,
    detail: str,
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
    state: Annotated[dict, InjectedState] = None,
) -> Command:
    """
    Add a flag to the workflow state.

    Args:
        substep: The substep identifier that triggered this flag.
        title: Short title describing the flag.
        severity: "HARD-STOP" | "SOFT-STOP" | "INFO".
        detail: Full description of what was flagged and why.
    """
    flag = {
        "substep": substep,
        "title": title,
        "severity": severity,
        "detail": detail,
        "flagged_at": datetime.datetime.utcnow().isoformat(),
    }
    return Command(update={
        "flags": [flag],
        "messages": [ToolMessage(f"Flag added: [{severity}] {title}", tool_call_id=tool_call_id)],
    })


_STEP_SEQUENCE = [
    "STEP_00", "STEP_00b", "STEP_01", "STEP_02", "STEP_03", "STEP_04",
    "STEP_05", "STEP_06", "STEP_07", "STEP_08", "STEP_09",
]


@tool
def save_step_report(
    step_id: str,
    summary: str,
    outputs: dict,
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
    state: Annotated[dict, InjectedState] = None,
) -> Command:
    """
    Persist the findings for a completed step and advance to the next step.
    You MUST call this after completing each step's tools.

    Args:
        step_id: The step identifier (e.g. "STEP_00").
        summary: A one-paragraph plain-English summary of what this step found.
        outputs: Dict of key results produced by this step (module output JSON).
    """
    report = {
        "step_id": step_id,
        "summary": summary,
        "outputs": outputs,
        "completed_at": datetime.datetime.utcnow().isoformat(),
    }

    idx = _STEP_SEQUENCE.index(step_id) if step_id in _STEP_SEQUENCE else -1
    if idx >= 0 and idx + 1 < len(_STEP_SEQUENCE):
        next_step = _STEP_SEQUENCE[idx + 1]
        msg = f"Step report saved for {step_id}. Advancing to {next_step}. Continue with {next_step} tools now."
    else:
        next_step = step_id
        msg = f"Step report saved for {step_id}. This is the final step."

    return Command(update={
        "step_reports": {step_id: report},
        "current_step": next_step,
        "messages": [ToolMessage(msg, tool_call_id=tool_call_id)],
    })


@tool
def get_workflow_status(
    state: Annotated[dict, InjectedState] = None,
) -> dict:
    """
    Return a summary of overall workflow progress: current step, completed steps,
    pending todos, and any flags raised.
    """
    s = state or {}
    todos: list[dict] = s.get("todos", [])
    flags: list[dict] = s.get("flags", [])
    step_reports: dict = s.get("step_reports", {})

    return {
        "current_step": s.get("current_step"),
        "completed_steps": list(step_reports.keys()),
        "todos": todos,
        "flag_count": len(flags),
        "hard_stops": sum(1 for f in flags if f.get("severity") == "HARD-STOP"),
    }
