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


def resolve_tools_for_step(state: dict) -> list[Any]:
    """
    Tool resolver called before every LLM invocation.
    Returns only the tools relevant to the current step plus general tools.
    """
    registry = get_tool_registry()
    current_step = get_current_step(state)

    general_tools = [registry[name] for name in GENERAL_TOOL_NAMES if name in registry]

    if not current_step:
        return general_tools

    if is_step_skipped(current_step, state):
        return [registry["write_todo"]] if "write_todo" in registry else general_tools

    step_tool_names = get_step_tools(current_step)
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
    current_step = get_current_step(state)
    if not current_step:
        return None
    if is_step_skipped(current_step, state):
        return None
    return load_plan_content(current_step)
