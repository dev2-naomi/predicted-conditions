"""Tests for the "flat loan-data JSON object" alternative shape of the
loan_file_xml input (tools/shared/xml_parser.py:
_try_parse_flat_loan_json / _flat_loan_json_to_profile).

2026-10-12 design decision: loan_file_xml can arrive as either real MISMO
XML text OR a flat JSON object shaped like:
    {"FicoScore": 724, "LTV": 70, "CLTV": 70, "DTI": 41.2,
     "LoanAmount": 420000, "PropertyValue": 600000, "Occupancy": "Primary",
     "LoanPurpose": "Purchase", "IncomeDocType": "Bank Statements: 12 Mo.",
     "Channel": "wholesale", "BorrowerType": "Self-Employed",
     "Citizenship": "US Citizen", "State": "CA", "County": "Orange",
     "LoanType": "Conventional", "ReservesMonths": 0,
     "FirstTimeHomeBuyer": "No"}
xml_to_loan_profile()/parse_mismo_xml() must detect which shape they were
given and return a profile dict in the same contract either way, so
nothing downstream needs to know or care which format arrived.

Run with: python3 -m pytest tests/test_flat_loan_json_input.py -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_DIR))

from dotenv import load_dotenv
load_dotenv(REPO_DIR / ".env")

import pytest

from tools.scenario_tools import build_scenario_summary, parse_loan_file
from tools.shared.xml_parser import (
    parse_mismo_xml,
    xml_to_loan_profile,
)

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
    "ReservesMonths": 0,
    "FirstTimeHomeBuyer": "No",
}

_BIBBY_CASE_AVAILABLE = (REPO_DIR / "compiled_inputs/bibby_10x/loan_file.xml").exists()
_requires_bibby_case = pytest.mark.skipif(
    not _BIBBY_CASE_AVAILABLE,
    reason="bibby_10x is a local-only, untracked real-loan-data fixture (not committed to git).",
)


class TestFlatJsonDetectionAndMapping:
    def test_dict_input_parses_without_error(self):
        profile = xml_to_loan_profile(SAMPLE_FLAT_JSON)
        assert "parse_error" not in profile

    def test_json_string_input_parses_without_error(self):
        profile = xml_to_loan_profile(json.dumps(SAMPLE_FLAT_JSON))
        assert "parse_error" not in profile

    def test_all_sixteen_fields_map_correctly(self):
        meta = xml_to_loan_profile(SAMPLE_FLAT_JSON)["metadata"]
        assert meta["fico"] == 724
        assert meta["ltv_pct"] == 70
        assert meta["cltv_pct"] == 70
        assert meta["dti"] == 41.2
        assert meta["loan_amount"] == 420000
        assert meta["property_value"] == 600000
        assert meta["property_type"] == "SFR"
        assert meta["occupancy"] == "Primary"
        assert meta["purpose"] == "Purchase"
        assert meta["income_doc"] == "Bank Statements: 12 Mo."
        assert meta["channel"] == "wholesale"
        assert meta["borrower_type"] == "Self-Employed"
        assert meta["citizenship"] == "US Citizen"
        assert meta["state"] == "CA"
        assert meta["county"] == "Orange"
        assert meta["loan_type"] == "Conventional"

    def test_reserves_months_maps_to_months_reserves(self):
        meta = xml_to_loan_profile(SAMPLE_FLAT_JSON)["metadata"]
        assert meta["months_reserves"] == 0

    def test_unrecognized_fields_preserved_not_dropped(self):
        profile = xml_to_loan_profile(SAMPLE_FLAT_JSON)
        extras = profile["_xml_supplemental"]["extra_flat_json_fields"]
        assert extras == {"FirstTimeHomeBuyer": "No"}

    def test_parse_mismo_xml_also_handles_flat_json_without_erroring(self):
        parsed = parse_mismo_xml(SAMPLE_FLAT_JSON)
        assert "parse_error" not in parsed
        assert parsed.get("format") == "flat_json"

    def test_fields_absent_from_flat_shape_default_to_none_like_xml_would(self):
        """Borrower name, property address, etc. have no equivalent in the
        flat shape -- they should default exactly the way real XML leaves
        them when genuinely absent, not raise or behave differently."""
        meta = xml_to_loan_profile(SAMPLE_FLAT_JSON)["metadata"]
        assert meta["borrower"] == {"suffix": "", "last_name": "", "first_name": "", "middle_name": ""}
        assert meta["property_address"] is None
        assert meta["co_borrower"] is None


class TestFlatJsonDoesNotMisfireOnRealXmlOrGarbage(object):
    def test_empty_string_is_not_treated_as_flat_json(self):
        profile = xml_to_loan_profile("")
        # Empty string triggers the real XML path's own parse_error (ET
        # can't parse ""), not a false-positive flat-JSON match.
        assert "parse_error" in profile

    def test_small_unrelated_json_object_is_not_misclassified(self):
        """A JSON object with fewer than the minimum matching keys should
        NOT be treated as this flat shape -- avoids false positives on
        incidental small JSON blobs that happen to share one key name."""
        profile = xml_to_loan_profile(json.dumps({"State": "CA"}))
        assert "parse_error" in profile  # falls through to real-XML parsing, which fails

    @_requires_bibby_case
    def test_real_mismo_xml_is_unaffected(self):
        xml = (REPO_DIR / "compiled_inputs/bibby_10x/loan_file.xml").read_text(encoding="utf-8")
        profile = xml_to_loan_profile(xml)
        assert "parse_error" not in profile
        assert profile["metadata"]["borrower"]["last_name"] == "Bibby"
        assert profile["metadata"]["ltv_pct"] == pytest.approx(63.043, abs=0.01)


class TestFlatJsonThroughFullPipeline:
    def test_parse_loan_file_tool_accepts_flat_json_string(self):
        state = {"loan_file_xml": json.dumps(SAMPLE_FLAT_JSON)}
        cmd = parse_loan_file.func(state=state)
        meta = cmd.update["scenario_summary"]["_loan_profile"]["metadata"]
        assert meta["ltv_pct"] == 70
        assert meta["fico"] == 724

    def test_gate_does_not_fail_when_flat_json_supplies_all_fields(self):
        """Since the sample flat JSON above supplies all 16 canonical
        fields directly (including FicoScore/IncomeDocType/Channel/
        BorrowerType, which real MISMO XML essentially never does), the
        critical_failure gate should NOT fire even with no eligibility_json
        at all."""
        state = {"loan_file_xml": json.dumps(SAMPLE_FLAT_JSON)}
        cmd1 = parse_loan_file.func(state=state)
        state.update(cmd1.update)
        cmd2 = build_scenario_summary.func(state=state)
        assert cmd2.update.get("critical_failure") is None

    def test_gate_still_fires_when_flat_json_is_missing_a_field(self):
        incomplete = {k: v for k, v in SAMPLE_FLAT_JSON.items() if k != "PropertyType"}
        state = {"loan_file_xml": json.dumps(incomplete)}
        cmd1 = parse_loan_file.func(state=state)
        state.update(cmd1.update)
        cmd2 = build_scenario_summary.func(state=state)
        cf = cmd2.update.get("critical_failure")
        assert cf is not None
        assert "PropertyType" in cf["missing_fields"]


# ---------------------------------------------------------------------------
# "Incomes" array shape (2026-10-13 follow-up)
# ---------------------------------------------------------------------------
# The flat shape's old top-level "IncomeDocType"/"BorrowerType" scalar
# fields are being replaced by an "Incomes" array, so multiple borrowers'
# income documentation (primary, secondary, co-borrower) can be captured at
# once instead of just one -- matching the same array shape the eligibility
# engine itself consumes. The bare-minimum gate only requires the PRIMARY
# BORROWER's entry (income_tier == "Primary" AND is_primary_borrower ==
# True) to carry income_doc + borrower_type; other entries (that borrower's
# own secondary income, or a co-borrower's income of any tier) are not
# required for the gate, though they're preserved verbatim for any future
# consumer.
# ---------------------------------------------------------------------------

SAMPLE_INCOMES_ARRAY: list[dict] = [
    {
        "income_tier": "Primary",
        "is_primary_borrower": True,
        "borrower_type": "Retired / Fixed Income",
        "income_doc": "Asset Utilization",
        "bank_statements_kind": None,
    },
    {
        "income_tier": "Secondary",
        "is_primary_borrower": True,
        "borrower_type": "Wage Earner",
        "income_doc": "Written VOE",
        "bank_statements_kind": None,
    },
    {
        "income_tier": "Primary",
        "is_primary_borrower": False,
        "borrower_type": "Self-Employed",
        "income_doc": "Bank Statements: 12 Mo.",
        "bank_statements_kind": "Personal",
    },
]

SAMPLE_FLAT_JSON_WITH_INCOMES: dict = {
    k: v for k, v in SAMPLE_FLAT_JSON.items() if k not in ("IncomeDocType", "BorrowerType")
}
SAMPLE_FLAT_JSON_WITH_INCOMES["Incomes"] = SAMPLE_INCOMES_ARRAY


class TestIncomesArrayDetectionAndMapping:
    def test_primary_borrowers_entry_populates_income_doc_and_borrower_type(self):
        """income_tier == "Primary" alone is not enough -- the entry with
        is_primary_borrower == False also has income_tier == "Primary" (its
        OWN primary income, not the primary borrower's), and must be
        skipped in favor of the entry where is_primary_borrower is True."""
        meta = xml_to_loan_profile(SAMPLE_FLAT_JSON_WITH_INCOMES)["metadata"]
        assert meta["income_doc"] == "Asset Utilization"
        assert meta["borrower_type"] == "Retired / Fixed Income"

    def test_secondary_and_coborrower_entries_do_not_leak_into_metadata(self):
        meta = xml_to_loan_profile(SAMPLE_FLAT_JSON_WITH_INCOMES)["metadata"]
        assert meta["income_doc"] != "Written VOE"
        assert meta["borrower_type"] != "Self-Employed"

    def test_full_incomes_array_preserved_in_supplemental_data(self):
        profile = xml_to_loan_profile(SAMPLE_FLAT_JSON_WITH_INCOMES)
        assert profile["_xml_supplemental"]["incomes"] == SAMPLE_INCOMES_ARRAY

    def test_incomes_key_not_treated_as_an_unrecognized_extra_field(self):
        profile = xml_to_loan_profile(SAMPLE_FLAT_JSON_WITH_INCOMES)
        assert "Incomes" not in profile["_xml_supplemental"]["extra_flat_json_fields"]

    def test_old_flat_fields_still_work_when_incomes_array_absent(self):
        """Backward compatibility: a caller not yet sending "Incomes" at
        all should still work exactly as before, via the old flat
        IncomeDocType/BorrowerType fields."""
        meta = xml_to_loan_profile(SAMPLE_FLAT_JSON)["metadata"]
        assert meta["income_doc"] == "Bank Statements: 12 Mo."
        assert meta["borrower_type"] == "Self-Employed"

    def test_incomes_array_takes_priority_over_stale_flat_fields_if_both_present(self):
        both = dict(SAMPLE_FLAT_JSON_WITH_INCOMES)
        both["IncomeDocType"] = "W2"
        both["BorrowerType"] = "Wage Earner"
        meta = xml_to_loan_profile(both)["metadata"]
        assert meta["income_doc"] == "Asset Utilization"
        assert meta["borrower_type"] == "Retired / Fixed Income"

    def test_no_matching_primary_borrower_entry_leaves_fields_none(self):
        """Only secondary-income / non-primary-borrower entries present --
        income_doc/borrower_type must come up empty (not accidentally pick
        up one of those), so the bare-minimum gate correctly flags them as
        missing rather than silently using the wrong borrower's data."""
        no_primary = dict(SAMPLE_FLAT_JSON_WITH_INCOMES)
        no_primary["Incomes"] = [
            e for e in SAMPLE_INCOMES_ARRAY if not e["is_primary_borrower"]
        ]
        meta = xml_to_loan_profile(no_primary)["metadata"]
        assert meta["income_doc"] is None
        assert meta["borrower_type"] is None

    def test_empty_incomes_array_leaves_fields_none(self):
        empty = dict(SAMPLE_FLAT_JSON_WITH_INCOMES)
        empty["Incomes"] = []
        meta = xml_to_loan_profile(empty)["metadata"]
        assert meta["income_doc"] is None
        assert meta["borrower_type"] is None

    def test_malformed_incomes_value_does_not_crash(self):
        """A caller sending "Incomes" as something other than a list (e.g.
        a stray string or dict) should degrade gracefully to "no primary
        income known", not raise."""
        malformed = dict(SAMPLE_FLAT_JSON_WITH_INCOMES)
        malformed["Incomes"] = "not a list"
        meta = xml_to_loan_profile(malformed)["metadata"]
        assert meta["income_doc"] is None
        assert meta["borrower_type"] is None
        assert xml_to_loan_profile(malformed)["_xml_supplemental"]["incomes"] == []


class TestIncomesArrayThroughFullPipeline:
    def test_gate_does_not_fail_with_incomes_array_covering_primary_borrower(self):
        state = {"loan_file_xml": json.dumps(SAMPLE_FLAT_JSON_WITH_INCOMES)}
        cmd1 = parse_loan_file.func(state=state)
        state.update(cmd1.update)
        cmd2 = build_scenario_summary.func(state=state)
        assert cmd2.update.get("critical_failure") is None

    def test_gate_fires_when_incomes_array_has_no_primary_borrower_entry(self):
        """Matches the user-reported scenario precisely: the Incomes array
        is present and non-empty, but every entry's is_primary_borrower is
        False (only secondary/co-borrower income is covered) -- the gate
        must still fire, naming both IncomeDocType and BorrowerType."""
        no_primary = dict(SAMPLE_FLAT_JSON_WITH_INCOMES)
        no_primary["Incomes"] = [
            e for e in SAMPLE_INCOMES_ARRAY if not e["is_primary_borrower"]
        ]
        state = {"loan_file_xml": json.dumps(no_primary)}
        cmd1 = parse_loan_file.func(state=state)
        state.update(cmd1.update)
        cmd2 = build_scenario_summary.func(state=state)
        cf = cmd2.update.get("critical_failure")
        assert cf is not None
        assert {"IncomeDocType", "BorrowerType"} <= set(cf["missing_fields"])

    def test_gate_does_not_fail_when_eligibility_json_present_even_without_incomes(self):
        """Sanity: eligibility_json presence still bypasses the gate
        entirely, same as every other field -- the Incomes array only
        matters for the no-eligibility-json path."""
        no_income_info = {
            k: v for k, v in SAMPLE_FLAT_JSON.items()
            if k not in ("IncomeDocType", "BorrowerType")
        }
        state = {
            "loan_file_xml": json.dumps(no_income_info),
            "eligibility_json": json.dumps({"application_data": {
                "IncomeDocType": "Full Doc", "BorrowerType": "Wage Earner",
            }}),
        }
        cmd1 = parse_loan_file.func(state=state)
        state.update(cmd1.update)
        from tools.scenario_tools import parse_eligibility_output

        cmd_e = parse_eligibility_output.func(state=state)
        state["scenario_summary"] = {
            **state.get("scenario_summary", {}),
            **cmd_e.update.get("scenario_summary", {}),
        }
        cmd2 = build_scenario_summary.func(state=state)
        assert cmd2.update.get("critical_failure") is None
