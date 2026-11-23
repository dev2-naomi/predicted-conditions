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

from tools.shared.manifest_parser import _doc_rank
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
