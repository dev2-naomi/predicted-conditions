"""
sequential_tool_node.py — A ToolNode that executes multi-tool-call batches
in order, with correct state propagation between calls WITHIN the batch.

Problem this fixes (found live, 2026-10-12):

LangGraph's stock ``langgraph.prebuilt.ToolNode`` injects graph state into
EVERY tool call in a batch from a single pre-batch snapshot (see
``ToolNode._parse_input`` -> ``inject_tool_args``), then runs all of them in
PARALLEL (a thread-pool ``executor.map`` for the sync path, ``asyncio.gather``
for the async path). Only after the ENTIRE batch finishes does LangGraph
merge each tool's ``Command.update`` into the real graph state.

This pipeline's tools have implicit ordering dependencies within STEP_00:
e.g. ``build_scenario_summary`` reads ``scenario_summary._loan_profile``,
which ``parse_loan_file`` writes. As long as the orchestrator LLM calls
these across SEPARATE turns, the dependency is respected (each turn's
Command.update is merged into state before the next turn's ToolNode
invocation begins). But the LLM is free to batch multiple tool calls into
ONE turn (one AIMessage with several tool_calls) -- and when it happens to
batch ``parse_loan_file`` and ``build_scenario_summary`` together, the stock
ToolNode's parallel/pre-batch-snapshot semantics mean
``build_scenario_summary`` silently computes its summary from an EMPTY loan
profile (confirmed live: a run produced an entirely "unknown"
scenario_summary even though the loan data parsed correctly moments
earlier, in the very same tool-call batch).

Fix: ``SequentialToolNode`` is a drop-in replacement for ``ToolNode`` that
executes a multi-call batch ONE TOOL AT A TIME, re-injecting state before
each call from a RUNNING snapshot that already reflects every prior call's
``Command.update`` in the same batch -- using the exact same reducers the
real graph uses for each state field (see ``agent.py``'s
``_merge_dicts``/``_append_list``/``_last_value``/``add_messages``). The
final combined output is equivalent to what the graph would produce if each
tool call had arrived in its own separate turn, regardless of how the LLM
happens to batch them.

For a single-tool-call batch (the common case, and the only shape the
original ToolNode semantics were ever actually correct for anyway), this
defers entirely to the stock ``ToolNode`` implementation -- zero behavior
change for that path.
"""
from __future__ import annotations

from typing import Any, Callable

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph.message import add_messages
from langgraph.prebuilt.tool_node import ToolNode
from langgraph.store.base import BaseStore
from langgraph.types import Command

# NOTE: kept in sync BY HAND with agent.py's _merge_dicts/_append_list/
# _last_value and PredictiveConditionsState's field->reducer mapping
# (duplicated here, not imported, to avoid a circular import -- agent.py
# imports ALL_TOOLS, which imports this module, transitively).


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


_STATE_REDUCERS: dict[str, Callable[[Any, Any], Any]] = {
    "messages": add_messages,
    "scenario_summary": _merge_dicts,
    "docs_by_facet": _merge_dicts,
    "overlays_by_facet": _merge_dicts,
    "guideline_section_refs": _merge_dicts,
    "module_outputs": _merge_dicts,
    "step_reports": _merge_dicts,
    "missing_core_variables": _append_list,
    "contradictions_detected": _append_list,
    "document_inventory": _append_list,
    "doctype_mapping_hints": _append_list,
    "seen_conflicts": _append_list,
    "current_step": _last_value,
    "final_output": _last_value,
    "critical_failure": _last_value,
}


def _apply_state_update(running_state: dict, update: dict | None) -> dict:
    """Merges a single tool's Command.update into a running state snapshot,
    using the same per-field reducer the real graph uses -- so a later tool
    call in the same batch sees exactly what it would have seen had the
    earlier tool's update already been merged by the graph."""
    if not update:
        return running_state
    result = dict(running_state)
    for key, new_val in update.items():
        reducer = _STATE_REDUCERS.get(key, _last_value)
        result[key] = reducer(result.get(key), new_val)
    return result


def _apply_output(running_state: dict, output: Any, messages_key: str) -> dict:
    if isinstance(output, Command):
        update = output.update if isinstance(output.update, dict) else None
    else:
        # A plain ToolMessage (not wrapped in a Command) -- its only effect
        # on state is appending itself to messages.
        update = {messages_key: [output]}
    return _apply_state_update(running_state, update)


class SequentialToolNode(ToolNode):
    """See module docstring. Drop-in replacement for
    ``langgraph.prebuilt.ToolNode`` -- same constructor, same output
    contract -- that executes multi-tool-call batches sequentially with
    correct inter-call state propagation, instead of in parallel against a
    single frozen pre-batch snapshot."""

    def _func(
        self,
        input: dict[str, Any],
        config: RunnableConfig,
        *,
        store: BaseStore | None,
    ) -> Any:
        if not isinstance(input, dict):
            # This pipeline always invokes the tools node with the full
            # state dict (StateGraph node convention) -- any other input
            # shape is outside what this subclass bothers handling
            # specially, so defer entirely to the stock implementation.
            return super()._func(input, config, store=store)

        raw_calls, input_type = self._raw_tool_calls(input)
        if len(raw_calls) <= 1:
            # No inter-call ordering ambiguity with 0-1 calls -- the stock
            # implementation is already correct here.
            return super()._func(input, config, store=store)

        running_state = dict(input)
        outputs: list[Any] = []
        for call in raw_calls:
            injected_call = self.inject_tool_args(dict(call), running_state, store)
            output = self._run_one(injected_call, input_type, config)
            outputs.append(output)
            running_state = _apply_output(running_state, output, self.messages_key)

        return self._combine_tool_outputs(outputs, input_type)

    async def _afunc(
        self,
        input: dict[str, Any],
        config: RunnableConfig,
        *,
        store: BaseStore | None,
    ) -> Any:
        if not isinstance(input, dict):
            return await super()._afunc(input, config, store=store)

        raw_calls, input_type = self._raw_tool_calls(input)
        if len(raw_calls) <= 1:
            return await super()._afunc(input, config, store=store)

        running_state = dict(input)
        outputs: list[Any] = []
        for call in raw_calls:
            injected_call = self.inject_tool_args(dict(call), running_state, store)
            output = await self._arun_one(injected_call, input_type, config)
            outputs.append(output)
            running_state = _apply_output(running_state, output, self.messages_key)

        return self._combine_tool_outputs(outputs, input_type)

    def _raw_tool_calls(self, input: dict[str, Any]) -> tuple[list[dict], str]:
        """Extracts the tool_calls list from the latest AIMessage WITHOUT
        performing state/store injection (that happens per-call, against the
        running state snapshot, in _func/_afunc above) -- mirrors
        ToolNode._parse_input's dict-input branch minus the injection step."""
        messages = input.get(self.messages_key, [])
        try:
            latest_ai_message = next(
                m for m in reversed(messages) if isinstance(m, AIMessage)
            )
        except StopIteration:
            raise ValueError("No AIMessage found in input")
        return [dict(call) for call in latest_ai_message.tool_calls], "dict"
