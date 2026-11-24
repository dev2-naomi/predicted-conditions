"""Unit tests for ticket 4274 ("Predicted Conditions: Don't Ask for
Documents the Loan Can't Need") -- one test class per explicit acceptance
criterion, each run against the REAL mandatory_docs()/conditional_docs()/
apply_negative_gates() pipeline (not a synthetic doc list standing in for
it), so these prove the actual floor-injection + gate behavior end to end,
the same quality bar set by tickets 4275-4278's test suites.

Source report: 17 Nov 2026 UAT (loans 90000108510/511/512) + 18 Nov 2026
loan 90000108513. 28 of the 4 loans' predicted conditions had no document
linked; 23 of those were for documents the loan didn't need.

Run with: python3 -m pytest tests/test_ticket_4274_predicted_conditions_scope.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_DIR))

from dotenv import load_dotenv
load_dotenv(REPO_DIR / ".env")

from tools.doc_rules import (
    apply_negative_gates,
    conditional_docs,
    derive_flags,
    derive_income_docs,
    mandatory_docs,
)


def _floor_and_conditional(flags: dict, ss: dict | None = None) -> list[dict]:
    """The real Layer 1 + Layer 2 + Layer 3 output (mandatory_docs +
    conditional_docs + derive_income_docs), exactly as
    apply_deterministic_rules assembles it before gating."""
    docs = mandatory_docs() + conditional_docs(flags)
    if ss is not None:
        docs += derive_income_docs(ss)
    return docs


def _types(docs: list[dict]) -> set[str]:
    return {d["document_type"] for d in docs}


def _ss(**overrides) -> dict:
    """Minimal scenario_summary, with sane defaults a real loan would have
    (purchase, owner-occupied, full-doc wage income, no REO/gifts/large-
    deposits, no submitted docs)."""
    ss = {
        "purpose": "Purchase",
        "occupancy": "Owner-Occupied",
        "income_profile": {"income_doc_label": "Full Doc"},
        "borrower_type": "Wage Earner",
        "asset_profile": {},
        "reo_summary": {"total_properties_owned": 0},
        "_submitted_docs": [],
    }
    ss.update(overrides)
    return ss


def _flags(**overrides) -> dict:
    """derive_flags() straight from _ss(**overrides) -- use _ss() directly
    (not this) for tests that also need the full Layer 3 income docs."""
    return derive_flags(_ss(**overrides))


def _submitted(name: str, doc_type: str = "", extracted_fields: dict | None = None) -> dict:
    return {
        "name": name,
        "doc_type": doc_type or name,
        "extracted_fields": extracted_fields or {},
    }


# ---------------------------------------------------------------------------
# AC1: Purchase loan before closing -> Deed of Trust and Grant Deed not in
# the list.
# ---------------------------------------------------------------------------

class TestAC1_DeedOfTrustAndGrantDeedNotPreClosing:
    def test_neither_is_in_the_floor_or_conditional_layer_on_a_purchase(self):
        """mandatory_docs()/conditional_docs() stopped flooring these two
        entirely (the 19 Nov fix) -- confirms that removal is still in
        place and hasn't regressed back."""
        flags = _flags(purpose="Purchase")
        docs = _floor_and_conditional(flags)
        assert "Deed of Trust" not in _types(docs)
        assert "Grant Deed" not in _types(docs)

    def test_llm_reproposed_copies_are_gated_out_regardless(self):
        """The real post-fix live-run finding (2026-11-23, nyarko): the
        per-module LLM steps independently re-propose these two from
        their own guideline reasoning even after the floor stopped
        injecting them -- apply_negative_gates must still catch an
        LLM-originated proposal, not just rely on the floor's absence."""
        flags = _flags(purpose="Purchase")
        docs = _floor_and_conditional(flags) + [
            {"document_type": "Deed of Trust", "specifications": ["x"],
             "reasons_needed": ["y"], "severity": "SOFT-STOP", "priority": "P2"},
            {"document_type": "Grant Deed", "specifications": ["x"],
             "reasons_needed": ["y"], "severity": "SOFT-STOP", "priority": "P2"},
        ]
        out, removed = apply_negative_gates(docs, flags)
        out_types = _types(out)
        assert "Deed of Trust" not in out_types
        assert "Grant Deed" not in out_types
        assert "Deed of Trust" in removed
        assert "Grant Deed" in removed

    def test_refinance_loan_has_neither_either(self):
        flags = _flags(purpose="Cash-OutRefinance")
        docs = _floor_and_conditional(flags)
        out, removed = apply_negative_gates(docs, flags)
        assert "Deed of Trust" not in _types(out)
        assert "Grant Deed" not in _types(out)


# ---------------------------------------------------------------------------
# AC2: Investment property loan -> Owner Occupancy Certification not in
# the list.
# ---------------------------------------------------------------------------

def _with_llm_proposal(docs: list[dict], *doc_types: str) -> list[dict]:
    """Append a simulated LLM-reproposed entry for each doc_type -- see
    TestAC1's "llm reproposed copies" test for why this matters: the
    floor/conditional layers already stopped injecting these types, so
    the only way to exercise apply_negative_gates' actual gate (rather
    than just an absence that was never tested) is to simulate the real
    post-fix failure mode the 17/23 Nov live runs found."""
    extra = [
        {"document_type": dt, "specifications": ["x"], "reasons_needed": ["y"],
         "severity": "SOFT-STOP", "priority": "P2"}
        for dt in doc_types
    ]
    return docs + extra


class TestAC2_OwnerOccupancyCertificationNotOnInvestment:
    def test_not_in_the_floor_or_conditional_layer_on_investment(self):
        flags = _flags(occupancy="Investment")
        docs = _floor_and_conditional(flags)
        assert "Owner Occupancy Certification" not in _types(docs)

    def test_llm_reproposed_copy_is_gated_out_on_investment(self):
        flags = _flags(occupancy="Investment")
        docs = _with_llm_proposal(
            _floor_and_conditional(flags), "Owner Occupancy Certification",
        )
        out, removed = apply_negative_gates(docs, flags)
        assert "Owner Occupancy Certification" not in _types(out)
        assert "Owner Occupancy Certification" in removed

    def test_never_appears_alongside_borrower_certification_business_purpose(self):
        """The ticket's own example: loan 513 had Owner Occupancy
        Certification predicted TOGETHER with Borrower Certification as
        to Business Purpose (the opposite document) -- on a real
        investment/NOO loan, the latter legitimately IS predicted (see
        conditional_docs' is_noo block), so this confirms the former is
        never also present alongside it even when an LLM step
        re-proposes it."""
        flags = _flags(occupancy="Investment")
        docs = _with_llm_proposal(
            _floor_and_conditional(flags), "Owner Occupancy Certification",
        )
        out, _ = apply_negative_gates(docs, flags)
        out_types = _types(out)
        assert "Borrower Certification as to Business Purpose" in out_types
        assert "Owner Occupancy Certification" not in out_types

    def test_owner_occupied_loan_also_gates_out_an_llm_reproposed_copy(self):
        """Owner Occupancy Certification is executed at closing (see
        _CLOSING_STAGE_SUPPRESSED's module comment) regardless of
        occupancy -- confirms the owner-occupied case doesn't regress
        back to predicting it either."""
        flags = _flags(occupancy="Owner-Occupied")
        docs = _with_llm_proposal(
            _floor_and_conditional(flags), "Owner Occupancy Certification",
        )
        out, removed = apply_negative_gates(docs, flags)
        assert "Owner Occupancy Certification" not in _types(out)


# ---------------------------------------------------------------------------
# AC3: DSCR loan -> Paystub, W-2, IRS 4506-C not in the list.
# ---------------------------------------------------------------------------

class TestAC3_DSCRLoanNoWageIncomeDocs:
    def test_dscr_loan_full_pipeline_has_none_of_the_three(self):
        ss = _ss(
            occupancy="Investment",
            income_profile={"income_doc_label": "DSCR"},
            program="Investor DSCR",
        )
        flags = derive_flags(ss)
        assert flags["is_dscr"] is True
        docs = _floor_and_conditional(flags, ss)
        # IRS 4506-C Authorization is floored unconditionally; Paystub/W2
        # come from the income-doc layer, which correctly derives NEITHER
        # for a DSCR label (see _income_docs_for_entry's early "dscr"
        # return) -- confirms the absence is real, not just untested, by
        # simulating an LLM-reproposed copy of all three before gating.
        docs = _with_llm_proposal(docs, "Paystub", "W2")
        out, removed = apply_negative_gates(docs, flags)
        out_types = _types(out)
        assert "Paystub" not in out_types
        assert "W2" not in out_types
        assert "IRS 4506-C Authorization" not in out_types
        for expected in ("Paystub", "W2", "IRS 4506-C Authorization"):
            assert expected in removed

    def test_dscr_loan_never_gets_the_w2_1099_tax_transcripts_doc_either(self):
        """has_wage_or_1099_income must be False on DSCR (ticket 4278's
        'W-2 or 1099 Tax Transcripts' doc is gated on this flag, a
        DIFFERENT document_type string than 'W2' -- _DSCR_SUPPRESSED
        alone would not catch it if this flag were ever wrong)."""
        ss = _ss(
            occupancy="Investment",
            income_profile={"income_doc_label": "DSCR"},
            program="Investor DSCR",
        )
        flags = derive_flags(ss)
        assert flags["has_wage_or_1099_income"] is False
        docs = _floor_and_conditional(flags, ss)
        assert "W-2 or 1099 Tax Transcripts" not in _types(docs)


# ---------------------------------------------------------------------------
# AC4: DSCR purchase with appraisal rent schedule -> Current Lease
# Agreement and Rental Income Calculations Worksheet not in the list.
# ---------------------------------------------------------------------------

class TestAC4_RentScheduleReplacesLeaseOnDSCRPurchase:
    def test_rent_schedule_present_suppresses_both(self):
        flags = _flags(
            purpose="Purchase",
            occupancy="Investment",
            income_profile={"income_types": ["DSCR"]},
            program="Investor DSCR",
            _submitted_docs=[
                _submitted(
                    "Appraisal Report", "Appraisal Report",
                    {"marketRent": 2400},
                ),
            ],
        )
        assert flags["has_appraisal_rent_schedule"] is True
        docs = _floor_and_conditional(flags)
        # Confirm the DSCR injection really did add these two first, so
        # the test actually exercises suppression rather than their
        # simply never having been added.
        assert "Rental Agreement" in _types(docs)
        assert "Rental Income Calculations Worksheet" in _types(docs)
        out, removed = apply_negative_gates(docs, flags)
        out_types = _types(out)
        assert "Rental Agreement" not in out_types
        assert "Rental Income Calculations Worksheet" not in out_types
        assert "Rental Agreement" in removed
        assert "Rental Income Calculations Worksheet" in removed

    def test_free_text_1007_mention_also_detected(self):
        flags = _flags(
            purpose="Purchase",
            occupancy="Investment",
            income_profile={"income_types": ["DSCR"]},
            program="Investor DSCR",
            _submitted_docs=[
                _submitted(
                    "Appraisal Report", "Appraisal Report",
                    {"appraiserComments": "See Form 1007 rent schedule attached"},
                ),
            ],
        )
        assert flags["has_appraisal_rent_schedule"] is True

    def test_no_rent_schedule_evidence_does_not_suppress(self):
        """Negative control: without any appraisal rent-schedule
        evidence, both docs stay (we don't over-suppress on absence of
        data -- same principle as every other best-effort flag in this
        module)."""
        flags = _flags(
            purpose="Purchase",
            occupancy="Investment",
            income_profile={"income_types": ["DSCR"]},
            program="Investor DSCR",
            _submitted_docs=[],
        )
        assert flags["has_appraisal_rent_schedule"] is False
        docs = _floor_and_conditional(flags)
        out, _ = apply_negative_gates(docs, flags)
        out_types = _types(out)
        assert "Rental Agreement" in out_types
        assert "Rental Income Calculations Worksheet" in out_types

    def test_refinance_with_rent_schedule_is_not_suppressed(self):
        """The ticket specifically says "on a purchase" -- a refinance
        with an existing tenant already in place still needs its own
        lease evidence even if the appraisal also carries a 1007."""
        flags = _flags(
            purpose="Cash-OutRefinance",
            occupancy="Investment",
            income_profile={"income_types": ["DSCR"]},
            program="Investor DSCR",
            _submitted_docs=[
                _submitted("Appraisal Report", "Appraisal Report", {"marketRent": 2400}),
            ],
        )
        docs = _floor_and_conditional(flags)
        out, _ = apply_negative_gates(docs, flags)
        assert "Rental Agreement" in _types(out)


# ---------------------------------------------------------------------------
# AC5: Loan with bank statements in file -> Verification of Deposit not in
# the list. (Pre-existing PC-03 fix; re-confirmed here against this
# ticket's own explicit acceptance criterion and full pipeline.)
# ---------------------------------------------------------------------------

class TestAC5_VODSuppressedWhenBankStatementsOnFile:
    def test_bank_statement_on_file_suppresses_vod(self):
        flags = _flags(_submitted_docs=[_submitted("Bank Statement")])
        docs = _floor_and_conditional(flags)
        assert "Verification of Deposit" in _types(docs)
        out, removed = apply_negative_gates(docs, flags)
        assert "Verification of Deposit" not in _types(out)
        assert "Verification of Deposit" in removed

    def test_no_bank_statement_keeps_vod_open(self):
        flags = _flags(_submitted_docs=[])
        docs = _floor_and_conditional(flags)
        out, _ = apply_negative_gates(docs, flags)
        assert "Verification of Deposit" in _types(out)


# ---------------------------------------------------------------------------
# AC6: Loan with mortgage statements or a rent-free letter -> Verification
# of Rent not in the list.
# ---------------------------------------------------------------------------

class TestAC6_VORSuppressedByMortgageStatementOrRentFreeLetter:
    def test_mortgage_statements_show_history_suppresses_vor(self):
        """loan 511's own finding."""
        flags = _flags(
            occupancy="Investment",
            _submitted_docs=[_submitted("Mortgage Statement")],
        )
        docs = _floor_and_conditional(flags)
        assert "Verification of Rent" in _types(docs)
        out, removed = apply_negative_gates(docs, flags)
        assert "Verification of Rent" not in _types(out)
        assert "Verification of Rent" in removed

    def test_rent_free_letter_in_file_suppresses_vor(self):
        """loan 513's own finding."""
        flags = _flags(
            occupancy="Investment",
            _submitted_docs=[_submitted("Rent Free Letter")],
        )
        docs = _floor_and_conditional(flags)
        out, removed = apply_negative_gates(docs, flags)
        assert "Verification of Rent" not in _types(out)
        assert "Verification of Rent" in removed

    def test_neither_present_keeps_vor_open(self):
        flags = _flags(occupancy="Investment", _submitted_docs=[])
        docs = _floor_and_conditional(flags)
        out, _ = apply_negative_gates(docs, flags)
        assert "Verification of Rent" in _types(out)


# ---------------------------------------------------------------------------
# AC7: Two-borrower, large file -> no condition name appears twice.
# ---------------------------------------------------------------------------

class TestAC7_NoConditionNameAppearsTwice:
    def test_collapse_helper_merges_same_canonical_type_different_context(self):
        from tools.merger_tools import _collapse_duplicate_canonical_types

        docs = [
            {
                "document_type": "Paystub",
                "document_context": {"borrower": "Jane Doe"},
                "specifications": ["Most recent 30 days"],
                "reasons_needed": ["Full doc income verification"],
                "severity": "SOFT-STOP",
                "priority": "P2",
            },
            {
                # Same real-world requirement, but no document_context at
                # all this time -- the exact inconsistency that let a
                # duplicate slip through the context-aware merge key.
                "document_type": "paystub",
                "specifications": ["Matches employer on application"],
                "reasons_needed": ["Full doc income verification"],
                "severity": "HARD-STOP",
                "priority": "P1",
            },
            {
                "document_type": "Credit Report",
                "specifications": ["Tri-merge"],
                "reasons_needed": ["Required for all loans"],
                "severity": "HARD-STOP",
                "priority": "P1",
            },
        ]
        out = _collapse_duplicate_canonical_types(docs)
        types = [d["document_type"] for d in out]
        assert types.count("Paystub") + types.count("paystub") == 1
        assert len(out) == 2
        paystub = next(d for d in out if d["document_type"].lower() == "paystub")
        # Merged content survives from both duplicates, not just the first.
        assert "Most recent 30 days" in paystub["specifications"]
        assert "Matches employer on application" in paystub["specifications"]
        # Higher severity/priority of the two duplicates wins.
        assert paystub["severity"] == "HARD-STOP"
        assert paystub["priority"] == "P1"

    def test_three_way_duplicate_collapses_to_one(self):
        from tools.merger_tools import _collapse_duplicate_canonical_types

        docs = [
            {"document_type": "Title Invoice", "specifications": ["a"],
             "reasons_needed": ["r1"], "severity": "SOFT-STOP", "priority": "P2"},
            {"document_type": "Title Invoice", "specifications": ["b"],
             "reasons_needed": ["r2"], "severity": "SOFT-STOP", "priority": "P2"},
            {"document_type": "Title Invoice", "specifications": ["c"],
             "reasons_needed": ["r3"], "severity": "SOFT-STOP", "priority": "P2"},
        ]
        out = _collapse_duplicate_canonical_types(docs)
        assert len(out) == 1
        assert set(out[0]["specifications"]) == {"a", "b", "c"}

    def test_merge_document_requests_tool_produces_no_duplicate_names(self):
        """End-to-end through the real merge_document_requests tool: feed
        the same document_type from two different module_outputs entries,
        one with document_context and one without -- confirms the final
        merged list the real pipeline produces never repeats a canonical
        document_type."""
        from tools.merger_tools import merge_document_requests

        state = {
            "module_outputs": {
                "01": {
                    "document_requests": [
                        {
                            "document_type": "Government-Issued Photo ID",
                            "document_context": {"borrower": "Jane Doe"},
                            "specifications": ["Unexpired"],
                            "reasons_needed": ["Identity verification"],
                            "severity": "HARD-STOP",
                            "priority": "P1",
                        },
                    ],
                },
                "04": {
                    "document_requests": [
                        {
                            # Same requirement, no document_context -- as
                            # module 04 (Credit) might independently
                            # reason about identity verification without
                            # tagging a borrower context the way module
                            # 01 (Cross-Cutting) did.
                            "document_type": "Government-Issued Photo ID",
                            "specifications": ["Matches credit report name"],
                            "reasons_needed": ["Fraud prevention"],
                            "severity": "SOFT-STOP",
                            "priority": "P2",
                        },
                    ],
                },
            },
            "scenario_summary": {
                "purpose": "Purchase",
                "occupancy": "Owner-Occupied",
                "income_profile": {"income_types": ["W2"]},
            },
        }
        cmd = merge_document_requests.func(tool_call_id="t1", state=state)
        merged = cmd.update["module_outputs"]["08"]["merged_document_requests"]
        names = [d["document_type"] for d in merged]
        assert len(names) == len(set(n.lower() for n in names)), (
            f"duplicate document_type name(s) in merged output: {names}"
        )


# ---------------------------------------------------------------------------
# AC8: Wage-earner loan -> Paystub and IRS 4506-C still predicted
# (negative control -- confirms AC3's DSCR gate doesn't over-fire).
# ---------------------------------------------------------------------------

class TestAC8_WageEarnerStillGetsPaystubAnd4506C:
    def test_full_doc_wage_loan_keeps_both(self):
        ss = _ss(
            occupancy="Owner-Occupied",
            income_profile={"income_doc_label": "Full Doc"},
            borrower_type="Wage Earner",
        )
        flags = derive_flags(ss)
        assert flags["is_dscr"] is False
        docs = _floor_and_conditional(flags, ss)
        assert "Paystub" in _types(docs)  # confirms the income layer fired
        out, removed = apply_negative_gates(docs, flags)
        out_types = _types(out)
        assert "Paystub" in out_types
        assert "IRS 4506-C Authorization" in out_types
        assert "Paystub" not in removed
        assert "IRS 4506-C Authorization" not in removed


# ---------------------------------------------------------------------------
# "What's asked" (not a numbered AC, but explicit in the ticket text):
# Title Invoice -- confirm whether it's needed before closing; if not,
# don't predict it. Implemented as a conditional (evidence-gated), not
# unconditional, suppression -- see _has_title_invoice_evidence's
# docstring in tools/doc_rules.py for why, and the explicit caveat that
# this is a proposal pending guideline confirmation.
# ---------------------------------------------------------------------------

class TestTitleInvoiceGatedOnEvidence:
    def test_no_evidence_suppresses_the_unconditional_floor_ask(self):
        flags = _flags(_submitted_docs=[])
        docs = _floor_and_conditional(flags)
        assert "Title Invoice" in _types(docs)
        out, removed = apply_negative_gates(docs, flags)
        assert "Title Invoice" not in _types(out)
        assert "Title Invoice" in removed

    def test_real_title_invoice_on_file_is_kept(self):
        flags = _flags(_submitted_docs=[_submitted("Title Invoice")])
        docs = _floor_and_conditional(flags)
        out, removed = apply_negative_gates(docs, flags)
        assert "Title Invoice" in _types(out)
        assert "Title Invoice" not in removed

    def test_title_fee_sheet_alias_also_counts_as_evidence(self):
        flags = _flags(_submitted_docs=[_submitted("Title Fee Sheet")])
        docs = _floor_and_conditional(flags)
        out, _ = apply_negative_gates(docs, flags)
        assert "Title Invoice" in _types(out)

    def test_eligibility_required_docs_bypass_path_is_not_exempt(self):
        """The real bug this ticket reported: eligibility_required_docs()
        injects "Title Invoice" via apply_deterministic_rules' own
        bypass_negative_gates=True path (so authoritative eligibility-
        engine-required docs like Bank Statement/Profit and Loss aren't
        wrongly swept by broad heuristic gates), which used to skip
        apply_negative_gates ENTIRELY -- meaning the evidence gate above
        never actually fired on a real loan whose eligibility JSON flags
        "Title Invoice" as a Minimum Required Document category (exactly
        what every one of the 4 UAT loans' eligibility JSON does,
        confirmed in data/input/*/eligibility_output.json). Live-confirmed
        as a real regression via compiled_inputs/nyarko on the first
        deploy of this ticket's fix before this test/fix existed."""
        from tools.doc_rules import apply_deterministic_rules

        ss = _ss(
            _submitted_docs=[],
            _eligibility_data={"required_doc_categories": ["title invoice"]},
        )
        final_docs, stats = apply_deterministic_rules(
            [], ss, canonical_fn=lambda n: n.strip().lower(),
        )
        assert "Title Invoice" not in {d["document_type"] for d in final_docs}

    def test_eligibility_required_docs_bypass_path_still_keeps_it_with_evidence(self):
        from tools.doc_rules import apply_deterministic_rules

        ss = _ss(
            _submitted_docs=[_submitted("Title Invoice")],
            _eligibility_data={"required_doc_categories": ["title invoice"]},
        )
        final_docs, stats = apply_deterministic_rules(
            [], ss, canonical_fn=lambda n: n.strip().lower(),
        )
        assert "Title Invoice" in {d["document_type"] for d in final_docs}

    def test_other_eligibility_required_docs_still_bypass_normally(self):
        """Negative control: the carve-out is specific to "title invoice"
        -- Profit and Loss (a bank-statement-only-gate-suppressed type on
        a DSCR loan in this test) must still survive via the bypass path,
        confirming the fix didn't disable bypass_negative_gates in
        general."""
        from tools.doc_rules import apply_deterministic_rules

        ss = _ss(
            occupancy="Investment",
            income_profile={"income_doc_label": "DSCR"},
            program="Investor DSCR",
            _submitted_docs=[],
            _eligibility_data={"required_doc_categories": ["profit and loss"]},
        )
        final_docs, stats = apply_deterministic_rules(
            [], ss, canonical_fn=lambda n: n.strip().lower(),
        )
        assert "Profit and Loss" in {d["document_type"] for d in final_docs}
