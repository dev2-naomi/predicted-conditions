"""
general.py — General-purpose tools available at every step.

These tools are always included regardless of the current step.
"""

from __future__ import annotations

import datetime
from typing import Any

from langchain_core.tools import InjectedToolArg, tool
from langgraph.types import Command
from typing_extensions import Annotated


@tool
def write_todo(
    substep_id: str,
    name: str,
    status: str,
    note: str = "",
    state: Annotated[dict, InjectedToolArg] = None,
) -> Command:
    """
    Track the status of a substep.

    Args:
        substep_id: The substep identifier (e.g. "0.1").
        name: Human-readable substep name.
        status: One of "pending", "in_progress", "completed", "skipped", "failed".
        note: Optional note about this substep's result or reason for status.
    """
    existing: list[dict] = (state or {}).get("todos", [])
    entry = {
        "substep_id": substep_id,
        "name": name,
        "status": status,
        "note": note,
        "updated_at": datetime.datetime.utcnow().isoformat(),
    }
    # Replace existing entry for this substep_id if present
    updated = [e for e in existing if e.get("substep_id") != substep_id]
    updated.append(entry)
    return Command(update={"todos": [entry]})


@tool
def add_flag(
    substep: str,
    title: str,
    severity: str,
    detail: str,
    state: Annotated[dict, InjectedToolArg] = None,
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
    return Command(update={"flags": [flag]})


@tool
def save_step_report(
    step_id: str,
    summary: str,
    outputs: dict,
    state: Annotated[dict, InjectedToolArg] = None,
) -> Command:
    """
    Persist the findings for a completed step.

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
    return Command(update={"step_reports": {step_id: report}})


@tool
def get_workflow_status(
    state: Annotated[dict, InjectedToolArg] = None,
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
