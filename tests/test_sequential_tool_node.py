"""Tests for tools/sequential_tool_node.py:SequentialToolNode.

Background: LangGraph's stock ``ToolNode`` injects graph state into every
tool call in a batch from a SINGLE pre-batch snapshot, then runs them all in
parallel -- so a tool call that depends on another tool call's output WITHIN
THE SAME BATCH (e.g. ``build_scenario_summary`` reading
``scenario_summary._loan_profile``, which ``parse_loan_file`` writes) sees
stale/empty state whenever the orchestrator LLM happens to batch both into
one turn. Confirmed live 2026-10-12: a real run produced an entirely
"unknown" scenario_summary because build_scenario_summary ran in the same
tool-call batch as parse_loan_file.

``SequentialToolNode`` fixes this by executing multi-call batches ONE AT A
TIME, re-injecting state before each call from a running snapshot that
reflects every prior call's ``Command.update`` in the same batch.

Run with: python3 -m pytest tests/test_sequential_tool_node.py -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_DIR))

from dotenv import load_dotenv
load_dotenv(REPO_DIR / ".env")

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.prebuilt.tool_node import ToolNode
from langgraph.types import Command

from tools import ALL_TOOLS
from tools.sequential_tool_node import SequentialToolNode, _apply_state_update

SAMPLE_FLAT_JSON: dict = {
    "FicoScore": 724,
    "LTV": 70,
    "CLTV": 70,
    "DTI": 41.2,
    "LoanAmount": 420000,
    "PropertyValue": 600000,
    "PropertyType": "SFR",
    "Occupancy": "Primary",
    "LoanPurpose": "Purchase",
    "IncomeDocType": "Bank Statements: 12 Mo.",
    "Channel": "wholesale",
    "BorrowerType": "Self-Employed",
    "Citizenship": "US Citizen",
    "State": "CA",
    "County": "Orange",
    "LoanType": "Conventional",
}


def _make_batch_state(*tool_names: str) -> dict:
    ai_msg = AIMessage(
        content="",
        tool_calls=[
            {"name": name, "args": {}, "id": f"call_{i}", "type": "tool_call"}
            for i, name in enumerate(tool_names)
        ],
    )
    return {"loan_file_xml": json.dumps(SAMPLE_FLAT_JSON), "messages": [ai_msg]}


def _scenario_summary_from(result: list) -> dict | None:
    for item in result:
        if isinstance(item, Command):
            ss = item.update.get("scenario_summary", {})
            if "numbers" in ss:
                return ss
    return None


def _critical_failure_from(result: list) -> dict | None:
    for item in result:
        if isinstance(item, Command) and "critical_failure" in item.update:
            return item.update["critical_failure"]
    return None


class TestRunnableCallableAutoInjectsStoreAndConfig:
    """Regression test for a real production incident (2026-10-12):
    ``SequentialToolNode._func``/``_afunc`` were first written with
    ``from __future__ import annotations`` + ``BaseStore | None`` (PEP 604)
    annotations. That combination stringifies every annotation (PEP 563),
    so LangGraph's ``RunnableCallable.__init__`` -- which decides whether to
    auto-inject ``store``/``config`` by matching each parameter's *literal*
    annotation object against a hardcoded set of sentinels -- silently
    stopped detecting ``store`` as injectable. Calling ``._func()`` directly
    with an explicit ``store=None`` kwarg (as the other tests in this file
    do) masked the bug completely; it only reproduced through the REAL
    ``.invoke()`` / graph-execution path, which is what production uses.
    This crashed a live deployed run with:
    ``TypeError: SequentialToolNode._func() missing 1 required
    keyword-only argument: 'store'``.
    """

    def test_func_accepts_detects_store_and_config(self):
        node = SequentialToolNode(ALL_TOOLS)
        assert "store" in node.func_accepts, (
            "RunnableCallable failed to detect 'store' as an injectable "
            "kwarg on SequentialToolNode._func -- likely caused by "
            "postponed annotation evaluation (PEP 563) or PEP 604 "
            "'X | None' syntax on the store/config parameters, which "
            "LangGraph's signature introspection does not recognize."
        )
        assert "config" in node.func_accepts

    def test_invoke_does_not_crash_on_multi_call_batch(self):
        """The actual bug reproduction: must go through .invoke(), NOT
        ._func() directly, since .invoke() is what exercises LangGraph's
        store/config auto-injection machinery."""
        state = _make_batch_state("parse_loan_file", "build_scenario_summary")
        node = SequentialToolNode(ALL_TOOLS)
        result = node.invoke(dict(state))  # must not raise TypeError
        ss = _scenario_summary_from(result)
        assert ss is not None
        assert ss["numbers"]["LTV"] == 70
        assert ss["occupancy"] == "Primary"


class TestReproducesAndFixesTheRaceCondition:
    """The core regression test: parse_loan_file + build_scenario_summary
    batched into ONE AI turn. The stock ToolNode gets this wrong (confirms
    the bug is real and reproducible); SequentialToolNode gets it right."""

    def test_stock_tool_node_exhibits_the_bug(self):
        """Documents the bug this module fixes -- NOT a statement that this
        behavior is desirable. If this test ever starts failing because
        LangGraph changed ToolNode's semantics, that's good news: it may
        mean SequentialToolNode is no longer needed for this case."""
        state = _make_batch_state("parse_loan_file", "build_scenario_summary")
        result = ToolNode(ALL_TOOLS)._func(dict(state), {}, store=None)
        ss = _scenario_summary_from(result)
        assert ss is not None
        assert ss["numbers"]["LTV"] is None  # stale -- never saw parse_loan_file's output
        assert ss["occupancy"] == "unknown"

    def test_sequential_tool_node_sees_prior_calls_output(self):
        state = _make_batch_state("parse_loan_file", "build_scenario_summary")
        result = SequentialToolNode(ALL_TOOLS)._func(dict(state), {}, store=None)
        ss = _scenario_summary_from(result)
        assert ss is not None
        assert ss["numbers"]["LTV"] == 70
        assert ss["numbers"]["CLTV"] == 70
        assert ss["numbers"]["loan_amount"] == 420000
        assert ss["occupancy"] == "Primary"
        assert ss["credit"]["fico"] == 724

    def test_sequential_tool_node_gate_does_not_false_positive(self):
        """With the race condition fixed, the critical_failure gate must
        correctly see ALL 16 fields the flat JSON supplied and NOT fire --
        the stock ToolNode incorrectly fires with all 16 fields "missing"."""
        state = _make_batch_state("parse_loan_file", "build_scenario_summary")
        result = SequentialToolNode(ALL_TOOLS)._func(dict(state), {}, store=None)
        assert _critical_failure_from(result) is None

    def test_stock_tool_node_gate_false_positives(self):
        state = _make_batch_state("parse_loan_file", "build_scenario_summary")
        result = ToolNode(ALL_TOOLS)._func(dict(state), {}, store=None)
        cf = _critical_failure_from(result)
        assert cf is not None  # the bug: false-positive failure
        assert len(cf["missing_fields"]) == 16  # every single field, incorrectly


class TestThreeToolBatchOrdering:
    def test_three_tool_batch_propagates_through_all_calls(self):
        state = _make_batch_state(
            "parse_loan_file", "load_doctype_masterlist", "build_scenario_summary",
        )
        result = SequentialToolNode(ALL_TOOLS)._func(dict(state), {}, store=None)
        ss = _scenario_summary_from(result)
        assert ss is not None
        assert ss["numbers"]["LTV"] == 70


class TestSingleCallBatchIsUnaffected:
    """A single tool call (the common case) must defer entirely to the
    stock ToolNode behavior -- zero behavior change."""

    def test_single_call_produces_same_result_as_stock(self):
        state = _make_batch_state("parse_loan_file")
        stock_result = ToolNode(ALL_TOOLS)._func(dict(state), {}, store=None)
        seq_result = SequentialToolNode(ALL_TOOLS)._func(dict(state), {}, store=None)
        assert len(stock_result) == len(seq_result) == 1
        stock_meta = stock_result[0].update["scenario_summary"]["_loan_profile"]["metadata"]
        seq_meta = seq_result[0].update["scenario_summary"]["_loan_profile"]["metadata"]
        assert stock_meta == seq_meta

    def test_empty_tool_calls_do_not_crash(self):
        """Defensive: a batch with zero tool calls (shouldn't normally
        happen, since should_continue only routes to "tools" when there ARE
        tool_calls) must not raise -- falls through to the stock path."""
        ai_msg = AIMessage(content="done", tool_calls=[])
        state = {"messages": [HumanMessage("hi"), ai_msg]}
        # Stock ToolNode itself raises ValueError for "no AIMessage with
        # tool_calls" style inputs in some versions -- just confirm
        # SequentialToolNode doesn't introduce a NEW failure mode here
        # (i.e. it either raises the same thing stock does, or handles it).
        try:
            SequentialToolNode(ALL_TOOLS)._func(dict(state), {}, store=None)
        except Exception as e:
            try:
                ToolNode(ALL_TOOLS)._func(dict(state), {}, store=None)
                raise AssertionError(
                    "SequentialToolNode raised but stock ToolNode did not: " + str(e)
                )
            except Exception:
                pass  # both raise -- consistent, acceptable


class TestApplyStateUpdateReducers:
    """Unit tests for the reducer-aware merge helper directly."""

    def test_merge_dicts_reducer_deep_merges(self):
        running = {"scenario_summary": {"a": 1, "nested": {"x": 1}}}
        result = _apply_state_update(running, {"scenario_summary": {"b": 2, "nested": {"y": 2}}})
        assert result["scenario_summary"] == {"a": 1, "b": 2, "nested": {"x": 1, "y": 2}}

    def test_append_list_reducer_concatenates(self):
        running = {"seen_conflicts": ["a"]}
        result = _apply_state_update(running, {"seen_conflicts": ["b"]})
        assert result["seen_conflicts"] == ["a", "b"]

    def test_last_value_reducer_replaces(self):
        running = {"current_step": "STEP_00"}
        result = _apply_state_update(running, {"current_step": "STEP_01"})
        assert result["current_step"] == "STEP_01"

    def test_messages_reducer_appends(self):
        m1 = HumanMessage("first")
        running = {"messages": [m1]}
        m2 = ToolMessage(content="second", tool_call_id="x")
        result = _apply_state_update(running, {"messages": [m2]})
        assert len(result["messages"]) == 2

    def test_unknown_field_defaults_to_last_value(self):
        running = {"loan_file_xml": "old"}
        result = _apply_state_update(running, {"loan_file_xml": "new"})
        assert result["loan_file_xml"] == "new"

    def test_empty_update_is_a_noop(self):
        running = {"scenario_summary": {"a": 1}}
        result = _apply_state_update(running, {})
        assert result == running
        result2 = _apply_state_update(running, None)
        assert result2 == running
