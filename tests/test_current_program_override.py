"""Unit tests for the `current_program` top-level input field in
tools/scenario_tools.py:build_scenario_summary.

Behavior under test (2026-11-23):
  - `current_program` is an optional, plain-string top-level input field
    (sibling of loan_file_xml / manifest_json / eligibility_json) naming
    the loan's CURRENT, already-assigned program (e.g. from the LOS).
  - When present, it is the single highest-priority source for
    scenario_summary["program"] -- it overrides BOTH the XML-inferred
    program AND the eligibility engine's eligible_programs list.
  - It is always surfaced under scenario_summary["current_program"]
    (None when the caller didn't supply it).
  - Absent/blank is a graceful no-op: falls through entirely to the
    existing eligibility/XML-derived program resolution.

Run with: python3 -m pytest tests/test_current_program_override.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_DIR))

from dotenv import load_dotenv
load_dotenv(REPO_DIR / ".env")

from tools.scenario_tools import build_scenario_summary


def _run(
    current_program: str | None = None,
    xml_program: str | None = None,
    eligible_programs: list[str] | None = None,
) -> dict:
    state: dict = {
        "scenario_summary": {
            "_loan_profile": {
                "metadata": (
                    {"loan_program": {"name": xml_program}} if xml_program else {}
                ),
            },
            "_eligibility_data": {
                "eligible_programs": eligible_programs or [],
                "application_data": {},
            },
        },
    }
    if current_program is not None:
        state["current_program"] = current_program
    cmd = build_scenario_summary.func(state=state)
    return cmd.update["scenario_summary"]


class TestCurrentProgramHighestPriority:
    def test_overrides_xml_inferred_program(self):
        ss = _run(current_program="DSCR Supreme", xml_program="Flex Select")
        assert ss["program"] == "DSCR Supreme"

    def test_overrides_eligibility_eligible_programs(self):
        ss = _run(
            current_program="ITIN",
            eligible_programs=["Flex Supreme", "No Ratio DSCR"],
        )
        assert ss["program"] == "ITIN"

    def test_overrides_both_xml_and_eligibility_simultaneously(self):
        ss = _run(
            current_program="Second Lien Select",
            xml_program="Flex Select",
            eligible_programs=["Flex Supreme"],
        )
        assert ss["program"] == "Second Lien Select"

    def test_surfaced_under_current_program_key(self):
        ss = _run(current_program="DSCR Supreme")
        assert ss["current_program"] == "DSCR Supreme"

    def test_strips_whitespace(self):
        ss = _run(current_program="  DSCR Supreme  ")
        assert ss["program"] == "DSCR Supreme"
        assert ss["current_program"] == "DSCR Supreme"


class TestCurrentProgramAbsentIsNoOp:
    def test_absent_falls_back_to_eligibility_derived_program(self):
        ss = _run(current_program=None, eligible_programs=["Flex Supreme"])
        assert ss["program"] == "Flex Supreme"
        assert ss["current_program"] is None

    def test_blank_string_is_a_no_op(self):
        ss = _run(current_program="   ", eligible_programs=["Flex Supreme"])
        assert ss["program"] == "Flex Supreme"
        assert ss["current_program"] is None

    def test_empty_string_is_a_no_op(self):
        ss = _run(current_program="", xml_program="Flex Select")
        assert ss["program"] == "Flex Select"
        assert ss["current_program"] is None
