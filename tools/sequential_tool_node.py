"""
sequential_tool_node.py — A ToolNode that executes multi-tool-call batches
in order, with correct state propagation between calls WITHIN the batch.

Problem this fixes (found live, 2026-10-12):

LangGraph's stock ``langgraph.prebuilt.ToolNode`` injects graph state into
EVERY tool call in a batch from a single pre-batch snapshot
(``ToolNode._extract_state`` just returns ``input`` unchanged, called once
per tool call but always against the same original ``input``), then runs
all of them in PARALLEL (a thread-pool ``executor.map`` for the sync path,
``asyncio.gather`` for the async path). Only after the ENTIRE batch finishes
does LangGraph merge each tool's ``Command.update`` into the real graph
state.

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
executes a multi-call batch ONE TOOL AT A TIME, rebuilding each call's
``ToolRuntime`` from a RUNNING state snapshot that already reflects every
prior call's ``Command.update`` in the same batch -- using the exact same
reducers the real graph uses for each state field (see ``agent.py``'s
``_merge_dicts``/``_append_list``/``_last_value``/``add_messages``). The
final combined output is equivalent to what the graph would produce if each
tool call had arrived in its own separate turn, regardless of how the LLM
happens to batch them.

For a single-tool-call batch (the common case, and the only shape the
original ToolNode semantics were ever actually correct for anyway), this
defers entirely to the stock ``ToolNode`` implementation -- zero behavior
change for that path.

VERSION SENSITIVITY -- READ BEFORE UPGRADING langgraph / langgraph-prebuilt:

This class subclasses ``ToolNode`` and overrides ``_func``/``_afunc``,
which are UNDERSCORE-PREFIXED INTERNAL METHODS with NO stability guarantee.
Their signature changed INCOMPATIBLY between the 0.6.x and 1.x
langgraph-prebuilt lines:

  - 0.6.x: ``_func(self, input, config, *, store: Optional[BaseStore])``
    -- store passed as a standalone keyword-only arg, auto-injected by
    ``RunnableCallable`` based on matching the ``store`` parameter's literal
    type annotation against a hardcoded sentinel list (``BaseStore``,
    ``"BaseStore"``, ``Optional[BaseStore]``, ``"Optional[BaseStore]"`` --
    note PEP 604 ``X | None`` syntax produces a DIFFERENT string than
    ``Optional[X]`` and will NOT match these sentinels if the module also
    uses ``from __future__ import annotations``; this exact mismatch broke
    production once already -- always use ``typing.Optional[X]`` literally,
    never ``X | None``, for any parameter LangGraph auto-injects by type).
  - 1.x: ``_func(self, input, config, runtime: Runtime)`` -- store is no
    longer a standalone kwarg; it's bundled inside a unified ``Runtime``
    object (``runtime.store``). The ``runtime`` parameter's type annotation
    is NOT checked (matched against ``ANY_TYPE`` in LangGraph's injection
    table) -- only the PARAMETER NAME ``runtime`` matters. Each tool call
    now gets its own ``ToolRuntime`` (state + tool_call_id + config + store
    + context + stream_writer + tools), constructed explicitly in
    ``_func``/``_afunc`` from ``input``/``config``/the outer ``runtime``.
    ``messages_key`` also became the private ``_messages_key`` in this line
    (no public alias) -- this also broke production once already.

requirements.txt / api/requirements.txt PIN langgraph and langgraph-prebuilt
to exact versions (1.2.11 / 1.1.0 as of 2026-10-12) for exactly this reason
-- production always runs that exact pinned pair. **If you bump either
package, re-read the new tool_node.py source for `_func`/`_afunc`/
`_run_one`/`_arun_one`/`_parse_input`/`_extract_state`/`_messages_key`/
`ToolRuntime` and update the "new API" branch below to match BEFORE
merging** -- then confirm `tests/test_sequential_tool_node.py` passes
against the new version (via Docker, see below -- NOT via host pytest,
which can't run the pinned stack at all on this project's Python 3.9 host).

DUAL-API SUPPORT: this project's local dev machine runs Python 3.9, which
langgraph/langgraph-prebuilt >=1.0 cannot even be installed on (they require
Python >=3.10) -- so `pytest tests/` on host necessarily runs against the
old 0.6.x line, while the pinned production Lambda (python:3.12 base image)
runs the 1.x line exclusively. This module detects which API shape is
importable and defines `_func`/`_afunc` against whichever one is actually
present, so `tools/__init__.py`'s import chain (and therefore the entire
host pytest suite) keeps working on Python 3.9, while production gets the
real, Docker-verified 1.x-targeted implementation. See
`_e2e_local_test/test_seq_node_minigraph.py` (gitignored scratch dir) for
the Docker-based harness that exercises the 1.x path against the exact
pinned versions via a real `graph.invoke()` -- this is the ONLY way this
project can locally verify the 1.x path, since host Python can't run it.
"""
from typing import Any, Dict, List, Optional, Union

from langchain_core.runnables import RunnableConfig
from langgraph.graph.message import add_messages
from langgraph.prebuilt.tool_node import ToolNode
from langgraph.types import Command

try:
    # 1.x line (pinned production: langgraph==1.2.11, langgraph-prebuilt==1.1.0).
    # store is bundled inside a unified Runtime/ToolRuntime object; messages_key
    # is private (_messages_key); _func/_afunc take `runtime` instead of `store`.
    from langgraph.prebuilt.tool_node import ToolRuntime as _ToolRuntime
    from langgraph.runtime import Runtime as _Runtime

    _NEW_TOOL_NODE_API = True
except ImportError:
    # 0.6.x line (this project's Python-3.9 host dev environment -- see
    # DUAL-API SUPPORT above). store is a standalone keyword-only _func/_afunc
    # arg; messages_key is public; state/store injection into tool call args
    # is done explicitly via the public inject_tool_args() before _run_one.
    from langgraph.store.base import BaseStore as _BaseStore

    _NEW_TOOL_NODE_API = False

# NOTE: kept in sync BY HAND with agent.py's _merge_dicts/_append_list/
# _last_value and PredictiveConditionsState's field->reducer mapping
# (duplicated here, not imported, to avoid a circular import -- agent.py
# imports ALL_TOOLS, which imports this module, transitively).


def _merge_dicts(old, new):
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


def _append_list(old, new):
    return (old or []) + (new or [])


def _last_value(old, new):  # noqa: ARG001
    return new


_STATE_REDUCERS = {
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


def _apply_state_update(running_state: dict, update) -> dict:
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


if _NEW_TOOL_NODE_API:

    class SequentialToolNode(ToolNode):
        """See module docstring. Drop-in replacement for
        ``langgraph.prebuilt.ToolNode`` -- same constructor, same output
        contract -- that executes multi-tool-call batches sequentially with
        correct inter-call state propagation, instead of in parallel against
        a single frozen pre-batch snapshot.

        This branch targets langgraph-prebuilt's 1.x ``ToolNode`` internals
        specifically (``_func(self, input, config, runtime)``, per-call
        ``ToolRuntime``) -- see the module docstring's DUAL-API SUPPORT /
        VERSION SENSITIVITY sections. This is the branch that actually runs
        in production (pinned langgraph==1.2.11 / langgraph-prebuilt==1.1.0).
        """

        def _func(
            self,
            input: Union[List[Any], Dict[str, Any], Any],
            config: RunnableConfig,
            runtime: "_Runtime",
        ) -> Any:
            if (
                not isinstance(input, dict)
                or input.get("__type") == "tool_call_with_context"
            ):
                # This pipeline always invokes the tools node with the full
                # state dict (StateGraph node convention) -- any other input
                # shape (Send-API payloads, list-of-messages, etc.) is
                # outside what this subclass bothers handling specially, so
                # defer entirely to the stock implementation.
                return super()._func(input, config, runtime)

            tool_calls, input_type = self._parse_input(input)
            if len(tool_calls) <= 1:
                # No inter-call ordering ambiguity with 0-1 calls -- the
                # stock implementation is already correct here.
                return super()._func(input, config, runtime)

            running_state: Dict[str, Any] = dict(input)
            outputs: List[Any] = []
            for call in tool_calls:
                tool_runtime = _ToolRuntime(
                    state=running_state,
                    tool_call_id=call["id"],
                    config=config,
                    context=runtime.context,
                    store=runtime.store,
                    stream_writer=runtime.stream_writer,
                    tools=list(self.tools_by_name.values()),
                    execution_info=runtime.execution_info,
                    server_info=runtime.server_info,
                )
                output = self._run_one(call, input_type, tool_runtime)
                outputs.append(output)
                running_state = _apply_output(running_state, output, self._messages_key)

            return self._combine_tool_outputs(outputs, input_type)

        async def _afunc(
            self,
            input: Union[List[Any], Dict[str, Any], Any],
            config: RunnableConfig,
            runtime: "_Runtime",
        ) -> Any:
            if (
                not isinstance(input, dict)
                or input.get("__type") == "tool_call_with_context"
            ):
                return await super()._afunc(input, config, runtime)

            tool_calls, input_type = self._parse_input(input)
            if len(tool_calls) <= 1:
                return await super()._afunc(input, config, runtime)

            running_state: Dict[str, Any] = dict(input)
            outputs: List[Any] = []
            for call in tool_calls:
                tool_runtime = _ToolRuntime(
                    state=running_state,
                    tool_call_id=call["id"],
                    config=config,
                    context=runtime.context,
                    store=runtime.store,
                    stream_writer=runtime.stream_writer,
                    tools=list(self.tools_by_name.values()),
                    execution_info=runtime.execution_info,
                    server_info=runtime.server_info,
                )
                output = await self._arun_one(call, input_type, tool_runtime)
                outputs.append(output)
                running_state = _apply_output(running_state, output, self._messages_key)

            return self._combine_tool_outputs(outputs, input_type)

else:

    class SequentialToolNode(ToolNode):
        """See module docstring. 0.6.x-API branch -- used only on this
        project's Python-3.9 host dev environment, which cannot install
        langgraph/langgraph-prebuilt >=1.0 at all (they require Python
        >=3.10). Exists so `tools/__init__.py`'s import chain (and therefore
        `pytest tests/` on host) keeps working; production never executes
        this branch -- see DUAL-API SUPPORT in the module docstring.
        """

        def _func(
            self,
            input: Union[List[Any], Dict[str, Any], Any],
            config: RunnableConfig,
            *,
            store: Optional[_BaseStore],
        ) -> Any:
            if not isinstance(input, dict):
                return super()._func(input, config, store=store)

            messages = input.get(self.messages_key, [])
            try:
                latest_ai_message = next(
                    m for m in reversed(messages) if hasattr(m, "tool_calls")
                )
            except StopIteration:
                return super()._func(input, config, store=store)
            raw_calls = [dict(c) for c in latest_ai_message.tool_calls]
            if len(raw_calls) <= 1:
                return super()._func(input, config, store=store)

            tool_calls, input_type = self._parse_input(input, store)
            running_state: Dict[str, Any] = dict(input)
            outputs: List[Any] = []
            for call in raw_calls:
                injected_call = self.inject_tool_args(dict(call), running_state, store)
                output = self._run_one(injected_call, input_type, config)
                outputs.append(output)
                running_state = _apply_output(running_state, output, self.messages_key)

            return self._combine_tool_outputs(outputs, input_type)

        async def _afunc(
            self,
            input: Union[List[Any], Dict[str, Any], Any],
            config: RunnableConfig,
            *,
            store: Optional[_BaseStore],
        ) -> Any:
            if not isinstance(input, dict):
                return await super()._afunc(input, config, store=store)

            messages = input.get(self.messages_key, [])
            try:
                latest_ai_message = next(
                    m for m in reversed(messages) if hasattr(m, "tool_calls")
                )
            except StopIteration:
                return await super()._afunc(input, config, store=store)
            raw_calls = [dict(c) for c in latest_ai_message.tool_calls]
            if len(raw_calls) <= 1:
                return await super()._afunc(input, config, store=store)

            _tool_calls, input_type = self._parse_input(input, store)
            running_state: Dict[str, Any] = dict(input)
            outputs: List[Any] = []
            for call in raw_calls:
                injected_call = self.inject_tool_args(dict(call), running_state, store)
                output = await self._arun_one(injected_call, input_type, config)
                outputs.append(output)
                running_state = _apply_output(running_state, output, self.messages_key)

            return self._combine_tool_outputs(outputs, input_type)
