"""Unit tests for ticket 4276 ("Predicted Conditions: Link the Right Copy of
Each Document").

Covers:
  - _doc_rank() now ranks by (signed, content_date, confidence, ts) instead
    of (signed, confidence, ts) -- "use the newer copy; same date -> higher
    Task Tile confidence."
  - Property-address scoping (_filter_to_subject_property /
    _is_subject_property_doc in tools/merger_tools.py): Hazard Insurance /
    Flood Hazard Determination / Rental Agreement only link documents for
    the SUBJECT property, not an other-properties folder.

Run with: python3 -m pytest tests/test_ticket_4276_duplicate_copy_selection.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_DIR))

from dotenv import load_dotenv
load_dotenv(REPO_DIR / ".env")

import json

from tools.shared.manifest_parser import (
    _annotate_bank_statement_coverage,
    _doc_rank,
    _merge_paystubs,
    parse_manifest_from_string,
)
from tools.merger_tools import _filter_to_subject_property, _find_all_submitted_docs


class TestDocRankDateBeforeConfidence:
    def test_newer_dated_copy_wins_over_older_regardless_of_confidence(self):
        """513: older of two payoff statements was linked -- the newer one
        (by its own content date) must win even if its confidence happens
        to be lower."""
        older = {"confidence": 0.95, "payoffDate": "2026-01-01"}
        newer = {"confidence": 0.60, "payoffDate": "2026-03-01"}
        assert _doc_rank(newer, ts=5) > _doc_rank(older, ts=10)

    def test_same_date_falls_back_to_higher_confidence(self):
        """512: same October bank statement uploaded twice -- the
        higher-confidence (10-page, 0.99) copy must win over the
        lower-confidence (4-page, 0.57) copy when both cover the same
        statement period."""
        low_conf = {"confidence": 0.57, "statementPeriodTo": "2026-10-31"}
        high_conf = {"confidence": 0.99, "statementPeriodTo": "2026-10-31"}
        assert _doc_rank(high_conf, ts=1) > _doc_rank(low_conf, ts=99)

    def test_no_date_on_either_side_falls_back_to_confidence_then_ts(self):
        """Doc types with no parseable content date behave exactly as
        before this ticket (PC-08's confidence-then-timestamp order)."""
        a = {"confidence": 0.80}
        b = {"confidence": 0.95}
        assert _doc_rank(b, ts=1) > _doc_rank(a, ts=99)

    def test_signed_always_wins_regardless_of_date_or_confidence(self):
        unsigned_newer_higher_conf = {
            "confidence": 0.99, "payoffDate": "2026-05-01",
        }
        signed_older_lower_conf = {
            "confidence": 0.10, "payoffDate": "2026-01-01", "signed": True,
        }
        assert _doc_rank(signed_older_lower_conf, ts=1) > _doc_rank(
            unsigned_newer_higher_conf, ts=100
        )


class TestSubjectPropertyScoping:
    def _sdoc(self, name, address):
        return {
            "name": name, "doc_type": "other",
            "extracted_fields": {"propertyAddress": address},
        }

    def test_other_property_hazard_policy_not_linked(self):
        """513: Hazard Insurance linked an expired policy from the
        other-properties folder while the subject property's own 2
        policies went unlinked."""
        docs = [
            self._sdoc("Hazard Insurance", "456 Other St, Springfield, IL"),
        ]
        scenario = {"property": {"address": "123 Main St, Springfield, IL"}}
        filtered = _filter_to_subject_property(docs, "hazard insurance", scenario)
        assert filtered == []

    def test_subject_property_hazard_policy_linked(self):
        docs = [
            self._sdoc("Hazard Insurance", "123 Main St, Springfield, IL"),
        ]
        scenario = {"property": {"address": "123 Main St, Springfield, IL"}}
        filtered = _filter_to_subject_property(docs, "hazard insurance", scenario)
        assert len(filtered) == 1

    def test_lease_for_other_property_not_linked_to_current_lease(self):
        docs = [self._sdoc("Lease Agreement", "789 Other Ave, Chicago, IL")]
        scenario = {"property": {"address": "123 Main St, Springfield, IL"}}
        filtered = _filter_to_subject_property(docs, "rental agreement", scenario)
        assert filtered == []

    def test_non_scoped_doc_type_unaffected(self):
        """Credit Report etc. are not property-scoped at all -- filter is a
        no-op for any canonical type outside _PROPERTY_SCOPED_TYPES."""
        docs = [self._sdoc("Credit Report", "456 Other St, Springfield, IL")]
        scenario = {"property": {"address": "123 Main St, Springfield, IL"}}
        filtered = _filter_to_subject_property(docs, "credit report", scenario)
        assert len(filtered) == 1

    def test_missing_address_data_defaults_to_include(self):
        """No subject address known at all -> never filter (avoid false
        negatives when we simply don't have the data to compare)."""
        docs = [self._sdoc("Hazard Insurance", "456 Other St")]
        scenario = {"property": {}}
        filtered = _filter_to_subject_property(docs, "hazard insurance", scenario)
        assert len(filtered) == 1

    def test_doc_with_no_address_field_defaults_to_include(self):
        docs = [{"name": "Hazard Insurance", "doc_type": "other", "extracted_fields": {}}]
        scenario = {"property": {"address": "123 Main St, Springfield, IL"}}
        filtered = _filter_to_subject_property(docs, "hazard insurance", scenario)
        assert len(filtered) == 1

    def test_all_matches_different_property_correctly_zeroes_out(self):
        """If the ONLY candidate(s) are clearly for a different property,
        that must zero out to "not in file" for the subject property --
        this is the exact 513 scenario, not an edge case to soften."""
        docs = [self._sdoc("Hazard Insurance", "999 Totally Different Rd, Nowhere, IL")]
        scenario = {"property": {"address": "123 Main St, Springfield, IL"}}
        filtered = _filter_to_subject_property(docs, "hazard insurance", scenario)
        assert filtered == []


def _manifest(documents: list[dict], tasks: list[dict]) -> str:
    return json.dumps({"job": {"tasks": tasks}, "documents": documents})


class TestAC1_LatestSigned1003Wins:
    """511: 2 of 7 copies of the 1003 were linked -- both unsigned -- while
    5 signed copies sat unused in the same file. This exercises the real
    end-to-end dedup pipeline (parse_manifest_from_string -> the
    (category_id, group_name, specific_type) bucketing -> _doc_rank), not
    just _doc_rank() in isolation, to confirm a signed copy wins even when
    unsigned copies were indexed MORE RECENTLY (the pre-PC-08 behavior that
    caused this exact bug: the latest-indexed copy won regardless of
    signed status)."""

    def test_signed_copy_wins_even_when_unsigned_copies_indexed_later(self):
        documents, tasks = [], []
        for doc_id, end_time in [("unsigned-1", 500), ("unsigned-2", 600)]:
            documents.append({
                "id": doc_id,
                "category": {"category_id": 349, "category_name": "URLA 1003"},
                "metadata": {"group_name": "1", "signed": False, "confidence": 0.9},
            })
            tasks.append({"document_id": doc_id, "type": "indexing", "end_time": end_time})
        for i in range(5):
            doc_id = f"signed-{i}"
            documents.append({
                "id": doc_id,
                "category": {"category_id": 349, "category_name": "URLA 1003"},
                "metadata": {"group_name": "1", "signed": True, "confidence": 0.9},
            })
            tasks.append({"document_id": doc_id, "type": "indexing", "end_time": 100 + i})

        result = parse_manifest_from_string(_manifest(documents, tasks))
        matches = [r for r in result if r["doc_id"] == "349"]
        assert len(matches) == 1, "exactly one surviving 1003 entry after dedup"
        assert matches[0]["document_id"].startswith("signed-"), (
            "the surviving entry must be one of the SIGNED copies, not an "
            "unsigned one, regardless of indexing order"
        )


class TestAC9_OneGovernmentIdPerBorrower:
    """512: two borrowers, two passports in the file, only one linked --
    both must survive dedup as two SEPARATE entries (one per borrower),
    not collapse into a single "latest" one, per PC-10's person-signature
    split (now also confirmed for Passport's "owner" field specifically,
    not just Drivers License)."""

    def test_two_borrowers_two_ids_both_survive_as_separate_entries(self):
        documents = [
            {
                "id": "passport-borrower-a",
                "category": {"category_id": 323, "category_name": "Passport"},
                "metadata": {"group_name": "1", "owner": "John Smith", "confidence": 0.95},
            },
            {
                "id": "passport-borrower-b",
                "category": {"category_id": 323, "category_name": "Passport"},
                "metadata": {"group_name": "1", "owner": "Jane Doe", "confidence": 0.95},
            },
        ]
        tasks = [
            {"document_id": "passport-borrower-a", "type": "indexing", "end_time": 100},
            {"document_id": "passport-borrower-b", "type": "indexing", "end_time": 200},
        ]
        result = parse_manifest_from_string(_manifest(documents, tasks))
        matches = [r for r in result if r["doc_id"] == "323"]
        assert len(matches) == 2, "both borrowers' IDs must survive, not collapse to one"
        ids = {r["document_id"] for r in matches}
        assert ids == {"passport-borrower-a", "passport-borrower-b"}

    def test_two_copies_of_the_same_borrowers_id_still_dedup_to_one(self):
        """Sanity check for the other half of PC-10/4276: TWO uploads of the
        SAME borrower's ID (e.g. a re-scan) must still collapse to one
        entry -- the fix only stops collapsing DIFFERENT borrowers, it must
        not start treating every re-upload as a distinct person."""
        documents = [
            {
                "id": "rescan-1",
                "category": {"category_id": 323, "category_name": "Passport"},
                "metadata": {"group_name": "1", "owner": "John Smith", "confidence": 0.70},
            },
            {
                "id": "rescan-2",
                "category": {"category_id": 323, "category_name": "Passport"},
                "metadata": {"group_name": "1", "owner": "John Smith", "confidence": 0.98},
            },
        ]
        tasks = [
            {"document_id": "rescan-1", "type": "indexing", "end_time": 100},
            {"document_id": "rescan-2", "type": "indexing", "end_time": 200},
        ]
        result = parse_manifest_from_string(_manifest(documents, tasks))
        matches = [r for r in result if r["doc_id"] == "323"]
        assert len(matches) == 1
        assert matches[0]["document_id"] == "rescan-2"


class TestAC5AC6_PaystubConsecutivenessAndGaps:
    """510/512: wage earner needs 2 consecutive paystubs covering 30 days
    -- both link and the requirement is satisfiable when truly consecutive
    (AC5); a real gap inside that window must keep the 30-day requirement
    open and name the missing dates, not be hidden by an overall span that
    merely looks wide enough (AC6)."""

    def _stub(self, doc_id: str, from_: str, to_: str):
        return {"document_id": doc_id, "extracted_fields": {
            "payStubPeriodFrom": from_, "payStubPeriodTo": to_, "current": 2000,
        }}

    def test_two_consecutive_paystubs_both_linked_no_gap(self):
        entries = [
            self._stub("a", "2026-01-01", "2026-01-15"),
            self._stub("b", "2026-01-16", "2026-01-31"),
        ]
        merged = _merge_paystubs(entries)
        assert len(merged) == 1
        assert merged[0]["document_ids"] == ["a", "b"]
        assert merged[0]["extracted_fields"]["has_coverage_gap"] is False
        assert merged[0]["extracted_fields"]["coverage_gaps"] == []

    def test_gap_inside_30_day_window_flagged_and_named(self):
        entries = [
            self._stub("a", "2026-01-01", "2026-01-15"),
            self._stub("b", "2026-02-10", "2026-02-24"),  # 25-day hole
        ]
        merged = _merge_paystubs(entries)
        assert len(merged) == 1
        fields = merged[0]["extracted_fields"]
        assert fields["has_coverage_gap"] is True
        assert fields["coverage_gaps"] == [{
            "gap_start": "2026-01-16", "gap_end": "2026-02-09", "gap_days": 25,
        }]
        # The gap must be visible in the entry's own display name too, not
        # just a buried field the satisfaction-grading step might miss.
        assert "COVERAGE GAP" in merged[0]["name"]
        assert "2026-01-16" in merged[0]["name"]

    def test_small_boundary_gap_not_flagged(self):
        """A payday landing a few days after the prior period's end date is
        normal pay-cycle noise, not a genuine missing-coverage gap."""
        entries = [
            self._stub("a", "2026-01-01", "2026-01-14"),
            self._stub("b", "2026-01-17", "2026-01-31"),  # 2-day gap
        ]
        merged = _merge_paystubs(entries)
        assert merged[0]["extracted_fields"]["has_coverage_gap"] is False


class TestAC3AC4_BankStatementCoverageAnnotation:
    """511/513: "Needs 12 months bank statements, all 12 in file -> all 12
    linked, requirement met" (AC3) / "one month missing -> requirement
    open, names the missing month" (AC4). Unlike paystubs, bank statements
    must stay as SEPARATE entries (AC3 says "all 12 linked", not one
    merged row) -- _annotate_bank_statement_coverage attaches a
    deterministic `_coverage_summary`/`_coverage_gaps` to EACH entry in
    place instead of merging them, grouped by account number so two
    different accounts' statements are never mixed into one timeline."""

    def _stmt(self, doc_id, from_, to_, acct="XX-XXXX-4127"):
        return {
            "document_id": doc_id,
            "extracted_fields": {
                "statementPeriodFrom": from_, "statementPeriodTo": to_,
                "accounts": [{"accountNumber": acct}],
            },
        }

    def test_contiguous_months_no_gap_all_entries_annotated(self):
        """Mirrors AC3: 3 consecutive monthly statements, all still
        present as separate entries (not merged), each annotated with a
        'no gaps' summary."""
        entries = [
            self._stmt("a", "2026-01-01", "2026-01-31"),
            self._stmt("b", "2026-02-01", "2026-02-28"),
            self._stmt("c", "2026-03-01", "2026-03-31"),
        ]
        _annotate_bank_statement_coverage(entries)
        assert len(entries) == 3  # never merged/removed
        for e in entries:
            assert e["extracted_fields"]["has_coverage_gap"] is False
            assert e["extracted_fields"]["_coverage_gaps"] == []
            assert "no gaps" in e["extracted_fields"]["_coverage_summary"]
            assert "3 statement(s)" in e["extracted_fields"]["_coverage_summary"]

    def test_missing_month_flagged_and_named(self):
        """Mirrors AC4: March is missing between Feb and Apr statements --
        the exact missing date range must be named, not just "incomplete"."""
        entries = [
            self._stmt("a", "2026-01-01", "2026-01-31"),
            self._stmt("b", "2026-02-01", "2026-02-28"),
            self._stmt("c", "2026-04-01", "2026-04-30"),  # March missing
        ]
        _annotate_bank_statement_coverage(entries)
        gapped = [e for e in entries if e["extracted_fields"]["has_coverage_gap"]]
        assert len(gapped) == 3  # all 3 entries in this account's bucket get it
        gaps = gapped[0]["extracted_fields"]["_coverage_gaps"]
        assert gaps == [{
            "gap_start": "2026-03-01", "gap_end": "2026-03-31", "gap_days": 31,
        }]
        assert "2026-03-01" in gapped[0]["extracted_fields"]["_coverage_summary"]
        assert "COVERAGE GAP" in gapped[0]["extracted_fields"]["_coverage_summary"]

    def test_two_different_accounts_not_mixed_into_one_timeline(self):
        """513/large-file scenario: a borrower with two bank accounts --
        each account's own coverage must be evaluated independently, not
        combined into one (possibly falsely gapped or falsely clean)
        timeline."""
        entries = [
            self._stmt("a", "2026-01-01", "2026-01-31", acct="1111"),
            self._stmt("b", "2026-02-01", "2026-02-28", acct="1111"),
            # Second account only has a Jan statement -- its own
            # single-entry bucket should NOT borrow account 1111's Feb
            # coverage, and should not be falsely flagged as gapped either
            # (nothing to compare against yet).
            self._stmt("c", "2026-01-01", "2026-01-31", acct="2222"),
        ]
        _annotate_bank_statement_coverage(entries)
        acct_1111 = [e for e in entries if e["document_id"] in ("a", "b")]
        acct_2222 = [e for e in entries if e["document_id"] == "c"]
        for e in acct_1111:
            assert e["extracted_fields"]["has_coverage_gap"] is False
            assert "2 statement(s)" in e["extracted_fields"]["_coverage_summary"]
        assert acct_2222[0]["extracted_fields"]["has_coverage_gap"] is False
        assert "1 statement(s)" in acct_2222[0]["extracted_fields"]["_coverage_summary"]

    def test_small_boundary_gap_not_flagged(self):
        """A statement cycle starting a few days after the prior one ended
        is normal billing-cycle noise, not a genuine missing-coverage gap
        -- same principle as paystubs' 3-day threshold, just a slightly
        wider one (5 days) since statement cycles are monthly."""
        entries = [
            self._stmt("a", "2026-01-01", "2026-01-28"),
            self._stmt("b", "2026-02-02", "2026-02-28"),  # 3-day gap
        ]
        _annotate_bank_statement_coverage(entries)
        assert entries[0]["extracted_fields"]["has_coverage_gap"] is False

    def test_undated_or_no_account_entries_left_untouched(self):
        """Entries with no parseable period, or no identifiable account
        number, must not crash the annotation pass and must simply not
        get a _coverage_summary -- fail safe, not loud."""
        entries = [
            {"document_id": "a", "extracted_fields": {}},
            self._stmt("b", "2026-01-01", "2026-01-31", acct=""),
        ]
        _annotate_bank_statement_coverage(entries)
        assert "_coverage_summary" not in entries[0]["extracted_fields"]
        # The no-account entry still gets grouped (into the "" bucket) and
        # annotated -- just labeled generically rather than by account.
        assert entries[1]["extracted_fields"]["has_coverage_gap"] is False
        assert "this account" in entries[1]["extracted_fields"]["_coverage_summary"]

    def test_end_to_end_through_parse_manifest_from_string_on_real_nyarko_data(self):
        """Confirms the real wiring in _parse_manifest_dict, not just the
        helper in isolation -- against compiled_inputs/nyarko's real,
        unmodified manifest (2 accounts, 3+2 statements, both genuinely
        contiguous)."""
        raw = (REPO_DIR / "compiled_inputs" / "nyarko" / "manifest.json").read_text()
        entries = parse_manifest_from_string(raw)
        bank_entries = [e for e in entries if e["doc_type"] == "bank_statement"]
        assert len(bank_entries) == 5  # all 5 raw statements still individually present
        for e in bank_entries:
            assert e["extracted_fields"]["has_coverage_gap"] is False
            assert "no gaps" in e["extracted_fields"]["_coverage_summary"]
