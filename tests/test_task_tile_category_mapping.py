"""Unit tests for Task Tile category -> condition linking (ticket 4275,
"Predicted Conditions: Recognise Documents That Are Already in the File").

Background: the 17 Nov 2026 UAT accuracy report (PC-07) and this follow-up
ticket (4275) found that several documents were correctly present in the
manifest, but submitted under Task Tile's own category NAME for that
document rather than any name the engine's _DOCTYPE_ALIASES map already
recognised -- so the condition was reported as having no document, even
though the real document was right there. Confirmed on real UAT loans
90000108510 (Verification of Employment / EMD Check), 90000108512 (Non QM
Bank Statement Analysis Worksheet), and 90000108513 (Asset Depletion
Worksheet, IRS 4506-C Authorization).

_find_all_submitted_docs() (via _DOCTYPE_ALIASES in tools/merger_tools.py)
is the exact function that decides whether a submitted manifest document
(keyed by its raw Task Tile `category_name`, carried through as the
submitted-doc "name" field -- see tools/shared/manifest_parser.py's
category_name -> "name" mapping) counts as evidence for a given canonical
document_type. Testing at this layer is a direct, LLM-free proof of
"linked" vs. "not linked" -- it is the same lookup run_satisfaction_pass
uses to populate a document request's document_ids.

Covers all 6 acceptance criteria from ticket 4275:
  AC1: "Verification of Income"        -> Verification of Employment
  AC2: "Transaction History"           -> EMD Check
  AC3: "Income Calculations Worksheet" -> Non QM Bank Statement Analysis Worksheet
  AC4: "Asset Worksheet"               -> Asset Depletion Worksheet
  AC5: "4506-C IVES"                   -> IRS 4506-C Authorization
  AC6: a document genuinely NOT in the file -> no match (negative control)

Run with: python3 -m pytest tests/test_task_tile_category_mapping.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_DIR))

from dotenv import load_dotenv
load_dotenv(REPO_DIR / ".env")

from tools.merger_tools import _find_all_submitted_docs


def _submitted(category_name: str, doc_type: str = "") -> dict:
    """A minimal submitted-manifest-document dict, shaped exactly like
    tools/shared/manifest_parser.py's real output: "name" carries the raw
    Task Tile category_name, "doc_type" carries the (often unrelated,
    generic) CATEGORY_ID_TO_DOC_TYPE bucket."""
    return {
        "doc_id": "999",
        "name": category_name,
        "doc_type": doc_type or "other",
        "document_id": "11111111-1111-1111-1111-111111111111",
        "extracted_fields": {"some_field": "some_value"},
        "flags": [],
    }


class TestAC1_VerificationOfIncomeToVOE:
    """Loan 510: employment verification report filed under Task Tile
    category 'Verification of Income' must link to Verification of
    Employment."""

    def test_links(self):
        docs = [_submitted("Verification of Income")]
        matches = _find_all_submitted_docs("Verification of Employment", docs)
        assert len(matches) == 1
        assert matches[0]["name"] == "Verification of Income"


class TestAC2_TransactionHistoryToEMDCheck:
    """Loan 510: cleared-deposit account activity filed under Task Tile
    category 'Transaction History' must link to EMD Check."""

    def test_links(self):
        docs = [_submitted("Transaction History")]
        matches = _find_all_submitted_docs("EMD Check", docs)
        assert len(matches) == 1
        assert matches[0]["name"] == "Transaction History"


class TestAC3_IncomeCalculationsWorksheetToBankStatementAnalysis:
    """Loan 512: bank-statement income worksheets filed under Task Tile
    category 'Income Calculations Worksheet' must link to Non QM Bank
    Statement Analysis Worksheet."""

    def test_links(self):
        docs = [_submitted("Income Calculations Worksheet")]
        matches = _find_all_submitted_docs(
            "Non QM Bank Statement Analysis Worksheet", docs
        )
        assert len(matches) == 1
        assert matches[0]["name"] == "Income Calculations Worksheet"


class TestAC4_AssetWorksheetToAssetDepletionWorksheet:
    """Loan 513 (asset-depletion refi): worksheet filed under Task Tile
    category 'Asset Worksheet' must link to Asset Depletion Worksheet.
    Also: the underlying bank/retirement statements backing an
    asset-depletion income condition must link to the (mandatory-floor)
    Bank Statement request via the existing 'other asset' alias, which
    is Task Tile's own generic bucket for brokerage/retirement account
    statements (see tools/shared/normalize.py's IRA/retirement note)."""

    def test_worksheet_links(self):
        docs = [_submitted("Asset Worksheet")]
        matches = _find_all_submitted_docs("Asset Depletion Worksheet", docs)
        assert len(matches) == 1
        assert matches[0]["name"] == "Asset Worksheet"

    def test_statements_link_to_income_condition(self):
        docs = [
            _submitted("Bank Statement"),
            _submitted("Bank Statement"),
            _submitted("Bank Statement"),
            _submitted("Other Asset"),  # retirement account statement bucket
        ]
        matches = _find_all_submitted_docs("Bank Statement", docs)
        assert len(matches) == 4


class TestAC5_4506CIVESToIRS4506C:
    """Loan 513: the 4506-C copy the underwriter actually used was filed
    under Task Tile category '4506-C IVES' and is the ONLY 4506-C-type
    document in the file -- it must link to IRS 4506-C Authorization."""

    def test_links(self):
        docs = [_submitted("4506-C IVES")]
        matches = _find_all_submitted_docs("IRS 4506-C Authorization", docs)
        assert len(matches) == 1
        assert matches[0]["name"] == "4506-C IVES"


class TestAC6_GenuinelyMissingDocumentStillShowsNoMatch:
    """Negative control: none of the new/existing aliases should cause a
    document that truly is NOT in the file to false-positive match."""

    def test_no_match_when_not_present(self):
        docs = [_submitted("Purchase Contract"), _submitted("Credit Report")]
        assert _find_all_submitted_docs("Verification of Employment", docs) == []
        assert _find_all_submitted_docs("EMD Check", docs) == []
        assert _find_all_submitted_docs(
            "Non QM Bank Statement Analysis Worksheet", docs
        ) == []
        assert _find_all_submitted_docs("Asset Depletion Worksheet", docs) == []
        assert _find_all_submitted_docs("IRS 4506-C Authorization", docs) == []

    def test_empty_manifest_matches_nothing(self):
        assert _find_all_submitted_docs("Verification of Employment", []) == []
