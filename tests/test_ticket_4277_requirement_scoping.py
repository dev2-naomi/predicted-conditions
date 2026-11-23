"""Unit tests for ticket 4277 ("Predicted Conditions: Keep Each Condition's
Requirements to What Applies").

Covers:
  - Flood-zone override sourced from a submitted Flood Hazard Determination
    document's own extracted data (not just the loan XML).
  - Hazard Insurance per-peril lines consolidated into one standard-coverage
    requirement.
  - W-2 canonical entry no longer duplicates Paystub/IRS-4506-C/VOE specs.
  - EMD Check "either-or": "not yet cleared" drops out once clearing
    evidence is on file.
  - Every condition keeps at least one requirement even when every
    conditioned item resolves False for this scenario.

Run with: python3 -m pytest tests/test_ticket_4277_requirement_scoping.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_DIR))

from dotenv import load_dotenv
load_dotenv(REPO_DIR / ".env")

from tools.doc_rules import (
    derive_flags, guideline_canonical_specs, apply_guideline_canonicalization,
)


def _base_ss(**overrides):
    ss = {
        "purpose": "Purchase",
        "occupancy": "Owner-Occupied",
        "income_profile": {"income_types": ["W2"]},
        "asset_profile": {},
        "reo_summary": {"total_properties_owned": 0},
        "property": {},
        "_submitted_docs": [],
    }
    ss.update(overrides)
    return ss


class TestFloodZoneFromSubmittedDocument:
    def test_flood_cert_shows_not_in_zone_suppresses_flood_insurance_specs(self):
        ss = _base_ss(_submitted_docs=[
            {"name": "Flood Hazard Determination", "doc_type": "other",
             "extracted_fields": {"specialFloodHazardArea": False}},
        ])
        flags = derive_flags(ss)
        assert flags["flood_insurance_required"] is False
        specs = guideline_canonical_specs("flood hazard determination", flags)
        joined = " ".join(specs).lower()
        assert "minimum flood insurance coverage must be the lower of" not in joined
        # The unconditioned "must be present"-style items still apply.
        assert any("fema" in s.lower() for s in specs)

    def test_flood_zone_letter_a_requires_insurance(self):
        ss = _base_ss(_submitted_docs=[
            {"name": "Flood Hazard Determination", "doc_type": "other",
             "extracted_fields": {"floodZone": "AE"}},
        ])
        flags = derive_flags(ss)
        assert flags["flood_insurance_required"] is True

    def test_no_flood_doc_at_all_leaves_unresolved_default_true(self):
        """No flood cert on file and no XML signal -> stays conservative
        (don't suppress specs we can't actually verify)."""
        flags = derive_flags(_base_ss())
        assert flags["flood_insurance_required"] is None

    def test_explicit_xml_signal_wins_over_submitted_doc(self):
        ss = _base_ss(
            property={"flood_insurance_required": True},
            _submitted_docs=[
                {"name": "Flood Hazard Determination", "doc_type": "other",
                 "extracted_fields": {"specialFloodHazardArea": False}},
            ],
        )
        flags = derive_flags(ss)
        assert flags["flood_insurance_required"] is True


class TestHazardInsurancePerPerilConsolidation:
    def test_no_individual_peril_lines_remain(self):
        flags = derive_flags(_base_ss())
        specs = guideline_canonical_specs("hazard insurance", flags)
        assert not any(s == "Hazard insurance must include coverage for hail." for s in specs)
        assert not any("coverage for windstorm" in s and "including" not in s for s in specs)

    def test_one_consolidated_standard_coverage_line_present(self):
        flags = derive_flags(_base_ss())
        specs = guideline_canonical_specs("hazard insurance", flags)
        assert any("standard extended coverage" in s.lower() for s in specs)


class TestW2NoLongerBleedsOtherDocumentRequirements:
    def test_w2_has_no_paystub_specific_items(self):
        flags = derive_flags(_base_ss())
        specs = guideline_canonical_specs("w2", flags)
        assert not any("pay stubs must" in s.lower() for s in specs)
        assert not any("30 days of paystubs" in s.lower() for s in specs)

    def test_w2_has_no_4506c_items(self):
        flags = derive_flags(_base_ss())
        specs = guideline_canonical_specs("w2", flags)
        assert not any("4506" in s for s in specs)

    def test_w2_still_has_its_own_form_specific_items(self):
        ss = _base_ss(income_profile={"income_doc_label": "Full Documentation"})
        flags = derive_flags(ss)
        assert flags["has_full_doc_income"] is True
        specs = guideline_canonical_specs("w2", flags)
        assert any("w-2s must be typed" in s.lower() for s in specs)


class TestEMDCheckEitherOr:
    def test_cleared_evidence_present_drops_not_yet_cleared_branch(self):
        ss = _base_ss(_submitted_docs=[
            {"name": "Transaction History", "doc_type": "other", "extracted_fields": {}},
        ])
        flags = derive_flags(ss)
        assert flags["has_emd_evidence"] is True
        specs = guideline_canonical_specs("emd check", flags)
        assert not any("has not yet cleared" in s for s in specs)
        assert any("has cleared the bank" in s for s in specs)

    def test_no_evidence_keeps_not_yet_cleared_branch_open(self):
        flags = derive_flags(_base_ss())
        assert flags["has_emd_evidence"] is False
        specs = guideline_canonical_specs("emd check", flags)
        assert any("has not yet cleared" in s for s in specs)


class TestEveryConditionHasAtLeastOneRequirement:
    def test_fallback_item_when_canonicalization_resolves_to_empty(self):
        docs = [{
            "document_type": "Flood Hazard Determination",
            "specifications": ["placeholder"],
        }]
        # Force every item in the canonical entry to resolve False by
        # faking a scenario where _resolve_condition always excludes --
        # simplest real way to hit this is a doc_type with a canonical
        # entry whose every item carries a condition that never resolves
        # true for any flags; flood_hazard_determination's own unconditioned
        # items make that hard to hit directly, so exercise the guard via
        # apply_guideline_canonicalization with a monkey-patched library
        # entry instead.
        import tools.doc_rules as doc_rules_mod

        original_loader = doc_rules_mod._load_guideline_specs
        try:
            def fake_loader():
                lib = dict(original_loader())
                lib["flood hazard determination"] = {
                    "items": [{"text": "Only applies in a flood zone", "condition": "flood_zone=A or flood_zone=V"}]
                }
                return lib
            doc_rules_mod._load_guideline_specs = fake_loader
            touched, uncovered = apply_guideline_canonicalization(docs, _base_ss())
        finally:
            doc_rules_mod._load_guideline_specs = original_loader

        assert touched == 1
        assert docs[0]["specifications"], "specifications must never be empty"
        assert "must be present in the file" in docs[0]["specifications"][0]
