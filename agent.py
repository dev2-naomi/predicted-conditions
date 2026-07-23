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
import logging
import os
import random
import time
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

    # Optional co-borrower object. When present, a second document-needs set is
    # generated for the co-borrower and appended (tagged party="coborrower").
    # Shape: {"manifest_json": str, "loan_file_xml": str (optional)}.
    # Eligibility is shared with the borrower.
    coborrower: NotRequired[dict]

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
    "max_retries": 0,
}
if "opus" in _MODEL:
    _llm_kwargs["thinking"] = {"type": "enabled", "budget_tokens": 8192}

_llm = ChatAnthropic(**_llm_kwargs)

# Fallback model — used only when the primary model returns a transient
# overload/rate-limit error and retries against it don't immediately clear.
# Trades a possible quality drop for run completion during an Anthropic outage.
# Set ANTHROPIC_FALLBACK_MODEL="" to disable (primary-only, retry until exhausted).
_FALLBACK_MODEL = os.environ.get("ANTHROPIC_FALLBACK_MODEL", "claude-sonnet-4-5")
_fallback_llm: ChatAnthropic | None = None
if _FALLBACK_MODEL and _FALLBACK_MODEL != _MODEL:
    _fallback_kwargs: dict = {
        "model": _FALLBACK_MODEL,
        "max_tokens": 16384,
        "max_retries": 0,
    }
    if "opus" in _FALLBACK_MODEL:
        _fallback_kwargs["thinking"] = {"type": "enabled", "budget_tokens": 8192}
    _fallback_llm = ChatAnthropic(**_fallback_kwargs)

# Retry tuning for transient Anthropic server errors (500/503/529 overload,
# 429 rate limit). Waits use exponential backoff with full jitter, capped at
# LLM_RETRY_MAX_BACKOFF, so a sustained overload is ridden out over a much
# longer total window than a fixed cooldown while staggering concurrent runs.
_RETRY_COOLDOWN_SECONDS = float(os.environ.get("LLM_RETRY_COOLDOWN", "5"))
_RETRY_MAX_BACKOFF = float(os.environ.get("LLM_RETRY_MAX_BACKOFF", "60"))
_MAX_RETRIES = int(os.environ.get("LLM_MAX_RETRIES", "8"))
_RETRYABLE_STATUS = {429, 500, 502, 503, 529}
_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Default initial prompt (used when caller sends only data, no messages)
# ---------------------------------------------------------------------------

_DEFAULT_INITIAL_PROMPT = (
    "Execute the FULL Predictive Document Needs workflow from STEP_00 through STEP_09.\n\n"
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
    "  STEP_08: merge_document_requests, rank_document_requests, cross_check_satisfaction, generate_final_output\n"
    "  STEP_09: style_document_requests (restyle all document requests into AUS-like display format)\n\n"
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

    # Extend the boundary forward to include ALL ToolMessages that belong to
    # the same AIMessage batch as the boundary ToolMessage.  This prevents
    # splitting a batch where one tool_result is at the boundary and sibling
    # tool_results are just after it, which would leave the AIMessage parent
    # with unmatched tool_use blocks when included via the backward scan.
    boundary_tm = messages[boundary_idx]
    if isinstance(boundary_tm, ToolMessage) and hasattr(boundary_tm, "tool_call_id"):
        parent_ids: set[str] = set()
        for i in range(boundary_idx - 1, -1, -1):
            msg = messages[i]
            if isinstance(msg, AIMessage) and msg.tool_calls:
                ids = {tc.get("id", "") for tc in msg.tool_calls}
                if boundary_tm.tool_call_id in ids:
                    parent_ids = ids
                    break
            elif isinstance(msg, ToolMessage):
                continue
            else:
                break
        if parent_ids:
            for i in range(boundary_idx + 1, len(messages)):
                msg = messages[i]
                if isinstance(msg, ToolMessage) and hasattr(msg, "tool_call_id") and msg.tool_call_id in parent_ids:
                    boundary_idx = i
                elif not isinstance(msg, ToolMessage):
                    break

    # Build summary from step_reports
    summary_lines = ["[COMPLETED STEPS SUMMARY]", ""]
    for step_id, report in sorted(step_reports.items()):
        summary_text = report.get("summary", "No summary.")
        if len(summary_text) > 300:
            summary_text = summary_text[:300] + "..."
        summary_lines.append(f"## {step_id}: {summary_text}")

    summary_lines.append("")
    summary = "\n".join(summary_lines)

    first_human = None
    for msg in messages:
        if isinstance(msg, HumanMessage):
            first_human = msg
            break

    current_step_messages = messages[boundary_idx + 1:]

    # Ensure we don't start with orphaned ToolMessages whose corresponding
    # AIMessage (with tool_use) was cut. Walk backward from the boundary to
    # include any AIMessage that owns tool calls consumed by the kept messages.
    needed_tool_call_ids: set[str] = set()
    for msg in current_step_messages:
        if isinstance(msg, ToolMessage) and hasattr(msg, "tool_call_id"):
            needed_tool_call_ids.add(msg.tool_call_id)

    for msg in current_step_messages:
        if isinstance(msg, AIMessage) and msg.tool_calls:
            for tc in msg.tool_calls:
                needed_tool_call_ids.discard(tc.get("id", ""))

    prefix_messages: list[BaseMessage] = []
    if needed_tool_call_ids:
        for i in range(boundary_idx, -1, -1):
            msg = messages[i]
            if isinstance(msg, AIMessage) and msg.tool_calls:
                ids_in_msg = {tc.get("id", "") for tc in msg.tool_calls}
                if ids_in_msg & needed_tool_call_ids:
                    prefix_messages.insert(0, msg)
                    # Also collect ALL ToolMessages for this AIMessage's
                    # tool_calls that are on the cut side (before boundary+1),
                    # so every tool_use has a matching tool_result.
                    kept_ids = {
                        m.tool_call_id for m in current_step_messages
                        if isinstance(m, ToolMessage) and hasattr(m, "tool_call_id")
                    }
                    for j in range(i + 1, boundary_idx + 1):
                        m2 = messages[j]
                        if (
                            isinstance(m2, ToolMessage)
                            and hasattr(m2, "tool_call_id")
                            and m2.tool_call_id in ids_in_msg
                            and m2.tool_call_id not in kept_ids
                        ):
                            prefix_messages.append(m2)

                    needed_tool_call_ids -= ids_in_msg
                    if not needed_tool_call_ids:
                        break

    result: list[BaseMessage] = []
    if first_human:
        result.append(first_human)
    result.append(SystemMessage(content=summary))
    result.extend(prefix_messages)
    result.extend(current_step_messages)

    return result


# ---------------------------------------------------------------------------
# LLM invocation with retry + cooldown
# ---------------------------------------------------------------------------


def _retry_delay(attempt: int) -> float:
    """Exponential backoff with full jitter for retry *attempt* (1-based).

    Base doubles each attempt (cooldown * 2**(attempt-1)) and is capped at
    LLM_RETRY_MAX_BACKOFF; the actual sleep is a random value in [0, cap] so
    concurrent runs don't retry in lockstep against an already-overloaded API.
    """
    cap = min(_RETRY_COOLDOWN_SECONDS * (2 ** (attempt - 1)), _RETRY_MAX_BACKOFF)
    return random.uniform(0, cap)


def _invoke_with_retry(llm, messages: list, fallback_llm=None) -> AIMessage:
    """
    Invoke the LLM with retry logic. On transient server errors (429 rate
    limit, 500/502/503/529 overloaded), retry up to LLM_MAX_RETRIES attempts
    (default 8) using exponential backoff with full jitter, capped at
    LLM_RETRY_MAX_BACKOFF seconds (default 60).

    When *fallback_llm* is provided, each attempt tries the primary model
    first and, if it returns a transient error, immediately tries the fallback
    model before sleeping. This lets a run complete on the fallback during a
    sustained overload of the primary rather than failing outright. Non-retryable
    errors propagate immediately.
    """
    from anthropic import APIStatusError

    candidates = [("primary", llm)]
    if fallback_llm is not None:
        candidates.append(("fallback", fallback_llm))

    last_exc: Exception | None = None
    for attempt in range(1, _MAX_RETRIES + 1):
        for label, current in candidates:
            try:
                return current.invoke(messages)
            except APIStatusError as e:
                if e.status_code not in _RETRYABLE_STATUS:
                    raise
                last_exc = e
                _logger.warning(
                    "Anthropic API error %d on %s model (attempt %d/%d).",
                    e.status_code, label, attempt, _MAX_RETRIES,
                )
        if attempt < _MAX_RETRIES:
            delay = _retry_delay(attempt)
            _logger.warning("All models overloaded; retrying in %.1fs...", delay)
            time.sleep(delay)

    assert last_exc is not None
    raise last_exc


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
    fallback_with_tools = (
        _fallback_llm.bind_tools(step_tools) if _fallback_llm is not None else None
    )

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

    response: AIMessage = _invoke_with_retry(
        llm_with_tools, injected, fallback_llm=fallback_with_tools
    )
    return {"messages": [response]}


def tool_node_factory(tools: list) -> ToolNode:
    """Create a ToolNode with all tools (tool_resolver scoping happens at LLM layer)."""
    return ToolNode(tools)


def should_continue(state: PredictiveConditionsState) -> Literal["tools", "coborrower"]:
    """Route: if the last message has tool calls, execute them; otherwise run
    the co-borrower post-pass before ending."""
    messages = state.get("messages", [])
    if not messages:
        return "coborrower"
    last = messages[-1]
    if isinstance(last, AIMessage) and last.tool_calls:
        return "tools"
    return "coborrower"


def coborrower_node(state: PredictiveConditionsState) -> dict:
    """Post-pipeline node: tag borrower docs and, when a co-borrower manifest
    is supplied, append the co-borrower's document set to final_output."""
    from tools.coborrower import apply_coborrower_pass

    return apply_coborrower_pass(dict(state))


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------

_tool_node = ToolNode(ALL_TOOLS)

_builder = StateGraph(PredictiveConditionsState)
_builder.add_node("orchestrator", orchestrator_node)
_builder.add_node("tools", _tool_node)
_builder.add_node("coborrower", coborrower_node)

_builder.set_entry_point("orchestrator")
_builder.add_conditional_edges(
    "orchestrator",
    should_continue,
    {"tools": "tools", "coborrower": "coborrower"},
)
_builder.add_edge("tools", "orchestrator")
_builder.add_edge("coborrower", END)

agent = _builder.compile().with_config({"recursion_limit": 150})
