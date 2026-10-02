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


_STEP_SEQUENCE = [
    "STEP_00", "STEP_01", "STEP_02", "STEP_03", "STEP_04",
    "STEP_05", "STEP_06", "STEP_07", "STEP_08", "STEP_09",
]


# ---------------------------------------------------------------------------
# Step-completion validators
#
# save_step_report used to trust the LLM's self-reported summary/outputs
# unconditionally and always advance current_step — nothing ever checked
# that the step's *actual* required tool call had run and produced its
# expected effect on state. This let a step narrate a detailed, plausible
# "completed" summary (and even a fabricated write_todo "note") while
# silently skipping the one tool call that does the real work — observed in
# production on a STEP_09 run for borrower Mark Kashana, where the agent
# marked substep 9.2 "completed" and wrote a full styled-documents summary
# in save_step_report's own `summary` field, without ever calling
# style_document_requests. The run still reported overall "success" because
# nothing else checked for this.
#
# These validators run a cheap, deterministic (non-LLM-judged) check of the
# actual resulting state before a step is allowed to advance. A validator
# returns None when the step's real effect is present, or an error string
# (surfaced back to the agent instead of advancing) when it is not.
# ---------------------------------------------------------------------------


def _validate_step_09(state: dict) -> str | None:
    """STEP_09 must have styled EVERY document request via
    style_document_requests before it can be marked complete.

    style_document_requests unconditionally sets
    ``display["document_ids"]`` and (when the LLM supplied a heading)
    ``display["document_heading"]`` for every document_request it touches —
    so a request with no styled display block (just the bare
    ``{"party": ...}`` stamped on by the co-borrower pass) is direct,
    unambiguous proof that the tool was never called for it.
    """
    final_output = (state or {}).get("final_output") or {}
    drs = final_output.get("document_requests") or []
    if not drs:
        return None  # nothing to style — trivially satisfied

    missing = [
        dr.get("document_type", "<unknown>")
        for dr in drs
        if not (dr.get("display") or {}).get("document_heading")
    ]
    if not missing:
        return None

    return (
        f"{len(missing)} of {len(drs)} document_requests are missing a styled "
        f"display block (display.document_heading is not set): {missing}. "
        f"You must call style_document_requests for ALL of these — writing a "
        f"todo note or a step-report summary describing styling work does NOT "
        f"style them. Call style_document_requests now (splitting into "
        f"multiple batches if needed), then call save_step_report again."
    )


def _validate_step_08(state: dict) -> str | None:
    """STEP_08 must have actually run merge_document_requests ->
    rank_document_requests -> cross_check_satisfaction ->
    generate_final_output in sequence before it can be marked complete.

    step_loader._gate_step_08_tools already makes it physically impossible
    for the model to batch two of these four into the same turn (closing
    the known race — see that function's module comment), but this
    validator is the defense-in-depth backstop: it checks the same
    completion markers from the *consuming* side, independent of whatever
    produced them, so any future regression (a new batching pattern, a
    tool raising instead of returning, etc.) fails loudly here instead of
    silently advancing to STEP_09 with hollowed-out output.
    """
    mo08 = (state.get("module_outputs") or {}).get("08") or {}
    missing: list[str] = []
    if "merged_document_requests" not in mo08:
        missing.append("merge_document_requests (merged_document_requests not set)")
    if not mo08.get("rank_done"):
        missing.append("rank_document_requests (rank_done marker not set)")
    if not mo08.get("cross_check_done"):
        missing.append("cross_check_satisfaction (cross_check_done marker not set)")
    if not state.get("final_output"):
        missing.append("generate_final_output (final_output not set)")
    if not missing:
        return None
    return (
        f"STEP_08 is incomplete: {'; '.join(missing)}. Call the missing "
        f"tool(s) now — ONE AT A TIME, never batched together in the same "
        f"turn, since each reads the previous tool's output from state — "
        f"then call save_step_report again."
    )


_STEP_COMPLETION_VALIDATORS: dict[str, Any] = {
    "STEP_08": _validate_step_08,
    "STEP_09": _validate_step_09,
}


# ---------------------------------------------------------------------------
# current_step regression guard
#
# Root cause of a second, more direct manifestation of the same Mark Kashana
# incident: on a workflow restart/resume, the agent sometimes redundantly
# re-calls save_step_report for an EARLIER step_id (e.g. "STEP_00") even
# though later steps (up through STEP_08) had already completed and
# advanced current_step to STEP_09. The old code computed next_step purely
# from the *argument* step_id — `_STEP_SEQUENCE[index(step_id) + 1]` — with
# no awareness of how far the workflow had actually progressed. That single
# stray call silently regressed current_step from STEP_09 back to STEP_01.
# Because step tools are bound per current_step (step_loader.py), the very
# next turn lost access to style_document_requests entirely — the agent
# then correctly (and helplessly) reported "I don't have access to
# style_document_requests" and gave up with a prose summary claiming the
# workflow was "effectively complete", ending the run on an AI message with
# no tool call. The graph terminates there and the run still reports
# `status: success`, even though STEP_09 (styling) never ran and every
# document_request's display block stayed bare.
#
# Fix: derive the furthest already-completed step from the *accumulated*
# step_reports in state (not just the incoming step_id) and never let
# current_step move backward from that.
# ---------------------------------------------------------------------------


def _resolve_next_step(step_id: str, existing_step_reports: dict) -> tuple[str, bool]:
    """Return (next_step, is_regression) for a save_step_report call.

    is_regression is True when `step_id` is a stale re-affirmation of a step
    that has already been superseded by later entries in
    `existing_step_reports` — in that case next_step is the step *after* the
    furthest already-completed one, not merely after `step_id`.
    """
    idx = _STEP_SEQUENCE.index(step_id) if step_id in _STEP_SEQUENCE else -1
    furthest_idx = idx
    for sid in (existing_step_reports or {}):
        if sid in _STEP_SEQUENCE:
            furthest_idx = max(furthest_idx, _STEP_SEQUENCE.index(sid))

    is_regression = furthest_idx > idx
    if furthest_idx < 0:
        return step_id, False
    if furthest_idx + 1 < len(_STEP_SEQUENCE):
        return _STEP_SEQUENCE[furthest_idx + 1], is_regression
    return _STEP_SEQUENCE[furthest_idx], is_regression


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
    validator = _STEP_COMPLETION_VALIDATORS.get(step_id)
    if validator is not None:
        error = validator(state or {})
        if error:
            # Do NOT advance current_step or persist a step_report — the
            # step's real effect is not actually present in state yet.
            return Command(update={
                "messages": [ToolMessage(
                    f"save_step_report REJECTED for {step_id}: {error} "
                    f"current_step remains {step_id}.",
                    tool_call_id=tool_call_id,
                )],
            })

    report = {
        "step_id": step_id,
        "summary": summary,
        "outputs": outputs,
        "completed_at": datetime.datetime.utcnow().isoformat(),
    }

    existing_step_reports = (state or {}).get("step_reports") or {}
    next_step, is_regression = _resolve_next_step(step_id, existing_step_reports)

    if is_regression:
        # Deliberately does NOT start with "Step report saved for " — agent.py's
        # _extract_step_from_tool_message/_summarize_completed_steps scans for
        # that exact prefix to find step-advance boundaries in message history.
        # An earlier version of this message used that prefix and was
        # misparsed as a fresh "step advanced away from STEP_00" boundary on
        # every restart, repeatedly re-anchoring the model's visible context
        # at this stale re-affirmation and hiding real progress on the actual
        # current step — causing an infinite STEP_00-checklist retry loop
        # instead of ever reaching the real current step. See agent.py's
        # _extract_step_from_tool_message docstring for the full incident.
        msg = (
            f"No-op: {step_id} was already completed (this is a stale re-affirmation) "
            f"— the workflow had already advanced past {step_id}. current_step "
            f"remains {next_step}; do NOT repeat earlier steps. Continue with {next_step}'s "
            f"tools now."
        )
    elif next_step == step_id:
        msg = f"Step report saved for {step_id}. This is the final step."
    else:
        msg = f"Step report saved for {step_id}. Advancing to {next_step}. Continue with {next_step} tools now."

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
    and pending todos.
    """
    s = state or {}
    todos: list[dict] = s.get("todos", [])
    step_reports: dict = s.get("step_reports", {})

    return {
        "current_step": s.get("current_step"),
        "completed_steps": list(step_reports.keys()),
        "todos": todos,
    }
