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
- SummarizationMiddleware: compresses completed-step messages into a compact
  summary before each LLM call, keeping only the current step's messages
  in full detail.
"""

from __future__ import annotations

import json
import os
from typing import Annotated, Any, Literal
from typing_extensions import NotRequired

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
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


def _last_value(old: Any, new: Any) -> Any:  # noqa: ARG001
    return new


# ---------------------------------------------------------------------------
# State schema
# ---------------------------------------------------------------------------


class PredictiveConditionsState(TypedDict, total=False):
    # ---- Input fields ----
    loan_file_xml: str                # MISMO XML — primary input
    manifest_json: str                # Raw manifest JSON (document inventory from extraction)
    eligibility_json: str             # Raw eligibility engine output JSON
    env: str                          # "Test" | "Prod"

    # ---- Message history ----
    messages: Annotated[list[BaseMessage], add_messages]

    # ---- Internal fields ----
    scenario_summary: Annotated[NotRequired[dict], _merge_dicts]
    missing_core_variables: Annotated[NotRequired[list], _append_list]
    contradictions_detected: Annotated[NotRequired[list], _append_list]
    document_inventory: Annotated[NotRequired[list], _append_list]
    doctype_mapping_hints: Annotated[NotRequired[list], _append_list]
    seen_conflicts: Annotated[NotRequired[list], _append_list]
    docs_by_facet: Annotated[NotRequired[dict], _merge_dicts]
    overlays_by_facet: Annotated[NotRequired[dict], _merge_dicts]
    guideline_section_refs: Annotated[NotRequired[dict], _merge_dicts]
    module_outputs: Annotated[NotRequired[dict], _merge_dicts]
    current_step: Annotated[NotRequired[str], _last_value]
    step_reports: Annotated[NotRequired[dict], _merge_dicts]
    final_output: Annotated[NotRequired[dict], _last_value]
    dev_mode: NotRequired[dict]


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------

_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-5")
_SYSTEM_PROMPT = load_system_prompt()

_llm_kwargs: dict = {
    "model": _MODEL,
    "max_tokens": 16384,
}
if "opus" in _MODEL:
    _llm_kwargs["thinking"] = {"type": "enabled", "budget_tokens": 8192}

_llm = ChatAnthropic(**_llm_kwargs)


# ---------------------------------------------------------------------------
# Default initial prompt (used when caller sends only data, no messages)
# ---------------------------------------------------------------------------

_DEFAULT_INITIAL_PROMPT = (
    "Execute the FULL Predictive Document Needs workflow from STEP_00 through STEP_08.\n\n"
    "You MUST complete ALL steps in sequence. Do NOT stop after a single step.\n"
    "Do NOT output a summary between steps — just call the tools.\n\n"
    "Step sequence:\n"
    "  STEP_00: parse_loan_file, parse_manifest_documents, parse_eligibility_output, "
    "load_doctype_masterlist, build_scenario_summary, detect_contradictions, route_to_facets\n"
    "  STEP_01: load_guideline_sections, check_overlay_conflicts, "
    "generate_crosscutting_document_requests\n"
    "  STEP_02: load_guideline_sections (income), then generate_income_document_requests\n"
    "  STEP_03: load_guideline_sections (assets), then generate_asset_document_requests\n"
    "  STEP_04: load_guideline_sections (credit), then generate_credit_document_requests\n"
    "  STEP_05: load_guideline_sections (property), then generate_property_document_requests\n"
    "  STEP_06: load_guideline_sections (title), then generate_title_document_requests\n"
    "  STEP_07: load_guideline_sections (compliance), then generate_compliance_document_requests\n"
    "  STEP_08: merge_document_requests, rank_document_requests, generate_final_output\n\n"
    "For STEP_02 through STEP_07: first load the relevant guideline sections, then "
    "reason over the scenario_summary + guidelines to generate document requests.\n"
    "Output document_requests (not conditions). Each document request must include "
    "specifications and reasons_needed."
)


# ---------------------------------------------------------------------------
# Message summarization
# ---------------------------------------------------------------------------

_STEP_SAVE_REPORT_PATTERN = "Step report saved for "


def _extract_step_from_tool_message(msg: ToolMessage) -> str | None:
    """If a ToolMessage indicates a step was saved, return the step ID."""
    content = msg.content if isinstance(msg.content, str) else ""
    if _STEP_SAVE_REPORT_PATTERN in content:
        # "Step report saved for STEP_02. Advancing to STEP_03..."
        after = content.split(_STEP_SAVE_REPORT_PATTERN, 1)[1]
        return after.split(".")[0].strip()
    return None


def _summarize_completed_steps(
    messages: list[BaseMessage],
    current_step: str | None,
    step_reports: dict,
) -> list[BaseMessage]:
    """
    Compress messages from completed steps into a single summary message.

    Keeps the first HumanMessage (initial instructions) and all messages
    from the current step in full detail. Everything in between gets
    replaced by a compact summary built from step_reports.
    """
    if not messages or not current_step or not step_reports:
        return messages

    # Find the boundary: the last ToolMessage that says
    # "Step report saved for STEP_XX. Advancing to {current_step}."
    boundary_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if isinstance(msg, ToolMessage):
            step_id = _extract_step_from_tool_message(msg)
            if step_id and step_id != current_step:
                boundary_idx = i
                break

    # If no boundary found or very few messages, no need to summarize
    if boundary_idx < 3:
        return messages

    # Build summary from step_reports
    summary_lines = ["[COMPLETED STEPS SUMMARY]", ""]
    for step_id, report in sorted(step_reports.items()):
        summary_text = report.get("summary", "No summary.")
        # Truncate very long summaries
        if len(summary_text) > 300:
            summary_text = summary_text[:300] + "..."
        summary_lines.append(f"## {step_id}: {summary_text}")

    # Also summarize condition counts from module_outputs if available
    summary_lines.append("")

    summary = "\n".join(summary_lines)

    # Keep: first HumanMessage + summary + messages from current step onward
    first_human = None
    for msg in messages:
        if isinstance(msg, HumanMessage):
            first_human = msg
            break

    current_step_messages = messages[boundary_idx + 1:]

    result: list[BaseMessage] = []
    if first_human:
        result.append(first_human)
    result.append(SystemMessage(content=summary))
    result.extend(current_step_messages)

    return result


# ---------------------------------------------------------------------------
# Graph nodes
# ---------------------------------------------------------------------------


def orchestrator_node(state: PredictiveConditionsState) -> dict:
    """
    Main ReAct node.

    Before invoking the LLM:
    1. Summarize completed-step messages (SummarizationMiddleware).
    2. Resolve tools for the current step (DynamicToolMiddleware).
    3. Inject the current step's plan as a transient system message
       (DynamicPlanMiddleware).
    """
    # Dynamic tool binding
    step_tools = resolve_tools_for_step(state)
    llm_with_tools = _llm.bind_tools(step_tools)

    # Build message list with summarization
    messages: list[BaseMessage] = list(state.get("messages", []))
    current_step = state.get("current_step") or "STEP_00"
    step_reports = state.get("step_reports", {})

    # Auto-inject initial instructions if caller sent no HumanMessage
    has_human = any(isinstance(m, HumanMessage) for m in messages)
    if not has_human:
        messages = [HumanMessage(content=_DEFAULT_INITIAL_PROMPT)] + messages

    # Compress completed steps into a summary
    messages = _summarize_completed_steps(messages, current_step, step_reports)

    # Build the system prefix: plan + summary are merged into a single
    # SystemMessage to satisfy Anthropic's constraint against multiple
    # non-consecutive system messages.
    plan = resolve_plan_for_step(state)
    system_parts: list[str] = []
    if plan:
        system_parts.append(f"[CURRENT STEP PLAN]\n\n{plan}")

    # Extract any SystemMessage we inserted for the summary and merge it
    # into the system prefix so there's only one SystemMessage at the front.
    non_system: list[BaseMessage] = []
    for msg in messages:
        if isinstance(msg, SystemMessage):
            system_parts.append(msg.content if isinstance(msg.content, str) else str(msg.content))
        else:
            non_system.append(msg)

    if system_parts:
        injected = [SystemMessage(content="\n\n---\n\n".join(system_parts))] + non_system
    elif not non_system:
        injected = [SystemMessage(content=_SYSTEM_PROMPT)]
    else:
        injected = non_system

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

agent = _builder.compile().with_config({"recursion_limit": 150})
