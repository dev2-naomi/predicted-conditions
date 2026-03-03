"""
agent.py — Main orchestrator agent definition.

Entry point referenced by langgraph.json:
    "predicted-conditions": "./agent.py:agent"

Architecture:
- Single ReAct agent loop using LangGraph StateGraph.
- DynamicToolMiddleware: calls tool_resolver(state) before each LLM invocation
  to scope available tools to the current step (60-75% context reduction).
- DynamicPlanMiddleware: injects the current step's plan as a transient
  system message before each LLM invocation (not persisted in history).
- SummarizationMiddleware: stub — extend to compress completed-step context.
"""

from __future__ import annotations

import os
from typing import Annotated, Any, Literal
from typing_extensions import NotRequired

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt.tool_node import ToolNode
from typing_extensions import TypedDict

from step_loader import load_system_prompt, resolve_plan_for_step, resolve_tools_for_step
from tools import ALL_TOOLS

# ---------------------------------------------------------------------------
# Custom reducers
# ---------------------------------------------------------------------------


def _merge_dicts(old: dict | None, new: dict | None) -> dict:
    if old is None:
        old = {}
    if new is None:
        return old
    merged = dict(old)
    for k, v in new.items():
        if k in merged and isinstance(merged[k], dict) and isinstance(v, dict):
            merged[k] = _merge_dicts(merged[k], v)
        else:
            merged[k] = v
    return merged


def _append_list(old: list | None, new: list | None) -> list:
    return (old or []) + (new or [])


def _dedupe_flags(old: list | None, new: list | None) -> list:
    combined = (old or []) + (new or [])
    seen: set[tuple] = set()
    result: list[dict] = []
    for flag in combined:
        key = (flag.get("substep", ""), flag.get("title", ""))
        if key not in seen:
            seen.add(key)
            result.append(flag)
    return result


def _last_value(old: Any, new: Any) -> Any:  # noqa: ARG001
    return new


# ---------------------------------------------------------------------------
# State schema
# ---------------------------------------------------------------------------


class PredictiveConditionsState(TypedDict, total=False):
    # ---- Input fields (shown in LangGraph UI) ----
    loan_file_xml: str
    loan_profile_json: str
    submitted_documents_json: str
    env: str  # "Test" | "Prod"

    # ---- Message history ----
    messages: Annotated[list[BaseMessage], add_messages]

    # ---- Internal fields ----
    scenario_summary: Annotated[NotRequired[dict], _merge_dicts]
    missing_core_variables: Annotated[NotRequired[list], _append_list]
    contradictions_detected: Annotated[NotRequired[list], _append_list]
    docs_by_facet: Annotated[NotRequired[dict], _merge_dicts]
    overlays_by_facet: Annotated[NotRequired[dict], _merge_dicts]
    guideline_section_refs: Annotated[NotRequired[dict], _merge_dicts]
    module_outputs: Annotated[NotRequired[dict], _merge_dicts]
    flags: Annotated[NotRequired[list[dict]], _dedupe_flags]
    current_step: Annotated[NotRequired[str], _last_value]
    step_reports: Annotated[NotRequired[dict], _merge_dicts]
    final_output: Annotated[NotRequired[dict], _last_value]
    todos: Annotated[NotRequired[list[dict]], _append_list]
    dev_mode: NotRequired[dict]


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------

_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-5")
_SYSTEM_PROMPT = load_system_prompt()

_llm = ChatAnthropic(
    model=_MODEL,
    temperature=0,
    max_tokens=8192,
)


# ---------------------------------------------------------------------------
# Graph nodes
# ---------------------------------------------------------------------------


def orchestrator_node(state: PredictiveConditionsState) -> dict:
    """
    Main ReAct node.

    Before invoking the LLM:
    1. Resolve tools for the current step (DynamicToolMiddleware).
    2. Inject the current step's plan as a transient system message (DynamicPlanMiddleware).
    """
    # Dynamic tool binding
    step_tools = resolve_tools_for_step(state)
    llm_with_tools = _llm.bind_tools(step_tools)

    # Build message list with optional transient plan injection
    messages: list[BaseMessage] = list(state.get("messages", []))

    plan = resolve_plan_for_step(state)
    if plan:
        # Insert plan as a system message immediately before the latest user/tool messages
        injected = [SystemMessage(content=f"[CURRENT STEP PLAN]\n\n{plan}")] + messages
    else:
        # Use global system prompt on first call
        if not messages:
            injected = [SystemMessage(content=_SYSTEM_PROMPT)]
        else:
            injected = messages

    response: AIMessage = llm_with_tools.invoke(injected)
    return {"messages": [response]}


def tool_node_factory(tools: list) -> ToolNode:
    """Create a ToolNode with all tools (tool_resolver scoping happens at LLM layer)."""
    return ToolNode(tools)


def should_continue(state: PredictiveConditionsState) -> Literal["tools", "end"]:
    """Route: if the last message has tool calls, execute them; otherwise end."""
    messages = state.get("messages", [])
    if not messages:
        return "end"
    last = messages[-1]
    if isinstance(last, AIMessage) and last.tool_calls:
        return "tools"
    return "end"


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------

_tool_node = ToolNode(ALL_TOOLS)

_builder = StateGraph(PredictiveConditionsState)
_builder.add_node("orchestrator", orchestrator_node)
_builder.add_node("tools", _tool_node)

_builder.set_entry_point("orchestrator")
_builder.add_conditional_edges(
    "orchestrator",
    should_continue,
    {"tools": "tools", "end": END},
)
_builder.add_edge("tools", "orchestrator")

agent = _builder.compile()
