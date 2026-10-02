"""
step_loader.py — Utilities for loading plan files and resolving tools per step.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

from registry import (
    GENERAL_TOOL_NAMES,
    get_current_step,
    get_step_plan_file,
    get_step_tools,
    is_step_skipped,
)

PLANS_DIR = Path(__file__).parent / "plans"


# ---------------------------------------------------------------------------
# Plan loading
# ---------------------------------------------------------------------------


def load_plan_content(step_id: str) -> str | None:
    """Read and return the markdown plan for the given step."""
    plan_file = get_step_plan_file(step_id)
    if not plan_file:
        return None
    path = PLANS_DIR / plan_file
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def load_system_prompt() -> str:
    path = PLANS_DIR / "system_prompt.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return "You are the SBIQ AI Predictive Conditions Orchestrator."


# ---------------------------------------------------------------------------
# Tool resolution
# ---------------------------------------------------------------------------


def _import_all_tools() -> dict[str, Any]:
    """
    Lazily import every @tool callable from the tools package.
    Returns a mapping of tool_name -> callable.
    """
    from tools import ALL_TOOLS  # noqa: PLC0415

    return {t.name: t for t in ALL_TOOLS}


_TOOL_REGISTRY: dict[str, Any] | None = None


def get_tool_registry() -> dict[str, Any]:
    global _TOOL_REGISTRY
    if _TOOL_REGISTRY is None:
        _TOOL_REGISTRY = _import_all_tools()
    return _TOOL_REGISTRY


# STEP_08 (merge -> rank -> cross_check -> generate_final_output) has a
# strict data dependency chain: each tool reads the module_outputs["08"]
# key (or, for generate_final_output, final_output) that the previous one
# just wrote, via InjectedState. LangGraph's ToolNode dispatches every tool
# call named in a single AIMessage CONCURRENTLY against one shared state
# snapshot (langgraph.prebuilt.tool_node.ToolNode._func uses
# executor.map(self._run_one, tool_calls, ...) and only merges all of their
# Command updates into the graph state once every call in that batch has
# returned) — so if the model ever batches two of these four into the same
# turn (Anthropic/OpenAI both allow multiple tool_use blocks per response
# by default), the later one silently reads pre-update state. Confirmed
# live: a run where the model batched merge_document_requests +
# rank_document_requests together produced "Ranked 0" (rank read an empty
# merged_document_requests), with the loss masked downstream only because
# generate_final_output happens to fall back to merged_document_requests
# when ranked_document_requests is empty.
#
# Disabling parallel tool calls globally (bind_tools(parallel_tool_calls=
# False)) would close this, but was rejected: a real run showed ~60% of
# all multi-call batches are benign (write_todo bundled alongside a real
# tool call), so a blanket disable would roughly double LLM round-trips for
# the ENTIRE pipeline just to fix this one four-tool chain — real risk
# against the 900s Lambda timeout on larger loan files. Instead, only
# expose ONE of these four tools at a time while on STEP_08, advancing
# strictly based on which stage of module_outputs["08"] is already
# populated — making it physically impossible for the model to batch two
# of them together, without touching tool-call parallelism anywhere else
# (write_todo etc. are unaffected since they're general tools, always
# available alongside whichever single STEP_08 tool is currently exposed).
_STEP_08_CHAIN: list[tuple[str, Any]] = [
    ("merge_document_requests", lambda mo08, state: "merged_document_requests" not in mo08),
    ("rank_document_requests", lambda mo08, state: not mo08.get("rank_done")),
    ("cross_check_satisfaction", lambda mo08, state: not mo08.get("cross_check_done")),
    ("generate_final_output", lambda mo08, state: not state.get("final_output")),
]


def _gate_step_08_tools(state: dict, step_tool_names: list[str]) -> list[str]:
    """Return the single next STEP_08 tool name still owed, or `step_tool_names`
    unchanged (defensive fallback) if the chain tool isn't actually in it, or
    [] once all four have run (only save_step_report, a general tool, remains)."""
    mo08 = (state.get("module_outputs") or {}).get("08") or {}
    for name, still_pending in _STEP_08_CHAIN:
        if still_pending(mo08, state):
            return [name] if name in step_tool_names else step_tool_names
    return []


def resolve_tools_for_step(state: dict) -> list[Any]:
    """
    Tool resolver called before every LLM invocation.
    Returns only the tools relevant to the current step plus general tools.
    """
    registry = get_tool_registry()
    # Default to STEP_00 when the caller did not seed current_step in the
    # initial state. Without this, a fresh run (which only sends
    # loan_file_xml/manifest_json/eligibility_json) would bind only the
    # general tools, so the STEP_00 parse tools never become available and
    # the model advances past ingestion with an empty scenario.
    current_step = get_current_step(state) or "STEP_00"

    general_tools = [registry[name] for name in GENERAL_TOOL_NAMES if name in registry]

    if is_step_skipped(current_step, state):
        return [registry["write_todo"]] if "write_todo" in registry else general_tools

    step_tool_names = get_step_tools(current_step)
    if current_step == "STEP_08":
        step_tool_names = _gate_step_08_tools(state, step_tool_names)
    step_tools = [registry[name] for name in step_tool_names if name in registry]

    # Deduplicate while preserving order (general tools first)
    seen: set[str] = set()
    result: list[Any] = []
    for tool in general_tools + step_tools:
        if tool.name not in seen:
            seen.add(tool.name)
            result.append(tool)

    return result


def resolve_plan_for_step(state: dict) -> str | None:
    """
    Plan resolver called before every LLM invocation.
    Returns the markdown plan for the current step as a transient system message.
    """
    current_step = get_current_step(state) or "STEP_00"
    if is_step_skipped(current_step, state):
        return None
    return load_plan_content(current_step)
