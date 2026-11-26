"""Tests for tools/general.py's step-skip guard in save_step_report().

Regression coverage for a confirmed, reproducible GraphRecursionError (150
steps burned with zero real progress) on a live run (thread
0659c53d-841b-41db-a4f3-b394899f88cd, 2026-10-14). step_reports ended up
with STEP_00-STEP_07 and STEP_09 but NOT STEP_08, and module_outputs only
had "01"/"02" -- proof STEP_08's own tools (merge_document_requests,
rank_document_requests, cross_check_satisfaction, generate_final_output)
never ran. The orchestrator had called save_step_report(step_id="STEP_09")
directly, skipping STEP_08 entirely.

_validate_step_08 exists specifically to catch a fraudulent STEP_08
completion, but it only runs when step_id == "STEP_08" is the one being
saved -- so skipping straight past it to "STEP_09" bypassed it completely.
_validate_step_09 then waved the bogus advance through because
`final_output == {"document_requests": []}` (present but empty) is
indistinguishable, from that validator's point of view, from "this loan
legitimately needs zero documents". With current_step then wrongly stuck at
"STEP_09" and no further legitimate progress possible, the orchestrator
never cleanly stopped and instead looped until LangGraph's hard recursion
cap fired.

The fix adds a step-skip guard, checked before any per-step validator: no
step_id may be saved more than one position past the furthest step already
present in step_reports, regardless of which specific steps are involved.
"""

from __future__ import annotations

from tools.general import _furthest_reported_idx, _STEP_SEQUENCE, save_step_report

_save_step_report = save_step_report.func


def _call(step_id: str, state: dict) -> str:
    """Invoke save_step_report.func and return the resulting ToolMessage content."""
    cmd = _save_step_report(
        step_id=step_id,
        summary="summary",
        outputs={},
        tool_call_id="call-1",
        state=state,
    )
    return cmd.update["messages"][0].content


class TestFurthestReportedIdx:
    def test_empty_reports_returns_minus_one(self):
        assert _furthest_reported_idx({}) == -1

    def test_single_entry(self):
        assert _furthest_reported_idx({"STEP_03": {}}) == _STEP_SEQUENCE.index("STEP_03")

    def test_takes_max_regardless_of_insertion_order(self):
        reports = {"STEP_07": {}, "STEP_02": {}, "STEP_05": {}}
        assert _furthest_reported_idx(reports) == _STEP_SEQUENCE.index("STEP_07")

    def test_ignores_unknown_step_ids(self):
        assert _furthest_reported_idx({"NOT_A_STEP": {}}) == -1


class TestStepSkipGuardRejectsSkips:
    def test_rejects_the_exact_regression_incident(self):
        # step_reports has STEP_00..STEP_07; orchestrator tries to save
        # STEP_09 directly, skipping STEP_08 -- exactly what happened live.
        state = {
            "step_reports": {f"STEP_{i:02d}": {} for i in range(8)},
            "module_outputs": {"01": {}, "02": {}},
            "final_output": {"document_requests": []},
            "current_step": "STEP_08",
        }
        content = _call("STEP_09", state)
        assert "REJECTED" in content
        assert "STEP_08" in content
        assert "skipped" in content.lower()

    def test_does_not_persist_a_step_report_on_skip(self):
        state = {"step_reports": {"STEP_00": {}}, "current_step": "STEP_00"}
        cmd = _save_step_report(
            step_id="STEP_05", summary="s", outputs={}, tool_call_id="x", state=state
        )
        assert "step_reports" not in cmd.update
        assert "current_step" not in cmd.update

    def test_rejects_skipping_multiple_steps_at_once(self):
        state = {"step_reports": {"STEP_00": {}}, "current_step": "STEP_00"}
        content = _call("STEP_09", state)
        assert "REJECTED" in content
        # Names every skipped step, not just the first one.
        for i in range(1, 9):
            assert f"STEP_{i:02d}" in content

    def test_rejects_from_completely_empty_step_reports(self):
        state = {"step_reports": {}, "current_step": "STEP_00"}
        content = _call("STEP_03", state)
        assert "REJECTED" in content
        assert "STEP_00" in content


class TestStepSkipGuardAllowsLegitimateProgress:
    def test_allows_normal_sequential_advance(self):
        state = {
            "step_reports": {f"STEP_{i:02d}": {} for i in range(8)},
            "current_step": "STEP_08",
            "module_outputs": {"08": {"merged_document_requests": [], "rank_done": True, "cross_check_done": True}},
            "final_output": {"document_requests": []},
        }
        content = _call("STEP_08", state)
        assert "REJECTED" not in content

    def test_allows_first_step(self):
        state = {"step_reports": {}, "current_step": "STEP_00"}
        content = _call("STEP_00", state)
        assert "REJECTED" not in content

    def test_allows_stale_reaffirmation_of_earlier_step(self):
        # idx (0, for STEP_00) <= furthest_before (7, from STEP_07) -- this
        # is the pre-existing "is_regression" no-op case, not a skip-ahead,
        # and must still be allowed through to that existing handling.
        state = {
            "step_reports": {f"STEP_{i:02d}": {} for i in range(8)},
            "current_step": "STEP_08",
        }
        content = _call("STEP_00", state)
        assert "REJECTED" not in content
        assert "No-op" in content

    def test_allows_resaving_the_current_furthest_step_again(self):
        state = {
            "step_reports": {f"STEP_{i:02d}": {} for i in range(7)},
            "current_step": "STEP_06",
        }
        content = _call("STEP_06", state)
        assert "REJECTED" not in content


class TestStepSkipGuardRunsBeforePerStepValidators:
    def test_skip_ahead_message_differs_from_completion_validator_message(self):
        # Even though STEP_08's own completion markers are also missing
        # here, the skip-ahead guard must fire first (and with its own
        # distinct message) since STEP_08 itself was never attempted.
        state = {
            "step_reports": {f"STEP_{i:02d}": {} for i in range(7)},  # only through STEP_06
            "current_step": "STEP_06",
            "final_output": {},
        }
        content = _call("STEP_09", state)
        assert "REJECTED" in content
        assert "you skipped" in content
        assert "STEP_07" in content and "STEP_08" in content
