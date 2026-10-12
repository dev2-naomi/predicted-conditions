"""Unit tests for the deterministic "bare-minimum fields missing" failure
gate (tools/scenario_tools.py:build_scenario_summary, agent.py:
route_after_tools / fail_fast_node).

Behavior under test:
  - When NO eligibility_json is provided AND the MISMO XML alone is missing
    one or more of the 16 canonical eligibility fields (FicoScore, LTV,
    CLTV, DTI, LoanAmount, PropertyValue, PropertyType, Occupancy,
    LoanPurpose, IncomeDocType, Channel, BorrowerType, Citizenship, State,
    County, LoanType), build_scenario_summary sets
    scenario_summary["critical_failure"] = {...}.
  - When eligibility_json IS provided (even if the XML alone would have been
    insufficient), the gate must NOT fire.
  - agent.py's route_after_tools() routes to "fail_fast" iff
    state["critical_failure"] is truthy, otherwise "orchestrator".
  - agent.py's fail_fast_node() emits a final_output with an empty
    document_requests list and a clear failure reason/message.

Run with: python3 -m pytest tests/test_critical_failure_gate.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_DIR))

from dotenv import load_dotenv
load_dotenv(REPO_DIR / ".env")

import pytest

from tools.scenario_tools import (
    _ELIGIBILITY_FIELD_MAP,
    build_scenario_summary,
    parse_eligibility_output,
    parse_loan_file,
)

ALL_SAMPLE_CASE_DIRS = sorted(
    p.parent for p in (REPO_DIR / "compiled_inputs").glob("*/loan_file.xml")
)

# bibby_10x is a LOCAL-ONLY, untracked test fixture (realistic loan data,
# not committed to git) used by the hardcoded single-case tests below. It
# will NOT exist on a fresh clone/worktree checkout (e.g. dev) -- skip those
# tests gracefully there rather than erroring. (ALL_SAMPLE_CASE_DIRS above
# already degrades gracefully to an empty list via glob when compiled_inputs
# itself doesn't exist, so the parametrized corpus-sweep classes below don't
# need this guard.)
_BIBBY_CASE_AVAILABLE = (REPO_DIR / "compiled_inputs/bibby_10x/eligibility.json").exists()
_requires_bibby_case = pytest.mark.skipif(
    not _BIBBY_CASE_AVAILABLE,
    reason="Local-only sample fixture compiled_inputs/bibby_10x not present in this checkout.",
)


def _run_step00(xml: str, eligibility_raw: str | None = None) -> dict:
    """Runs parse_loan_file (+ parse_eligibility_output if given) then
    build_scenario_summary, merging updates the same way the real LangGraph
    state reducers would, and returns the final build_scenario_summary
    Command's update dict."""
    state: dict = {"loan_file_xml": xml}
    if eligibility_raw is not None:
        state["eligibility_json"] = eligibility_raw

    cmd1 = parse_loan_file.func(state=state)
    state.update(cmd1.update)

    if eligibility_raw is not None:
        cmd_e = parse_eligibility_output.func(state=state)
        state["scenario_summary"] = {
            **state.get("scenario_summary", {}),
            **cmd_e.update.get("scenario_summary", {}),
        }

    cmd2 = build_scenario_summary.func(state=state)
    return cmd2.update


# ---------------------------------------------------------------------------
# Gate firing / not firing
# ---------------------------------------------------------------------------

@_requires_bibby_case
class TestCriticalFailureGate:
    def test_fires_when_xml_only_and_fields_missing(self):
        case = REPO_DIR / "compiled_inputs/bibby_10x"
        xml = (case / "loan_file.xml").read_text(encoding="utf-8")
        update = _run_step00(xml, eligibility_raw=None)
        assert update.get("critical_failure") is not None
        cf = update["critical_failure"]
        assert cf["reason"] == "missing_bare_minimum_fields"
        assert isinstance(cf["missing_fields"], list) and cf["missing_fields"]

    def test_does_not_fire_when_eligibility_json_provided(self):
        case = REPO_DIR / "compiled_inputs/bibby_10x"
        xml = (case / "loan_file.xml").read_text(encoding="utf-8")
        elig = (case / "eligibility.json").read_text(encoding="utf-8")
        update = _run_step00(xml, eligibility_raw=elig)
        assert update.get("critical_failure") is None

    def test_does_not_fire_with_empty_string_eligibility_json_vs_missing_key(self):
        """An empty-string eligibility_json ("") must be treated the same as
        eligibility_json being absent entirely -- both mean "no eligibility
        engine data was provided" and should still allow the gate to fire if
        XML fields are insufficient (parse_eligibility_output's own
        `if not raw` check uses the same falsy semantics)."""
        case = REPO_DIR / "compiled_inputs/bibby_10x"
        xml = (case / "loan_file.xml").read_text(encoding="utf-8")
        update = _run_step00(xml, eligibility_raw="")
        assert update.get("critical_failure") is not None

    def test_missing_fields_list_matches_known_xml_gaps(self):
        """Empirically, the loan XML alone never supplies these 4 fields
        (FicoScore, IncomeDocType, Channel, BorrowerType) across every local
        sample case -- confirmed via a full-corpus sweep. DTI is present in
        exactly 1/16 samples. bibby_10x has no DTI either, so all 5 should
        be reported missing for it."""
        case = REPO_DIR / "compiled_inputs/bibby_10x"
        xml = (case / "loan_file.xml").read_text(encoding="utf-8")
        update = _run_step00(xml, eligibility_raw=None)
        missing = set(update["critical_failure"]["missing_fields"])
        assert {"FicoScore", "IncomeDocType", "Channel", "BorrowerType", "DTI"} <= missing

    def test_critical_failure_message_lists_missing_fields_by_name(self):
        case = REPO_DIR / "compiled_inputs/bibby_10x"
        xml = (case / "loan_file.xml").read_text(encoding="utf-8")
        update = _run_step00(xml, eligibility_raw=None)
        msg = update["critical_failure"]["message"]
        assert "FicoScore" in msg
        assert "No eligibility_json was provided" in msg

    def test_tool_message_includes_critical_failure_banner(self):
        case = REPO_DIR / "compiled_inputs/bibby_10x"
        xml = (case / "loan_file.xml").read_text(encoding="utf-8")
        update = _run_step00(xml, eligibility_raw=None)
        tool_msg = update["messages"][0].content
        assert "CRITICAL FAILURE" in tool_msg
        assert "no document requests will be generated" in tool_msg.lower()

    def test_tool_message_has_no_failure_banner_when_gate_does_not_fire(self):
        case = REPO_DIR / "compiled_inputs/bibby_10x"
        xml = (case / "loan_file.xml").read_text(encoding="utf-8")
        elig = (case / "eligibility.json").read_text(encoding="utf-8")
        update = _run_step00(xml, eligibility_raw=elig)
        tool_msg = update["messages"][0].content
        assert "CRITICAL FAILURE" not in tool_msg


# ---------------------------------------------------------------------------
# Full-corpus regression sweep
# ---------------------------------------------------------------------------

class TestCriticalFailureGateAcrossAllSamples:
    @pytest.mark.parametrize("case_dir", ALL_SAMPLE_CASE_DIRS, ids=lambda p: p.name)
    def test_xml_only_always_fails_on_real_samples(self, case_dir: Path):
        """Every local sample loan file is missing at least one bare-minimum
        field when parsed from XML alone (no real-world sample XML carries
        FICO/IncomeDocType/Channel/BorrowerType). This is a known-state
        regression guard: if this ever starts passing for a given case, it
        means either the sample file changed to include those fields, or the
        XML parser started extracting more fields than before -- worth
        noticing either way, not silently ignoring."""
        xml = (case_dir / "loan_file.xml").read_text(encoding="utf-8")
        update = _run_step00(xml, eligibility_raw=None)
        assert update.get("critical_failure") is not None, (
            f"{case_dir.name}: expected XML-only critical_failure to fire"
        )

    @pytest.mark.parametrize("case_dir", ALL_SAMPLE_CASE_DIRS, ids=lambda p: p.name)
    def test_with_eligibility_never_fails_on_real_samples(self, case_dir: Path):
        elig_path = case_dir / "eligibility.json"
        if not elig_path.exists():
            pytest.skip(f"{case_dir.name} has no eligibility.json")
        xml = (case_dir / "loan_file.xml").read_text(encoding="utf-8")
        elig = elig_path.read_text(encoding="utf-8")
        update = _run_step00(xml, eligibility_raw=elig)
        assert update.get("critical_failure") is None, (
            f"{case_dir.name}: critical_failure unexpectedly fired with "
            f"eligibility_json present: {update.get('critical_failure')}"
        )


# ---------------------------------------------------------------------------
# Graph wiring (agent.py)
# ---------------------------------------------------------------------------

class TestGraphRouting:
    def test_route_after_tools_goes_to_fail_fast_when_set(self):
        from agent import route_after_tools
        state = {"critical_failure": {"reason": "missing_bare_minimum_fields"}}
        assert route_after_tools(state) == "fail_fast"

    def test_route_after_tools_goes_to_orchestrator_when_not_set(self):
        from agent import route_after_tools
        assert route_after_tools({}) == "orchestrator"
        assert route_after_tools({"critical_failure": None}) == "orchestrator"

    def test_fail_fast_node_emits_empty_document_requests(self):
        from agent import fail_fast_node
        state = {
            "critical_failure": {
                "reason": "missing_bare_minimum_fields",
                "message": "No eligibility_json was provided. Missing: FicoScore",
                "missing_fields": ["FicoScore"],
            },
            "scenario_summary": {"program": "unknown"},
        }
        result = fail_fast_node(state)
        assert result["final_output"]["document_requests"] == []
        assert result["final_output"]["stats"]["status"] == "failed"
        assert result["final_output"]["stats"]["failure_reason"] == "missing_bare_minimum_fields"
        assert "FicoScore" in result["final_output"]["stats"]["missing_fields"]
        assert result["current_step"] == "FAILED"

    def test_fail_fast_node_handles_missing_critical_failure_gracefully(self):
        """Defensive: fail_fast_node should not crash even if somehow invoked
        with no critical_failure in state (shouldn't happen given the
        routing guard, but shouldn't blow up the run if it ever does)."""
        from agent import fail_fast_node
        result = fail_fast_node({})
        assert result["final_output"]["document_requests"] == []
        assert result["final_output"]["stats"]["status"] == "failed"

    def test_graph_has_fail_fast_node_wired(self):
        """Confirms the compiled graph actually includes the new node/edges,
        not just that the standalone functions work in isolation."""
        import agent
        assert "fail_fast" in agent._builder.nodes


# ---------------------------------------------------------------------------
# _ELIGIBILITY_FIELD_MAP sanity (the 16-field contract this gate checks)
# ---------------------------------------------------------------------------

class TestEligibilityFieldMapContract:
    def test_field_map_has_sixteen_entries(self):
        assert len(_ELIGIBILITY_FIELD_MAP) == 16

    def test_field_map_keys_match_expected_eligibility_names(self):
        expected = {
            "FicoScore", "LTV", "CLTV", "DTI", "LoanAmount", "PropertyValue",
            "PropertyType", "Occupancy", "LoanPurpose", "IncomeDocType",
            "Channel", "BorrowerType", "Citizenship", "State", "County",
            "LoanType",
        }
        assert set(_ELIGIBILITY_FIELD_MAP.keys()) == expected
