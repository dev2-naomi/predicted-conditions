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
from tools.merger_tools import (
    run_satisfaction_pass, _apply_tradeline_not_verified_wording,
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


class TestSingleUnitNo24UnitFormRequirement:
    """Acceptance criterion: "Single-unit appraisal -> 2-4 unit form
    requirement not open." (PC-06's property_units=2-4 vs. 1-4 distinction
    -- pre-existing fix, confirmed here directly against this ticket's own
    AC wording rather than only via code review.)"""

    def test_single_unit_property_has_no_multi_unit_form_requirement(self):
        ss = _base_ss(property={"property_type": "Single Family"})
        flags = derive_flags(ss)
        assert flags["is_multi_unit"] is False
        specs = guideline_canonical_specs("appraisal report", flags)
        assert not any(
            "2-4" in s or "small residential income property" in s.lower()
            for s in specs
        )

    def test_four_unit_property_does_get_the_multi_unit_form_requirement(self):
        ss = _base_ss(property={"property_type": "4 Unit"})
        flags = derive_flags(ss)
        assert flags["is_multi_unit"] is True
        specs = guideline_canonical_specs("appraisal report", flags)
        assert any(
            "small residential income property" in s.lower() for s in specs
        )


class TestNoteNotCheckedBeforeClosing:
    """Acceptance criterion: "Before closing with appraisal -> no check
    against the Note shows missing." The Note/security instrument is only
    executed at closing, so a cross-document check against it can never
    resolve "missing_sibling_document" (a verdict that reads to a broker
    as a real gap in THIS file) on any pre-closing loan -- confirmed
    end-to-end via derive_flags -> apply_guideline_canonicalization ->
    run_satisfaction_pass, the same chain real document_requests go
    through."""

    def test_appraisal_note_cross_checks_do_not_show_missing_sibling_document(self):
        ss = _base_ss(_submitted_docs=[
            {"name": "Appraisal Report", "doc_type": "other",
             "extracted_fields": {"propertyUnits": 1}},
        ])
        flags = derive_flags(ss)
        dr = {
            "document_type": "Appraisal Report", "document_category": "Property",
            "priority": "P2", "severity": "SOFT-STOP",
            "specifications": [], "reasons_needed": [],
        }
        docs = [dr]
        apply_guideline_canonicalization(docs, ss)
        cross_ref_map = dr.get("_cross_reference_map") or {}
        assert any("mortgage note" in refs or "promissory note" in refs
                    for refs in (tuple(v) for v in cross_ref_map.values())), (
            "sanity check: the Note cross-reference specs must actually be "
            "present on Appraisal Report for this test to mean anything"
        )

        run_satisfaction_pass(docs, ss["_submitted_docs"], ss)

        checks = dr.get("cross_document_checks") or []
        note_checks = [
            c for c in checks
            if any(r in ("mortgage note", "promissory note") for r in c.get("cross_references", []))
        ]
        assert note_checks, "expected at least one Note-referencing cross-document check"
        for c in note_checks:
            assert c["status"] != "missing_sibling_document"
            assert c["status"] == "not_yet_available_pre_closing"
            assert "closing" in c["note"].lower()


class TestTradelineNotVerifiedWording:
    """Acceptance criterion: "Credit report with empty tradelines ->
    tradeline requirements 'not verified'." PC-12 already stopped
    auto-satisfying these on an empty creditTradeLines field (that was a
    false positive); this confirms the open spec's own text now says
    "Not verified" explicitly rather than staying as generic wording that
    doesn't distinguish "couldn't check" from "doesn't exist"."""

    def test_empty_tradelines_rewrites_open_spec_text_to_not_verified(self):
        specs = [
            "Tradeline must show account type, balance, and payment "
            "history for the past 24 months.",
            "Credit report must show scores from all three bureaus.",
        ]
        satisfied: list[dict] = []
        fields = [{"creditTradeLines": [], "mortgageSummary": {"foo": "bar"}}]
        _apply_tradeline_not_verified_wording("credit report", specs, satisfied, fields)

        assert specs[0].lower().startswith("not verified")
        assert "tradeline" in specs[0].lower()
        # A spec that isn't about tradeline-detail evidence at all (the
        # 3-bureau score spec) must be left completely untouched.
        assert specs[1] == "Credit report must show scores from all three bureaus."
        # This is explicitly NOT the same claim as "satisfied" -- PC-12's
        # whole point was that empty tradelines must not be auto-passed.
        assert satisfied == []

    def test_non_empty_tradelines_leaves_wording_untouched(self):
        specs = [
            "Tradeline must show account type, balance, and payment "
            "history for the past 24 months.",
        ]
        satisfied: list[dict] = []
        fields = [{"creditTradeLines": [{"accountType": "Mortgage"}]}]
        _apply_tradeline_not_verified_wording("credit report", specs, satisfied, fields)
        assert not specs[0].lower().startswith("not verified")

    def test_non_credit_report_doc_type_unaffected(self):
        specs = ["Tradeline detail must be shown."]
        satisfied: list[dict] = []
        fields = [{"creditTradeLines": []}]
        _apply_tradeline_not_verified_wording("w2", specs, satisfied, fields)
        assert specs[0] == "Tradeline detail must be shown."
