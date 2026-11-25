"""Tests for the DSCR/DTI required-field swap (tools/scenario_tools.py:
build_scenario_summary's "_REQUIRED_FIELDS" / "missing_core_variables"
logic).

2026-11-03 design decision: DTI is required for every loan EXCEPT when the
PRIMARY borrower's borrower_type is "DSCR" -- identified via the
loan_file_xml "Incomes" array's entry where income_tier == "Primary" AND
is_primary_borrower == True (see tools/shared/xml_parser.py's
_primary_borrower_income), e.g.:
    "Incomes": [
        {"income_tier": "Primary", "is_primary_borrower": true,
         "borrower_type": "DSCR", "income_doc": "DSCR Long Term Rent",
         "bank_statements_kind": null},
    ]
For such DSCR borrowers, DTI is swapped out for a required "DSCR" field
instead, sourced from a top-level "DSCR" key in loan_file_xml (NOT from the
Incomes array) -- one of "DSCR No Ratio", "DSCR < 1%", "DSCR 1-1.14%",
"DSCR > 1.15%".

Run with: python3 -m pytest tests/test_dscr_replaces_dti_requirement.py -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_DIR))

from dotenv import load_dotenv

load_dotenv(REPO_DIR / ".env")

from tools.scenario_tools import build_scenario_summary, parse_loan_file

# A complete-enough flat loan-data JSON to keep every OTHER required field
# satisfied, isolating just the DTI/DSCR swap under test.
_BASE_FLAT_JSON: dict = {
    "FicoScore": 724,
    "LTV": 70,
    "CLTV": 70,
    "LoanAmount": 420000,
    "PropertyValue": 600000,
    "PropertyType": "SFR",
    "Occupancy": "Investment",
    "LoanPurpose": "Purchase",
    "Channel": "wholesale",
    "Citizenship": "US Citizen",
    "State": "CA",
    "County": "Orange",
    "LoanType": "Conventional",
}

_DSCR_INCOMES: list[dict] = [
    {
        "income_tier": "Primary",
        "is_primary_borrower": True,
        "borrower_type": "DSCR",
        "income_doc": "DSCR Long Term Rent",
        "bank_statements_kind": None,
    },
]

_DSCR_INCOME_DOC_ONLY_WRONG_BORROWER_TYPE: list[dict] = [
    {
        "income_tier": "Primary",
        "is_primary_borrower": True,
        # borrower_type deliberately NOT "DSCR" here (present, so it still
        # satisfies BorrowerType's own independent bare-minimum-presence
        # requirement, but wrong) -- simulates the real-world case where
        # the upstream loan-file source doesn't reliably populate
        # BorrowerType="DSCR" for a DSCR-program loan (e.g. it derives
        # DSCR status from income data rather than the loan's actual
        # program, and income is blank/thin, so BorrowerType falls back to
        # some generic non-DSCR value). Only the income_doc label signals
        # DSCR in this scenario.
        "borrower_type": "Unknown",
        "income_doc": "DSCR Long Term Rent",
        "bank_statements_kind": None,
    },
]

_NON_DSCR_INCOMES: list[dict] = [
    {
        "income_tier": "Primary",
        "is_primary_borrower": True,
        "borrower_type": "Self-Employed",
        "income_doc": "Bank Statements: 12 Mo.",
        "bank_statements_kind": "Personal",
    },
]


def _run_pipeline(flat_json: dict) -> dict:
    state = {"loan_file_xml": json.dumps(flat_json)}
    cmd1 = parse_loan_file.func(state=state)
    state.update(cmd1.update)
    cmd2 = build_scenario_summary.func(state=state)
    return cmd2.update


def _missing_fields(update: dict) -> set[str]:
    return {m["field"] for m in update["missing_core_variables"]}


class TestDscrPrimaryBorrowerReplacesDtiWithDscrField:
    def test_dscr_borrower_type_does_not_require_dti(self):
        flat = {**_BASE_FLAT_JSON, "Incomes": _DSCR_INCOMES, "DSCR": "DSCR 1-1.14%"}
        update = _run_pipeline(flat)
        assert "DTI" not in _missing_fields(update)

    def test_dscr_borrower_type_requires_dscr_field_when_absent(self):
        flat = {**_BASE_FLAT_JSON, "Incomes": _DSCR_INCOMES}
        update = _run_pipeline(flat)
        assert "DSCR" in _missing_fields(update)
        assert "DTI" not in _missing_fields(update)

    def test_dscr_field_present_satisfies_the_requirement(self):
        for tier in ("DSCR No Ratio", "DSCR < 1%", "DSCR 1-1.14%", "DSCR > 1.15%"):
            flat = {**_BASE_FLAT_JSON, "Incomes": _DSCR_INCOMES, "DSCR": tier}
            update = _run_pipeline(flat)
            assert "DSCR" not in _missing_fields(update), f"tier={tier!r}"

    def test_dscr_label_surfaced_on_summary(self):
        flat = {**_BASE_FLAT_JSON, "Incomes": _DSCR_INCOMES, "DSCR": "DSCR > 1.15%"}
        update = _run_pipeline(flat)
        assert update["scenario_summary"]["dscr_label"] == "DSCR > 1.15%"


class TestNonDscrPrimaryBorrowerStillRequiresDti:
    def test_non_dscr_borrower_type_requires_dti_not_dscr(self):
        flat = {**_BASE_FLAT_JSON, "Incomes": _NON_DSCR_INCOMES}
        update = _run_pipeline(flat)
        missing = _missing_fields(update)
        assert "DTI" in missing
        assert "DSCR" not in missing

    def test_non_dscr_borrower_type_with_dti_present_satisfies_it(self):
        flat = {**_BASE_FLAT_JSON, "Incomes": _NON_DSCR_INCOMES, "DTI": 41.2}
        update = _run_pipeline(flat)
        assert "DTI" not in _missing_fields(update)

    def test_dscr_field_present_but_borrower_type_not_dscr_still_requires_dti(self):
        """A stray top-level "DSCR" value doesn't matter if the primary
        borrower isn't actually DSCR-qualified -- DTI is still what's
        required."""
        flat = {
            **_BASE_FLAT_JSON,
            "Incomes": _NON_DSCR_INCOMES,
            "DSCR": "DSCR No Ratio",
        }
        update = _run_pipeline(flat)
        missing = _missing_fields(update)
        assert "DTI" in missing
        assert "DSCR" not in missing


# ---------------------------------------------------------------------------
# Same DTI/DSCR substitution, but for the HARD "critical_failure" bare-
# minimum-fields gate (build_scenario_summary, fires only when NO
# eligibility_json is provided -- see tests/test_critical_failure_gate.py).
# Without this substitution, every eligibility_json-less DSCR loan would
# permanently fail this gate on DTI, a field DSCR borrowers structurally
# never have -- confirmed live against the deployed aws-dev Lambda
# (2026-11-03) before this fix was added.
# ---------------------------------------------------------------------------

class TestCriticalFailureGateDscrSubstitution:
    def test_dscr_borrower_with_dscr_field_does_not_trigger_critical_failure(self):
        flat = {**_BASE_FLAT_JSON, "Incomes": _DSCR_INCOMES, "DSCR": "DSCR No Ratio"}
        update = _run_pipeline(flat)
        assert update.get("critical_failure") is None

    def test_dscr_borrower_without_dscr_field_fails_naming_dscr_not_dti(self):
        flat = {**_BASE_FLAT_JSON, "Incomes": _DSCR_INCOMES}
        update = _run_pipeline(flat)
        cf = update.get("critical_failure")
        assert cf is not None
        assert "DSCR" in cf["missing_fields"]
        assert "DTI" not in cf["missing_fields"]

    def test_non_dscr_borrower_without_dti_still_fails_naming_dti(self):
        flat = {**_BASE_FLAT_JSON, "Incomes": _NON_DSCR_INCOMES}
        update = _run_pipeline(flat)
        cf = update.get("critical_failure")
        assert cf is not None
        assert "DTI" in cf["missing_fields"]
        assert "DSCR" not in cf["missing_fields"]

    def test_non_dscr_borrower_with_dti_does_not_trigger_critical_failure(self):
        flat = {**_BASE_FLAT_JSON, "Incomes": _NON_DSCR_INCOMES, "DTI": 41.2}
        update = _run_pipeline(flat)
        assert update.get("critical_failure") is None


# ---------------------------------------------------------------------------
# 2026-11-25 bug fix: the critical bare-minimum gate used to check ONLY
# borrower_type == "DSCR", with no fallback -- unlike the advisory-only
# missing_core_variables check above, which also falls back to
# income_types when borrower_type doesn't cleanly say "DSCR". That gap
# meant a DSCR-program loan whose upstream source doesn't reliably
# populate BorrowerType="DSCR" (e.g. because it derives DSCR status from
# income data rather than program, and income is blank/thin) would
# incorrectly CRITICALLY FAIL the entire run demanding DTI, instead of
# correctly substituting DSCR. These tests pin the fixed behavior: the
# gate must now agree with the advisory check and fall back to
# income_types too.
# ---------------------------------------------------------------------------


class TestCriticalFailureGateFallsBackToIncomeTypesLikeAdvisoryCheck:
    def test_blank_borrower_type_with_dscr_income_doc_label_does_not_demand_dti(self):
        flat = {
            **_BASE_FLAT_JSON,
            "Incomes": _DSCR_INCOME_DOC_ONLY_WRONG_BORROWER_TYPE,
        }
        update = _run_pipeline(flat)
        cf = update.get("critical_failure")
        # Must not fail at all, or if it does, must name DSCR (missing tier
        # value) -- NEVER DTI, since this borrower structurally has none.
        if cf is not None:
            assert "DTI" not in cf["missing_fields"]
            assert "DSCR" in cf["missing_fields"]

    def test_blank_borrower_type_with_dscr_income_doc_and_tier_present_passes(self):
        flat = {
            **_BASE_FLAT_JSON,
            "Incomes": _DSCR_INCOME_DOC_ONLY_WRONG_BORROWER_TYPE,
            "DSCR": "DSCR 1-1.14%",
        }
        update = _run_pipeline(flat)
        assert update.get("critical_failure") is None

    def test_advisory_and_critical_gate_agree_on_dscr_detection(self):
        """The advisory check (missing_core_variables) and the critical
        bare-minimum gate must reach the same DSCR/DTI conclusion given
        the same ambiguous input -- this is the exact inconsistency that
        was fixed."""
        flat = {
            **_BASE_FLAT_JSON,
            "Incomes": _DSCR_INCOME_DOC_ONLY_WRONG_BORROWER_TYPE,
        }
        update = _run_pipeline(flat)
        advisory_missing = _missing_fields(update)
        cf = update.get("critical_failure")
        cf_missing = set(cf["missing_fields"]) if cf else set()
        assert ("DTI" in advisory_missing) == ("DTI" in cf_missing)
        assert ("DSCR" in advisory_missing) == ("DSCR" in cf_missing)
