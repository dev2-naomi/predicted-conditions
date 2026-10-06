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

from registry import STEP_ORDER
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
    required_documents_json: str      # Submission Requirements checklist (program_name,
                                       # program_results_key, documents[]) — the loan's
                                       # minimum_required_documents item, same one the
                                       # Submission Requirements tab renders. Optional —
                                       # absent/empty is a graceful no-op (see
                                       # tools.scenario_tools.parse_required_documents).
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

# Extended thinking (Opus) forces temperature=1 at the Anthropic API level —
# it can't be lowered while thinking is enabled. That's the main source of
# spec-wording drift across modules 01-07 on reruns of the SAME input (see
# consistency testing in the predicted-conditions repo history). Non-thinking
# models (Sonnet, etc.) have no such restriction, so give them an explicit,
# low, configurable temperature instead of falling through to the Anthropic
# API's own default of 1.0.
_LLM_TEMPERATURE = os.environ.get("LLM_TEMPERATURE", "0.2")

# Primary-provider override — set PRIMARY_PROVIDER=openai (or leave
# ANTHROPIC_API_KEY unset) to run entirely on OpenAI instead of Anthropic.
# Added 2026-10-05 after a revoked/invalid ANTHROPIC_API_KEY hard-failed
# every run with a 401 (not in _RETRYABLE_STATUS below, so the fallback
# chain never even got a chance to kick in) — gives us a clean way to run
# dev/test traffic on OpenAI only without touching the Anthropic client
# construction at all (which errors at import time on a missing key).
# OPENAI_PRIMARY_MODEL defaults to "gpt-5-mini" -- OpenAI's balanced/
# cost-effective tier, the closest analog to claude-sonnet-4-5 (mirrors the
# existing Opus-flagship vs Sonnet-balanced split, vs. "gpt-5" flagship
# which is the closer Opus analog and is already used as the Tier 2
# cross-provider fallback default below).
_PRIMARY_PROVIDER = os.environ.get("PRIMARY_PROVIDER", "").strip().lower()
_USE_OPENAI_PRIMARY = _PRIMARY_PROVIDER == "openai" or (
    not os.environ.get("ANTHROPIC_API_KEY") and _PRIMARY_PROVIDER != "anthropic"
)

if _USE_OPENAI_PRIMARY:
    from langchain_openai import ChatOpenAI

    _OPENAI_PRIMARY_MODEL = os.environ.get(
        "OPENAI_PRIMARY_MODEL", os.environ.get("OPENAI_FALLBACK_MODEL", "gpt-5-mini"),
    )
    _oai_primary_kwargs: dict = {"model": _OPENAI_PRIMARY_MODEL, "max_retries": 0}
    _primary_effort = os.environ.get("OPENAI_REASONING_EFFORT", "medium")
    if _primary_effort:
        _oai_primary_kwargs["reasoning_effort"] = _primary_effort
    _llm = ChatOpenAI(**_oai_primary_kwargs)
    logging.getLogger(__name__).warning(
        "PRIMARY MODEL OVERRIDE: running on OpenAI %s instead of Anthropic "
        "%s (PRIMARY_PROVIDER=%r, ANTHROPIC_API_KEY set=%s).",
        _OPENAI_PRIMARY_MODEL, _MODEL, _PRIMARY_PROVIDER,
        bool(os.environ.get("ANTHROPIC_API_KEY")),
    )
else:
    _llm_kwargs: dict = {
        "model": _MODEL,
        "max_tokens": 16384,
        "max_retries": 0,
    }
    if "opus" in _MODEL:
        _llm_kwargs["thinking"] = {"type": "enabled", "budget_tokens": 8192}
    else:
        _llm_kwargs["temperature"] = float(_LLM_TEMPERATURE)

    _llm = ChatAnthropic(**_llm_kwargs)

_PRIMARY_LLM_PROVIDER = "openai" if _USE_OPENAI_PRIMARY else "anthropic"

# Fallback chain — tried in order when the primary model returns a transient
# overload/rate-limit error. Each entry is (provider, llm). A cross-provider
# fallback (OpenAI) is the most effective during an Anthropic-wide overload,
# since a same-provider model (Sonnet) is often saturated at the same time.
# Trades a possible quality/behavior shift for run completion during an outage.
_fallback_specs: list[tuple[str, Any]] = []

# Tier 1 — Anthropic Sonnet (same provider; cheaper/faster than Opus).
# Set ANTHROPIC_FALLBACK_MODEL="" to disable this tier. Also skipped
# whenever the primary is already running on the OpenAI override above —
# no point building a second client against the same (missing/invalid)
# Anthropic key.
_FALLBACK_MODEL = os.environ.get("ANTHROPIC_FALLBACK_MODEL", "claude-sonnet-4-5")
if _FALLBACK_MODEL and _FALLBACK_MODEL != _MODEL and not _USE_OPENAI_PRIMARY:
    _fallback_kwargs: dict = {
        "model": _FALLBACK_MODEL,
        "max_tokens": 16384,
        "max_retries": 0,
    }
    if "opus" in _FALLBACK_MODEL:
        _fallback_kwargs["thinking"] = {"type": "enabled", "budget_tokens": 8192}
    else:
        _fallback_kwargs["temperature"] = float(_LLM_TEMPERATURE)
    _fallback_specs.append(("anthropic", ChatAnthropic(**_fallback_kwargs)))

# Tier 2 — OpenAI reasoning ("thinking") model for cross-provider resilience.
# Enabled only when OPENAI_API_KEY is set and langchain-openai imports.
# Configure model via OPENAI_FALLBACK_MODEL (default gpt-5) and reasoning depth
# via OPENAI_REASONING_EFFORT (default "medium"; set "" to disable reasoning).
_OPENAI_FALLBACK_MODEL = os.environ.get("OPENAI_FALLBACK_MODEL", "gpt-5")
if os.environ.get("OPENAI_API_KEY") and _OPENAI_FALLBACK_MODEL:
    try:
        from langchain_openai import ChatOpenAI

        _oai_kwargs: dict = {"model": _OPENAI_FALLBACK_MODEL, "max_retries": 0}
        _effort = os.environ.get("OPENAI_REASONING_EFFORT", "medium")
        if _effort:
            _oai_kwargs["reasoning_effort"] = _effort
        _fallback_specs.append(("openai", ChatOpenAI(**_oai_kwargs)))
    except Exception as _oai_err:  # noqa: BLE001 — fallback is best-effort
        logging.getLogger(__name__).warning(
            "OpenAI fallback disabled (could not initialise ChatOpenAI): %s",
            _oai_err,
        )

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
    "parse_required_documents, "
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
    """If a ToolMessage indicates a step genuinely advanced, return the step ID.

    Only matches the exact "Step report saved for STEP_XX. Advancing..."
    format produced by a real (non-regression, non-rejection) advance in
    save_step_report, and only when the extracted token is a canonical
    STEP_ID from STEP_ORDER.

    This used to naively take everything between the prefix and the first
    "." — which broke when save_step_report started returning a *different*
    message for a stale re-affirmation of an already-completed step:
    "Step report saved for STEP_00 (a re-affirmation of an already-completed
    step ...). current_step remains STEP_09 ...". That message also starts
    with the same "Step report saved for " prefix, so the naive parser
    extracted the garbled token "STEP_00 (a re-affirmation ...)" as a fake
    "step advanced away from" boundary. Since that garbled token is (like
    any real step) != current_step, _summarize_completed_steps treated it as
    a valid, ever-more-recent boundary on every restart, repeatedly
    re-anchoring the model's visible context right at the stale STEP_00
    re-affirmation and hiding real STEP_09 progress — causing the agent to
    loop the STEP_00 checklist forever instead of ever reaching STEP_09's
    real work. Observed live on Kashana reruns after deploying the
    current_step regression guard in tools/general.py. Restricting the
    match to a genuine canonical STEP_ID closes this off entirely, whatever
    text save_step_report's message happens to contain afterward.
    """
    content = msg.content if isinstance(msg.content, str) else ""
    if _STEP_SAVE_REPORT_PATTERN not in content:
        return None
    # "Step report saved for STEP_02. Advancing to STEP_03..."
    after = content.split(_STEP_SAVE_REPORT_PATTERN, 1)[1]
    token = after.split(".")[0].strip()
    return token if token in STEP_ORDER else None


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


_TRANSIENT_KEYWORDS = (
    "overload", "rate_limit", "ratelimit", "timeout", "connection",
    "serviceunavailable", "service_unavailable", "internalserver",
    "internal_server", "api_error", "apierror",
)


def _is_transient_exc(exc: Exception) -> bool:
    """True if *exc* is a transient provider error worth retrying/falling back.

    Works across providers (Anthropic + OpenAI SDKs both expose ``status_code``
    on API errors). We match on THREE signals because no single one is reliable:

    1. HTTP status code — for ordinary non-streaming errors (429/500/502/503/529).
    2. The structured error *body* — CRITICAL for Anthropic mid-stream errors.
       When the provider overloads while streaming, the HTTP response already
       returned 200 OK and an ``error`` SSE event arrives afterwards. The SDK
       then raises a bare ``APIStatusError`` whose ``status_code`` is 200 (from
       the original response), so signal (1) misses it. The real cause lives in
       ``exc.body`` as ``{"error": {"type": "overloaded_error", ...}}``.
    3. The exception class name / message text — for connection/timeout errors
       that carry no status code or body.
    """
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and status in _RETRYABLE_STATUS:
        return True

    # Extract the provider error "type" from the structured body, if present.
    err_type = ""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            err_type = str(err.get("type", "")).lower()
        elif body.get("type"):
            err_type = str(body.get("type", "")).lower()

    haystack = f"{err_type} {type(exc).__name__} {exc}".lower()
    return any(k in haystack for k in _TRANSIENT_KEYWORDS)


def _messages_for_openai(messages: list) -> list:
    """Sanitise Anthropic-shaped history for an OpenAI model.

    Opus emits assistant turns whose ``content`` is a list of blocks that can
    include Anthropic-specific ``thinking``/``redacted_thinking`` items OpenAI
    rejects. Flatten AIMessage content to plain text (dropping thinking blocks)
    while preserving ``tool_calls`` so the ReAct loop keeps working.
    """
    out: list = []
    for m in messages:
        if isinstance(m, AIMessage) and isinstance(m.content, list):
            texts: list[str] = []
            for block in m.content:
                if isinstance(block, dict):
                    if block.get("type") == "text" and block.get("text"):
                        texts.append(block["text"])
                elif isinstance(block, str):
                    texts.append(block)
            new_msg = AIMessage(
                content="\n".join(texts),
                tool_calls=list(getattr(m, "tool_calls", []) or []),
            )
            out.append(new_msg)
        else:
            out.append(m)
    return out


def _invoke_with_retry(llm, messages: list, fallbacks=None) -> AIMessage:
    """
    Invoke the LLM with retry logic. On transient server errors (429 rate
    limit, 500/502/503/529 overloaded, connection/timeout), retry up to
    LLM_MAX_RETRIES attempts (default 8) using exponential backoff with full
    jitter, capped at LLM_RETRY_MAX_BACKOFF seconds (default 60).

    *fallbacks* is an ordered list of (provider, llm) tuples. Each attempt tries
    the primary first and, on a transient error, immediately tries each fallback
    (e.g. Anthropic Sonnet, then an OpenAI reasoning model) before sleeping.
    This lets a run complete on another model/provider during a sustained
    primary overload rather than failing outright.

    A non-transient error from the PRIMARY propagates immediately (it's a real
    error on well-formed input). Fallback errors are always swallowed so a
    best-effort fallback (e.g. cross-provider format quirks) never kills a run.
    """
    candidates: list[tuple[str, str, Any, bool]] = [("primary", "anthropic", llm, True)]
    for i, (provider, fb_llm) in enumerate(fallbacks or []):
        candidates.append((f"fallback[{i + 1}:{provider}]", provider, fb_llm, False))

    last_exc: Exception | None = None
    for attempt in range(1, _MAX_RETRIES + 1):
        for label, provider, current, is_primary in candidates:
            try:
                msgs = _messages_for_openai(messages) if provider == "openai" else messages
                return current.invoke(msgs)
            except Exception as e:  # noqa: BLE001 — classified below
                last_exc = e
                transient = _is_transient_exc(e)
                if is_primary and not transient:
                    raise
                _logger.warning(
                    "LLM invoke failed on %s (transient=%s, attempt %d/%d): %s",
                    label, transient, attempt, _MAX_RETRIES, e,
                )
        if attempt < _MAX_RETRIES:
            delay = _retry_delay(attempt)
            _logger.warning("All models failed this round; retrying in %.1fs...", delay)
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
    # Dynamic tool binding.
    #
    # OpenAI's reasoning models (gpt-5 family, incl. gpt-5-mini/5/5.1/5.2 —
    # confirmed across all of them in dev testing) have a much stronger
    # tendency than Claude to self-terminate early: their internal reasoning
    # "convinces" them the task is done and they emit a plain-text reply
    # with no tool_calls instead of continuing, even when the system/human
    # prompt explicitly says "you MUST complete ALL steps, do NOT stop
    # early". Prompt wording alone does not fix this for OpenAI models.
    #
    # The real fix is structural: force tool_choice="required" on OpenAI
    # models (langchain_openai supports this; langchain_anthropic does not
    # take the same value, and Claude doesn't need it) for as long as
    # final_output.document_requests hasn't been populated yet -- i.e. the
    # workflow is NOT genuinely finished. Once generate_final_output (the
    # STEP_09 tool) has actually run and populated final_output, we stop
    # forcing so the model can emit its natural closing text reply and let
    # should_continue() route to party_split as designed.
    final_output = state.get("final_output") or {}
    _workflow_incomplete = not final_output.get("document_requests")

    def _bind(llm, provider: str):
        if _workflow_incomplete and provider == "openai":
            return llm.bind_tools(step_tools, tool_choice="required")
        return llm.bind_tools(step_tools)

    step_tools = resolve_tools_for_step(state)
    llm_with_tools = _bind(_llm, _PRIMARY_LLM_PROVIDER)
    fallbacks_with_tools = [
        (provider, _bind(fb_llm, provider))
        for provider, fb_llm in _fallback_specs
    ]

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
        llm_with_tools, injected, fallbacks=fallbacks_with_tools
    )
    return {"messages": [response]}


def tool_node_factory(tools: list) -> ToolNode:
    """Create a ToolNode with all tools (tool_resolver scoping happens at LLM layer)."""
    return ToolNode(tools)


def should_continue(state: PredictiveConditionsState) -> Literal["tools", "party_split"]:
    """Route: if the last message has tool calls, execute them; otherwise run
    the per-party attribution post-pass before ending."""
    messages = state.get("messages", [])
    if not messages:
        return "party_split"
    last = messages[-1]
    if isinstance(last, AIMessage) and last.tool_calls:
        return "tools"
    # The model responded without calling a tool. Normally this means the
    # workflow genuinely finished (STEP_09 ran, final_output.document_requests
    # is populated). If final_output is still empty, the model stopped
    # PREMATURELY mid-workflow -- observed live on a real rerun: execution
    # got through module_outputs 01-04 (current_step stuck at "STEP_01") and
    # then the orchestrator emitted a plain text reply instead of the next
    # tool call, so this routed straight to party_split -> END with an empty
    # final_output, yet execute_background_run still recorded the run as
    # "success" (graph.invoke() returned without raising). Rather than
    # silently completing with a garbage/empty result, raise here so the run
    # is correctly recorded as an error (retriable/investigable) instead of a
    # false "success" that callers have no way to distinguish from a real,
    # complete, empty-document-set result.
    final_output = state.get("final_output") or {}
    if not final_output.get("document_requests"):
        current_step = state.get("current_step", "unknown")
        raise RuntimeError(
            "Orchestrator stopped calling tools before completing the workflow "
            f"(current_step={current_step!r}, final_output.document_requests "
            "empty). Treating this as a failed run rather than a false "
            "'success' with an incomplete result."
        )
    return "party_split"


def party_split_node(state: PredictiveConditionsState) -> dict:
    """Post-pipeline node: auto-detect borrower/co-borrower from the eligibility
    JSON (falling back to the loan XML), fuzzy-assign each manifest document to a
    party, and build a full condition set per party in final_output."""
    from tools.coborrower import apply_coborrower_pass

    return apply_coborrower_pass(dict(state))


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------

_tool_node = ToolNode(ALL_TOOLS)

_builder = StateGraph(PredictiveConditionsState)
_builder.add_node("orchestrator", orchestrator_node)
_builder.add_node("tools", _tool_node)
_builder.add_node("party_split", party_split_node)

_builder.set_entry_point("orchestrator")
_builder.add_conditional_edges(
    "orchestrator",
    should_continue,
    {"tools": "tools", "party_split": "party_split"},
)
_builder.add_edge("tools", "orchestrator")
_builder.add_edge("party_split", END)


def build_agent(checkpointer: Any | None = None):
    """Compile the graph, optionally with a checkpointer.

    ``langgraph dev`` / LangGraph Platform manage persistence themselves and
    never call this directly (they use the module-level ``agent`` below,
    compiled without an explicit checkpointer). The AWS Lambda shim
    (api/registry.py) calls this per-request with a DynamoDB-backed
    checkpointer so /threads/{id}/state and multi-turn resumption work the
    same way there as they do under LangGraph Platform.
    """
    return _builder.compile(checkpointer=checkpointer).with_config({"recursion_limit": 150})


agent = build_agent()
