"""Unit tests for ticket 4278 ("Predicted Conditions: Add the Documents
Almost Every Loan Needs").

Covers:
  - is_wholesale flag + the new "channel" condition resolver (which also
    retroactively fixes previously-dormant channel=Wholesale conditions
    already sitting in data/canonical_doc_specs.json).
  - conditional_docs() gating for: title company docs/CPL/property tax
    cert (wholesale), appraisal delivery confirmation (every loan with an
    appraisal), W-2 or 1099 tax transcripts (wage/1099 income, not DSCR),
    entity documents (LLC vesting), appraisal transfer letter + AIR cert
    (different-lender appraisal evidence).

Run with: python3 -m pytest tests/test_ticket_4278_new_documents.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_DIR))

from dotenv import load_dotenv
load_dotenv(REPO_DIR / ".env")

from tools.doc_rules import derive_flags, conditional_docs, _resolve_condition


def _base_ss(**overrides):
    ss = {
        "purpose": "Purchase",
        "occupancy": "Owner-Occupied",
        "income_profile": {"income_types": ["W2"]},
        "asset_profile": {},
        "reo_summary": {"total_properties_owned": 0},
        "property": {},
        "_submitted_docs": [],
        "_eligibility_data": {},
    }
    ss.update(overrides)
    return ss


def _types(docs):
    return {d["document_type"] for d in docs}


class TestWholesaleChannelFlag:
    def test_wholesale_loan_gets_title_company_docs_cpl_and_tax_cert(self):
        ss = _base_ss(channel="wholesale")
        flags = derive_flags(ss)
        docs = conditional_docs(flags)
        types = _types(docs)
        assert "Title Company Wiring Instructions" in types
        assert "Title Company E&O Insurance" in types
        assert "Closing Protection Letter" in types
        assert "Property Tax Certificate" in types

    def test_retail_loan_does_not_get_wholesale_docs(self):
        ss = _base_ss(channel="retail")
        flags = derive_flags(ss)
        docs = conditional_docs(flags)
        types = _types(docs)
        assert "Closing Protection Letter" not in types
        assert "Title Company Wiring Instructions" not in types

    def test_dormant_channel_condition_in_spec_library_now_resolves(self):
        """The channel=Wholesale / channel=retail_or_wholesale conditions
        already sitting in data/canonical_doc_specs.json were previously
        unresolvable (no "channel" case in _resolve_atomic at all) --
        confirm they now actually resolve based on real channel data."""
        wholesale_flags = derive_flags(_base_ss(channel="wholesale"))
        retail_flags = derive_flags(_base_ss(channel="retail"))
        assert _resolve_condition("channel=Wholesale", wholesale_flags) is True
        assert _resolve_condition("channel=Wholesale", retail_flags) is False
        assert _resolve_condition("channel=retail_or_wholesale", wholesale_flags) is True
        assert _resolve_condition("channel=retail_or_wholesale", retail_flags) is True
        assert _resolve_condition("channel=correspondent", retail_flags) is False


class TestAppraisalDeliveryConfirmation:
    def test_any_loan_gets_appraisal_delivery_confirmation(self):
        flags = derive_flags(_base_ss())
        docs = conditional_docs(flags)
        assert "Appraisal Delivery Confirmation" in _types(docs)


class TestWageOr1099TaxTranscripts:
    def test_wage_earner_loan_gets_tax_transcripts(self):
        ss = _base_ss(income_profile={"income_doc_label": "Full Documentation"})
        flags = derive_flags(ss)
        assert flags["has_wage_or_1099_income"] is True
        docs = conditional_docs(flags)
        assert "W-2 or 1099 Tax Transcripts" in _types(docs)

    def test_dscr_loan_no_wage_income_gets_no_tax_transcripts(self):
        ss = _base_ss(program="DSCR", dscr_label="DSCR")
        flags = derive_flags(ss)
        assert flags["has_wage_or_1099_income"] is False
        docs = conditional_docs(flags)
        assert "W-2 or 1099 Tax Transcripts" not in _types(docs)


class TestEntityDocuments:
    def test_llc_vesting_gets_entity_documents(self):
        ss = _base_ss(_eligibility_data={
            "application_data": {"LLCOrLegalEntity": True},
        })
        flags = derive_flags(ss)
        assert flags["is_llc"] is True
        docs = conditional_docs(flags)
        types = _types(docs)
        assert "Articles of Organization" in types
        assert "Federal Tax Id Number" in types

    def test_individual_vesting_gets_no_entity_documents(self):
        flags = derive_flags(_base_ss())
        assert flags["is_llc"] is False
        docs = conditional_docs(flags)
        types = _types(docs)
        assert "Articles of Organization" not in types
        assert "Federal Tax Id Number" not in types


class TestAppraisalDifferentLender:
    def test_appraisal_names_different_lender_adds_transfer_letter_and_air_cert(self):
        ss = _base_ss(
            lender="NQM Funding, LLC",
            _submitted_docs=[{
                "name": "Appraisal Report", "doc_type": "other",
                "extracted_fields": {"lenderName": "Some Other Lender Inc"},
            }],
        )
        flags = derive_flags(ss)
        assert flags["appraisal_different_lender"] is True
        docs = conditional_docs(flags)
        types = _types(docs)
        assert "Appraisal Transfer Letter" in types
        assert "AIR Certification" in types

    def test_appraisal_names_same_lender_no_extra_docs(self):
        ss = _base_ss(
            lender="NQM Funding, LLC",
            _submitted_docs=[{
                "name": "Appraisal Report", "doc_type": "other",
                "extracted_fields": {"lenderName": "NQM Funding, LLC"},
            }],
        )
        flags = derive_flags(ss)
        assert flags["appraisal_different_lender"] is False
        docs = conditional_docs(flags)
        types = _types(docs)
        assert "Appraisal Transfer Letter" not in types
        assert "AIR Certification" not in types

    def test_no_lender_data_at_all_defaults_to_no_extra_docs(self):
        flags = derive_flags(_base_ss())
        assert flags["appraisal_different_lender"] is False
