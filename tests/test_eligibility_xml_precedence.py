"""Unit/integration tests for the loan-XML-vs-eligibility-engine precedence
policy in tools/scenario_tools.py:build_scenario_summary.

Behavior under test (2026-10-12 design decision):
  - When the loan XML and the eligibility engine output BOTH supply a value
    for the same field, the loan XML value wins (the eligibility engine can
    run on an earlier snapshot, before final loan terms are locked in the
    closing package's XML).
  - When only ONE source supplies a value for a field, that value is used
    (eligibility fills genuine gaps the XML left blank, and vice versa).
  - This applies across all 16 _ELIGIBILITY_FIELD_MAP fields, plus the
    derived summary fields built from them (occupancy, numbers.LTV,
    numbers.loan_amount, income_profile.income_doc_label) that are
    double-checked by the "Scenario Lock" consistency block later in
    build_scenario_summary.
  - "program" has no XML equivalent (the loan XML never names a retail loan
    program), so it is NOT part of this policy -- it continues to be set
    authoritatively from the eligibility engine's eligible_programs list.

Run with: python3 -m pytest tests/test_eligibility_xml_precedence.py -v
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

_BIBBY_CASE_AVAILABLE = (REPO_DIR / "compiled_inputs/bibby_10x/eligibility.json").exists()
_requires_bibby_case = pytest.mark.skipif(
    not _BIBBY_CASE_AVAILABLE,
    reason="bibby_10x is a local-only, untracked real-loan-data fixture (not committed to git).",
)


# ---------------------------------------------------------------------------
# Synthetic unit tests -- directly construct scenario_summary._loan_profile
# (XML side) and scenario_summary._eligibility_data (eligibility side) so
# both the "both present, conflicting" and "only one present" cases can be
# exercised precisely without depending on real sample data happening to
# disagree (or agree) on any particular field.
# ---------------------------------------------------------------------------

def _run_reapply(xml_meta: dict, elig_resolved_meta: dict | None = None, elig_app: dict | None = None) -> dict:
    """Runs build_scenario_summary with a synthetic XML-only loan_profile
    and a synthetic eligibility payload, returning the resulting metadata
    actually used (via the LTV field, which is always present in the
    returned Command's scenario_summary.numbers.LTV) -- but more directly,
    we inspect profile metadata precedence by checking specific resolved
    summary fields that round-trip 1:1 from meta (numbers.LTV <- ltv_pct,
    credit.fico <- fico, numbers.CLTV <- cltv_pct)."""
    state = {
        "scenario_summary": {
            "_loan_profile": {"metadata": xml_meta},
            "_eligibility_data": {
                "resolved_metadata": elig_resolved_meta or {},
                "application_data": elig_app or {},
            },
        },
    }
    cmd = build_scenario_summary.func(state=state)
    return cmd.update["scenario_summary"]


class TestXmlWinsOnConflict:
    def test_ltv_xml_wins_when_both_present_and_differ(self):
        ss = _run_reapply(
            xml_meta={"ltv_pct": 70.0},
            elig_resolved_meta={"ltv_pct": 55.0},
        )
        assert ss["numbers"]["LTV"] == 70.0

    def test_fico_xml_wins_when_both_present_and_differ(self):
        ss = _run_reapply(
            xml_meta={"fico": 800},
            elig_resolved_meta={"fico": 650},
        )
        assert ss["credit"]["fico"] == 800

    def test_cltv_xml_wins_when_both_present_and_differ(self):
        ss = _run_reapply(
            xml_meta={"cltv_pct": 70.0},
            elig_resolved_meta={"cltv_pct": 90.0},
        )
        assert ss["numbers"]["CLTV"] == 70.0


class TestEligibilityFillsGenuineGaps:
    def test_eligibility_fills_field_xml_left_blank(self):
        """FICO is a canonical example: XML essentially never supplies it,
        eligibility almost always does. Confirms the gap-filling direction
        still works after the precedence flip."""
        ss = _run_reapply(
            xml_meta={"ltv_pct": 70.0},  # fico absent from XML
            elig_resolved_meta={"fico": 650},
        )
        assert ss["credit"]["fico"] == 650
        assert ss["numbers"]["LTV"] == 70.0  # untouched XML value still intact

    def test_xml_fills_field_eligibility_left_blank(self):
        ss = _run_reapply(
            xml_meta={"ltv_pct": 70.0, "cltv_pct": None},
            elig_resolved_meta={},  # eligibility has nothing
        )
        assert ss["numbers"]["LTV"] == 70.0

    def test_empty_string_and_unknown_xml_values_are_treated_as_gaps(self):
        """An XML value of "" or "unknown" must be treated the same as
        None -- eligibility should still be allowed to fill it in."""
        ss = _run_reapply(
            xml_meta={"occupancy": "unknown", "purpose": ""},
            elig_resolved_meta={"occupancy": "investment", "purpose": "purchase"},
        )
        assert ss["occupancy"] == "investment"
        assert ss["purpose"] == "purchase"


class TestBackwardCompatApplicationDataOnly:
    def test_falls_back_to_application_data_when_resolved_metadata_absent(self):
        """If a caller only populates _eligibility_data.application_data
        (no resolved_metadata), build_scenario_summary should still be able
        to derive eligibility values from it directly."""
        ss = _run_reapply(
            xml_meta={},
            elig_resolved_meta=None,
            elig_app={"FicoScore": 700},
        )
        assert ss["credit"]["fico"] == 700


# ---------------------------------------------------------------------------
# Real-sample integration test (bibby_10x has both loan_file.xml and
# eligibility.json, and its real-world Occupancy/LTV/LoanAmount values
# genuinely disagree in representation between the two sources -- XML says
# occupancy "Investment", eligibility says "NOO" -- a perfect real-world
# case for this policy).
# ---------------------------------------------------------------------------

@_requires_bibby_case
class TestRealSampleXmlVsEligibilityPrecedence:
    def _run_full_chain(self) -> tuple[dict, str]:
        case = REPO_DIR / "compiled_inputs/bibby_10x"
        xml = (case / "loan_file.xml").read_text(encoding="utf-8")
        elig = (case / "eligibility.json").read_text(encoding="utf-8")

        state: dict = {"loan_file_xml": xml, "eligibility_json": elig}
        cmd1 = parse_loan_file.func(state=state)
        state.update(cmd1.update)

        cmd2 = parse_eligibility_output.func(state=state)
        state["scenario_summary"] = {
            **state.get("scenario_summary", {}),
            **cmd2.update.get("scenario_summary", {}),
        }

        cmd3 = build_scenario_summary.func(state=state)
        ss = cmd3.update["scenario_summary"]
        lock_msg = cmd3.update["messages"][0].content
        return ss, lock_msg

    def test_occupancy_keeps_xml_value_not_eligibility_value(self):
        ss, _ = self._run_full_chain()
        # bibby_10x's XML says "Investment"; its eligibility.json says "NOO".
        assert ss["occupancy"] == "Investment"
        assert ss["occupancy"] != "NOO"

    def test_lock_message_notes_discrepancy_without_forcing_override(self):
        _, lock_msg = self._run_full_chain()
        assert "NOTE: Occupancy is 'Investment'" in lock_msg
        assert "OVERRIDE: Occupancy" not in lock_msg

    def test_program_is_still_force_set_from_eligibility(self):
        """program has no XML equivalent, so it must still come from the
        eligibility engine's eligible_programs list, unaffected by this
        policy change."""
        ss, lock_msg = self._run_full_chain()
        assert ss["program"] in ss.get("eligible_programs", []) or "OVERRIDE: Program" in lock_msg

    def test_loan_profile_metadata_is_never_contaminated_by_eligibility_overlay(self):
        """Regression guard for the bug this fix addresses: previously,
        parse_eligibility_output wrote eligibility values directly into
        scenario_summary._loan_profile.metadata, silently clobbering the
        XML-derived values in state via the reducer -- before
        build_scenario_summary's own precedence logic ever got a chance to
        run. Confirms _loan_profile.metadata stays pure-XML throughout."""
        case = REPO_DIR / "compiled_inputs/bibby_10x"
        xml = (case / "loan_file.xml").read_text(encoding="utf-8")
        elig = (case / "eligibility.json").read_text(encoding="utf-8")

        state: dict = {"loan_file_xml": xml, "eligibility_json": elig}
        cmd1 = parse_loan_file.func(state=state)
        state.update(cmd1.update)
        xml_only_ltv = state["scenario_summary"]["_loan_profile"]["metadata"].get("ltv_pct")

        cmd2 = parse_eligibility_output.func(state=state)
        assert "_loan_profile" not in cmd2.update.get("scenario_summary", {})
        state["scenario_summary"] = {
            **state.get("scenario_summary", {}),
            **cmd2.update.get("scenario_summary", {}),
        }
        # Still the pure XML value -- eligibility's own overlay never wrote here.
        assert state["scenario_summary"]["_loan_profile"]["metadata"].get("ltv_pct") == xml_only_ltv
