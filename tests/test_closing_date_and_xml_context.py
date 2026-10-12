"""Unit tests for the closing_date extraction + XML-supplemental-context
changes (tools/shared/xml_parser.py, tools/scenario_tools.py,
tools/merger_tools.py, registry.py, tools/__init__.py).

Run with: python3 -m pytest tests/test_closing_date_and_xml_context.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_DIR))

import pytest

from tools.shared.xml_parser import parse_mismo_xml, xml_to_loan_profile
from tools.scenario_tools import build_scenario_summary, inspect_raw_xml_data, parse_loan_file
from tools.merger_tools import _build_reference_context
from registry import GENERAL_TOOL_NAMES
from tools import ALL_TOOLS, GENERAL_TOOLS
from step_loader import resolve_tools_for_step


# These sample case directories (bibby_10x, otodo_augustine_10x, etc.) are
# LOCAL-ONLY, untracked test fixtures -- they are NOT committed to git (they
# contain realistic loan data) and so will NOT exist on a fresh clone/
# worktree checkout (e.g. dev). Guard every read so the module still
# imports cleanly (and the affected tests just skip) in any environment
# that doesn't have these fixtures locally, instead of crashing the entire
# module at collection time.
_OTODO_XML_PATH = REPO_DIR / "compiled_inputs/otodo_augustine_10x/loan_file.xml"
_BIBBY_XML_PATH = REPO_DIR / "compiled_inputs/bibby_10x/loan_file.xml"
_LOCAL_SAMPLES_AVAILABLE = _OTODO_XML_PATH.exists() and _BIBBY_XML_PATH.exists()

HAS_CLOSING_DATE_XML = _OTODO_XML_PATH.read_text() if _LOCAL_SAMPLES_AVAILABLE else ""
NO_CLOSING_DATE_XML = _BIBBY_XML_PATH.read_text() if _LOCAL_SAMPLES_AVAILABLE else ""

ALL_SAMPLE_XML_PATHS = sorted((REPO_DIR / "compiled_inputs").glob("*/loan_file.xml"))

pytestmark = pytest.mark.skipif(
    not _LOCAL_SAMPLES_AVAILABLE,
    reason=(
        "Local-only sample loan files (compiled_inputs/bibby_10x, "
        "compiled_inputs/otodo_augustine_10x) not present in this checkout."
    ),
)


# ---------------------------------------------------------------------------
# closing_date extraction (xml_parser.py)
# ---------------------------------------------------------------------------

class TestClosingDateExtraction:
    def test_extracts_closing_date_when_present(self):
        parsed = parse_mismo_xml(HAS_CLOSING_DATE_XML)
        assert parsed.get("closing_date") == "2026-06-30"

    def test_closing_date_none_when_absent(self):
        parsed = parse_mismo_xml(NO_CLOSING_DATE_XML)
        assert parsed.get("closing_date") is None

    def test_closing_date_not_literal_unknown_string(self):
        """Regression guard: must default to None, not the generic
        _resolve_str 'unknown' placeholder used by most other string
        fields -- a date field defaulting to the string 'unknown' would
        silently corrupt any later date parsing attempt."""
        parsed = parse_mismo_xml(NO_CLOSING_DATE_XML)
        assert parsed.get("closing_date") != "unknown"

    def test_surfaced_in_xml_supplemental(self):
        profile = xml_to_loan_profile(HAS_CLOSING_DATE_XML)
        assert profile["_xml_supplemental"]["closing_date"] == "2026-06-30"

    def test_does_not_break_profile_without_closing_date(self):
        profile = xml_to_loan_profile(NO_CLOSING_DATE_XML)
        assert "parse_error" not in profile
        assert profile["_xml_supplemental"]["closing_date"] is None


# ---------------------------------------------------------------------------
# scenario_summary + loan_facts wiring (scenario_tools.py, merger_tools.py)
# ---------------------------------------------------------------------------

class TestClosingDateWiring:
    def _build_summary(self, xml_content: str) -> dict:
        profile = xml_to_loan_profile(xml_content)
        cmd = build_scenario_summary.func(state={"scenario_summary": {"_loan_profile": profile}})
        return cmd.update["scenario_summary"]

    def test_scenario_summary_has_closing_date(self):
        ss = self._build_summary(HAS_CLOSING_DATE_XML)
        assert ss.get("closing_date") == "2026-06-30"

    def test_scenario_summary_closing_date_none_when_absent(self):
        ss = self._build_summary(NO_CLOSING_DATE_XML)
        assert ss.get("closing_date") is None

    def test_loan_facts_exposes_closing_date_estimated(self):
        ss = self._build_summary(HAS_CLOSING_DATE_XML)
        ctx = _build_reference_context(ss, [])
        assert ctx["loan_facts"]["closing_date_estimated"] == "2026-06-30"

    def test_loan_facts_omits_key_entirely_when_absent(self):
        """Regression guard: must be ABSENT from the dict, not present with
        a None/empty value -- an empty key would still show up in the LLM's
        json.dumps'd prompt as noise ('closing_date_estimated: null')."""
        ss = self._build_summary(NO_CLOSING_DATE_XML)
        ctx = _build_reference_context(ss, [])
        assert "closing_date_estimated" not in ctx.get("loan_facts", {})

    def test_loan_facts_key_name_is_not_bare_closing_date(self):
        """The estimate-caveat naming (_estimated suffix) is deliberate --
        guards against a future edit accidentally renaming it to the bare
        'closing_date' key, which would lose the "this is a projection, not
        an actual date" signal the LLM prompt rule depends on."""
        ss = self._build_summary(HAS_CLOSING_DATE_XML)
        ctx = _build_reference_context(ss, [])
        assert "closing_date" not in ctx.get("loan_facts", {})
        assert "closing_date_estimated" in ctx.get("loan_facts", {})


# ---------------------------------------------------------------------------
# inspect_raw_xml_data tool
# ---------------------------------------------------------------------------

class TestInspectRawXmlData:
    def _state_for(self, xml_content: str) -> dict:
        cmd0 = parse_loan_file.func(state={"loan_file_xml": xml_content})
        ss0 = cmd0.update["scenario_summary"]
        cmd1 = build_scenario_summary.func(state={"scenario_summary": ss0})
        merged = dict(ss0)
        merged.update(cmd1.update["scenario_summary"])
        return {"scenario_summary": merged}

    def test_lists_sections_when_called_with_no_args(self):
        state = self._state_for(HAS_CLOSING_DATE_XML)
        out = inspect_raw_xml_data.func(state=state)
        assert "MISMO section(s) available" in out
        assert "DECLARATION_DETAIL" in out

    def test_retrieves_requested_section_content(self):
        state = self._state_for(HAS_CLOSING_DATE_XML)
        out = inspect_raw_xml_data.func(section_names=["DECLARATION_DETAIL"], state=state)
        assert "BankruptcyIndicator" in out

    def test_reports_missing_section_without_crashing(self):
        state = self._state_for(HAS_CLOSING_DATE_XML)
        out = inspect_raw_xml_data.func(
            section_names=["DECLARATION_DETAIL", "NOT_A_REAL_SECTION"], state=state,
        )
        assert "Not present in this loan file" in out
        assert "NOT_A_REAL_SECTION" in out

    def test_handles_empty_state_without_crashing(self):
        out = inspect_raw_xml_data.func(state={})
        assert "No raw XML data available" in out

    def test_handles_no_xml_provided_without_crashing(self):
        state = self._state_for("")
        out = inspect_raw_xml_data.func(state=state)
        assert "No raw XML data available" in out


# ---------------------------------------------------------------------------
# build_scenario_summary's expanded ToolMessage (supplemental context block)
# ---------------------------------------------------------------------------

class TestScenarioSummarySupplementalContext:
    def _message_for(self, xml_content: str) -> str:
        profile = xml_to_loan_profile(xml_content)
        cmd = build_scenario_summary.func(state={"scenario_summary": {"_loan_profile": profile}})
        return cmd.update["messages"][0].content

    def test_includes_declarations_when_present(self):
        msg = self._message_for(HAS_CLOSING_DATE_XML)
        assert "declarations" in msg
        assert "BankruptcyIndicator" in msg or "bankruptcy" in msg.lower()

    def test_includes_closing_date_in_message(self):
        msg = self._message_for(HAS_CLOSING_DATE_XML)
        assert "2026-06-30" in msg

    def test_excludes_ssn_and_dob(self):
        """PII minimization guard: borrower_ssns/borrower_dobs must never
        appear in this block, even though they live in the same
        _xml_supplemental dict as the fields we DO surface."""
        msg = self._message_for(HAS_CLOSING_DATE_XML)
        assert "borrower_ssns" not in msg
        assert "borrower_dobs" not in msg

    def test_excludes_raw_sections_dump(self):
        """raw_sections is intentionally NOT included here (handled by the
        on-demand inspect_raw_xml_data tool instead) -- including it would
        make every single run's context balloon with the full generic XML
        dump regardless of whether it's ever needed."""
        msg = self._message_for(HAS_CLOSING_DATE_XML)
        assert "raw_sections" not in msg

    def test_omits_block_entirely_when_no_supplemental_data(self):
        """Regression guard against a dangling empty '\\n\\nAdditional loan
        file details...:\\n{}' block appearing when there's nothing to show.

        Note: calling with totally empty XML and no eligibility_json also
        trips the critical_failure bare-minimum-fields gate (see
        TestCriticalFailureGate below), so the message now ends with that
        failure block rather than the lock_msg's closing line directly --
        that's expected, not a regression.
        """
        msg = self._message_for("")
        assert "Additional loan file details" not in msg
        assert "ALL SUBSEQUENT STEPS MUST USE THESE VALUES ***" in msg
        assert msg.rstrip().endswith("no document requests will be generated.")


# ---------------------------------------------------------------------------
# Tool registration (registry.py, tools/__init__.py, step_loader.py)
# ---------------------------------------------------------------------------

class TestToolRegistration:
    def test_registered_in_general_tool_names(self):
        assert "inspect_raw_xml_data" in GENERAL_TOOL_NAMES

    def test_registered_in_all_tools(self):
        assert "inspect_raw_xml_data" in [t.name for t in ALL_TOOLS]

    def test_registered_in_general_tools_list(self):
        assert "inspect_raw_xml_data" in [t.name for t in GENERAL_TOOLS]

    @pytest.mark.parametrize("step", [
        "STEP_00", "STEP_01", "STEP_02", "STEP_03", "STEP_04",
        "STEP_05", "STEP_06", "STEP_07", "STEP_08", "STEP_09",
    ])
    def test_available_at_every_step(self, step):
        tools = resolve_tools_for_step({"current_step": step})
        assert "inspect_raw_xml_data" in [t.name for t in tools], (
            f"inspect_raw_xml_data missing from {step}'s resolved tools"
        )


# ---------------------------------------------------------------------------
# Broad regression sweep: every sample loan file must parse without
# exceptions through the full STEP_00 chain + the new tool, old or new code
# path alike. Catches format-variance edge cases a single hand-picked file
# wouldn't.
# ---------------------------------------------------------------------------

class TestRegressionAcrossAllSampleFiles:
    @pytest.mark.parametrize(
        "xml_path", ALL_SAMPLE_XML_PATHS, ids=[p.parent.name for p in ALL_SAMPLE_XML_PATHS],
    )
    def test_full_step00_chain_does_not_raise(self, xml_path):
        xml_content = xml_path.read_text(encoding="utf-8")

        cmd0 = parse_loan_file.func(state={"loan_file_xml": xml_content})
        ss0 = cmd0.update["scenario_summary"]
        assert "_loan_profile" in ss0

        cmd1 = build_scenario_summary.func(state={"scenario_summary": ss0})
        ss1 = cmd1.update["scenario_summary"]
        merged = dict(ss0)
        merged.update(ss1)

        ctx = _build_reference_context(merged, [])
        assert isinstance(ctx, dict)

        listing = inspect_raw_xml_data.func(state={"scenario_summary": merged})
        assert isinstance(listing, str) and len(listing) > 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
