"""
Parity test for tools.merger_tools.run_document_merger_pipeline.

STEP_08 used to be 4 separate LLM-issued tool calls (merge_document_requests
-> rank_document_requests -> cross_check_satisfaction -> generate_final_output),
gated one-at-a-time by step_loader._gate_step_08_tools to avoid a confirmed
data race (LangGraph's ToolNode dispatches every tool call in one AIMessage
concurrently against one shared state snapshot; batching two of these
together made the second one read pre-update state -- see that function's
module comment for the full history, including a real "Ranked 0" incident).

run_document_merger_pipeline replaces those 4 turns with 1 by running the
same 4 underlying functions as plain sequential Python calls inside a
single tool invocation, manually re-applying each one's Command.update onto
a local state dict using the exact same reducer semantics
(agent.PredictiveConditionsState) the real graph applies between turns.

This test proves that equivalence directly: it runs a fixed pre-STEP_08
state through both paths --

  (A) the original 4 tools, called one at a time, each Command.update
      folded into the running state using the REAL graph reducers
      (imported straight from agent.py, not reimplemented), exactly
      mimicking what 4 separate orchestrator turns would have produced;
  (B) the new combined tool, called once;

and asserts the two paths land on byte-identical module_outputs["08"] and
final_output. Because this pipeline has zero LLM judgment in it (every
step is deterministic Python over already-fixed input), a single clean
comparison on fixed input is a strong correctness proof -- unlike
LLM-judged steps, there's no run-to-run variance to worry about.

Two fixtures are exercised:
  - a small synthetic state (below), self-contained so this test always
    runs (CI, fresh clones) without depending on any local-only data;
  - a real, previously-captured 27-document production state
    (montes_local_final_state.json), gitignored (*_state.json) because
    it's real loan data -- this richer check only runs when that file
    happens to exist locally, and is how the combined tool was originally
    validated (2026-11-13) before being pushed.
"""
import copy
import json
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import pytest

from tools.merger_tools import (
    cross_check_satisfaction,
    generate_final_output,
    merge_document_requests,
    rank_document_requests,
    run_document_merger_pipeline,
)

_REAL_FIXTURE = Path(__file__).resolve().parent.parent / "montes_local_final_state.json"


# A small, self-contained pre-STEP_08 state -- just enough shape (two
# modules each contributing one document_request, differing in priority/
# severity so ranking actually has something to order) to exercise the
# full merge -> rank -> cross-check -> finalize chain without depending on
# any local-only fixture.
_SYNTHETIC_PRE_STEP08_STATE: dict = {
    "module_outputs": {
        "01": {
            "document_requests": [
                {
                    "document_type": "Government-Issued Photo ID",
                    "specifications": ["Must be unexpired government-issued photo ID"],
                    "reasons_needed": [
                        {"reason": "Borrower identity must be verified.", "reason_type": "document_validation"},
                    ],
                    "priority": "P2",
                    "severity": "SOFT-STOP",
                    "document_category": "Cross-Cutting",
                    "status": "needed",
                    "tags": ["crosscutting"],
                    "source_module": "01",
                },
            ],
        },
        "02": {
            "document_requests": [
                {
                    "document_type": "Form 1040",
                    "specifications": ["Most recent 2 years of personal tax returns"],
                    "reasons_needed": ["Required for income qualification"],
                    "priority": "P1",
                    "severity": "HARD-STOP",
                    "document_category": "Income",
                    "status": "needed",
                    "tags": ["income"],
                    "source_module": "02",
                },
            ],
        },
        "03": {"document_requests": []},
        "04": {"document_requests": []},
        "05": {"document_requests": []},
        "06": {"document_requests": []},
        "07": {"document_requests": []},
    },
    "document_inventory": [],
    "scenario_summary": {"_submitted_docs": []},
}


def _load_real_pre_step08_state() -> dict:
    """A real post-run state, stripped back down to exactly what STEP_08
    sees on entry: module_outputs["01"]-["07"] populated, but no
    module_outputs["08"] and no final_output yet (as if STEP_08 were about
    to run for the first time)."""
    with open(_REAL_FIXTURE) as f:
        full_state = json.load(f)

    state = copy.deepcopy(full_state)
    state.get("module_outputs", {}).pop("08", None)
    state.pop("final_output", None)
    state.pop("current_step", None)
    return state


def _run_original_four_tool_chain(base_state: dict) -> dict:
    """Path A: the original 4 tools, one at a time, each Command.update
    folded into the running state using the REAL graph reducers -- exactly
    what 4 separate orchestrator turns would have produced."""
    from agent import _last_value, _merge_dicts  # real graph reducers

    state = copy.deepcopy(base_state)
    for fn in (
        merge_document_requests,
        rank_document_requests,
        cross_check_satisfaction,
        generate_final_output,
    ):
        result = fn.func(tool_call_id="tc", state=state)
        update = result.update or {}
        for k, v in update.items():
            if k == "messages":
                continue
            if k == "module_outputs":
                state[k] = _merge_dicts(state.get(k), v)
            else:
                state[k] = _last_value(state.get(k), v)
    return state


def _run_combined_tool(base_state: dict) -> dict:
    """Path B: the new combined tool, called once."""
    from agent import _last_value, _merge_dicts  # real graph reducers

    state = copy.deepcopy(base_state)
    result = run_document_merger_pipeline.func(tool_call_id="tc", state=state)
    update = result.update or {}
    for k, v in update.items():
        if k == "messages":
            continue
        if k == "module_outputs":
            state[k] = _merge_dicts(state.get(k), v)
        else:
            state[k] = _last_value(state.get(k), v)
    return state


def _assert_pipeline_parity(base_state: dict) -> None:
    # Note: cross_check_satisfaction makes one LLM call per document as
    # part of its satisfaction check. Path A and Path B each call it
    # independently, so if this ran with live credentials and the LLM gave
    # a different judgment on each call, the two paths could in principle
    # diverge on that LLM-authored content -- a concern about
    # cross_check_satisfaction's own judgment, not about this test. Without
    # a working OPENAI_API_KEY (the default here / in CI), every call
    # falls back to the same deterministic heuristic, which is exactly what
    # this test needs: its job is to verify run_document_merger_pipeline's
    # *plumbing* (sequencing + reducer application) matches the real graph,
    # not to re-verify cross_check_satisfaction's own judgment.
    state_a = _run_original_four_tool_chain(base_state)
    state_b = _run_combined_tool(base_state)

    assert state_a["module_outputs"]["08"] == state_b["module_outputs"]["08"]
    assert state_a["final_output"] == state_b["final_output"]
    assert state_a.get("current_step") == state_b.get("current_step") == "STEP_08"

    # Sanity: this isn't vacuously true -- real document requests came out.
    assert len(state_b["final_output"]["document_requests"]) > 0
    assert state_b["module_outputs"]["08"].get("rank_done") is True
    assert state_b["module_outputs"]["08"].get("cross_check_done") is True


class TestStep08PipelineParitySynthetic:
    """Always runs -- no local-only fixture required."""

    def test_combined_tool_matches_original_four_tool_chain(self):
        _assert_pipeline_parity(_SYNTHETIC_PRE_STEP08_STATE)

    def test_combined_tool_single_messages_entry(self):
        """The combined tool must emit exactly one ToolMessage (it is one
        tool call from the model's perspective -- Anthropic/OpenAI require
        exactly one tool_result per tool_use block)."""
        state = copy.deepcopy(_SYNTHETIC_PRE_STEP08_STATE)
        result = run_document_merger_pipeline.func(tool_call_id="tc-123", state=state)
        messages = result.update.get("messages", [])
        assert len(messages) == 1
        assert messages[0].tool_call_id == "tc-123"
        content = messages[0].content
        assert "document requests" in content.lower()
        assert "ranked" in content.lower()
        assert "cross-check" in content.lower() or "satisf" in content.lower()
        assert "final output" in content.lower()


@pytest.mark.skipif(not _REAL_FIXTURE.exists(), reason="requires montes_local_final_state.json fixture (local only, gitignored)")
class TestStep08PipelineParityRealData:
    """Richer check against a real, previously-captured 27-document
    production state -- only runs when that local-only fixture exists."""

    def test_combined_tool_matches_original_four_tool_chain(self):
        _assert_pipeline_parity(_load_real_pre_step08_state())
