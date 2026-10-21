"""Tests for the deterministic per-document confidence score.

Covers tools.merger_tools.compute_document_confidence / assign_confidence_scores,
and that the field survives tools.shared.normalize.normalize_document_structure's
field allow-list (the single choke point both generate_final_output and
coborrower.build_party_document_requests route through before returning
document_requests).
"""

from __future__ import annotations

from tools.merger_tools import (
    _CONFIDENCE_BASE_BY_SOURCE,
    _CONFIDENCE_BASE_DEFAULT,
    _CONFIDENCE_BASE_UNKNOWN,
    _CONFIDENCE_DOCUMENT_MATCH_BONUS,
    assign_confidence_scores,
    compute_document_confidence,
)
from tools.shared.normalize import normalize_document_structure


def _dr(**overrides) -> dict:
    base = {
        "document_type": "Appraisal Report",
        "source_module": "deterministic",
        "specifications": [],
        "satisfied_specifications": [],
        "document_ids": [],
    }
    base.update(overrides)
    return base


class TestComputeDocumentConfidenceBaseScores:
    def test_deterministic_source_gets_highest_base(self):
        score = compute_document_confidence(_dr(source_module="deterministic"))
        assert score == _CONFIDENCE_BASE_BY_SOURCE["deterministic"]

    def test_crosscutting_module_01_gets_its_own_base(self):
        score = compute_document_confidence(_dr(source_module="01"))
        assert score == _CONFIDENCE_BASE_BY_SOURCE["01"]

    def test_other_numeric_modules_get_default_base(self):
        for mod in ("02", "03", "04", "05", "06", "07"):
            score = compute_document_confidence(_dr(source_module=mod))
            assert score == _CONFIDENCE_BASE_DEFAULT, f"module {mod} should use default base"

    def test_missing_source_module_gets_conservative_fallback(self):
        score = compute_document_confidence(_dr(source_module=""))
        assert score == _CONFIDENCE_BASE_UNKNOWN

    def test_none_source_module_gets_conservative_fallback(self):
        dr = _dr()
        dr.pop("source_module")
        score = compute_document_confidence(dr)
        assert score == _CONFIDENCE_BASE_UNKNOWN


class TestComputeDocumentConfidenceEvidenceAdjustment:
    def test_no_specs_evaluated_leaves_score_at_base(self):
        dr = _dr(source_module="02", specifications=[], satisfied_specifications=[])
        score = compute_document_confidence(dr)
        assert score == _CONFIDENCE_BASE_DEFAULT

    def test_all_specs_satisfied_moves_score_up_from_base(self):
        base_only = compute_document_confidence(
            _dr(source_module="02", specifications=["spec a"], satisfied_specifications=[])
        )
        all_satisfied = compute_document_confidence(
            _dr(
                source_module="02",
                specifications=[],
                satisfied_specifications=[{"specification": "spec a", "reason": "ok"}],
            )
        )
        assert all_satisfied > base_only

    def test_all_specs_outstanding_with_zero_satisfied_stays_at_base(self):
        # Zero satisfied specs is the normal "borrower hasn't submitted this
        # yet" state for most "needed" documents -- it must NOT be read as
        # low confidence in the requirement itself, so the score should
        # simply stay at the source-based base, not drop below it.
        all_outstanding = compute_document_confidence(
            _dr(source_module="02", specifications=["a", "b"], satisfied_specifications=[])
        )
        assert all_outstanding == _CONFIDENCE_BASE_DEFAULT

    def test_mixed_satisfaction_lands_between_base_and_fully_satisfied(self):
        mixed = compute_document_confidence(
            _dr(
                source_module="02",
                specifications=["a"],
                satisfied_specifications=[{"specification": "b", "reason": "ok"}],
            )
        )
        all_satisfied = compute_document_confidence(
            _dr(
                source_module="02",
                specifications=[],
                satisfied_specifications=[
                    {"specification": "a", "reason": "ok"},
                    {"specification": "b", "reason": "ok"},
                ],
            )
        )
        assert _CONFIDENCE_BASE_DEFAULT < mixed < all_satisfied


class TestComputeDocumentConfidenceDocumentMatchBonus:
    def test_matched_document_ids_adds_bonus(self):
        # Use a lower-base source_module so the +bonus doesn't get clipped by
        # the 0.99 ceiling (that clamping behavior is covered separately
        # below) and the raw delta is actually observable.
        without_match = compute_document_confidence(_dr(source_module="02", document_ids=[]))
        with_match = compute_document_confidence(_dr(source_module="02", document_ids=["uuid-1"]))
        assert round(with_match - without_match, 2) == _CONFIDENCE_DOCUMENT_MATCH_BONUS

    def test_bonus_still_clamped_at_ceiling(self):
        # deterministic base (0.95) + bonus (0.05) would be 1.00 uncapped;
        # must clamp below 1.0 per the "never exactly 1" contract.
        score = compute_document_confidence(
            _dr(source_module="deterministic", document_ids=["uuid-1"])
        )
        assert score <= 0.99


class TestComputeDocumentConfidenceClamping:
    def test_score_never_exceeds_ceiling(self):
        score = compute_document_confidence(
            _dr(
                source_module="deterministic",
                specifications=[],
                satisfied_specifications=[{"specification": "a", "reason": "ok"}],
                document_ids=["uuid-1"],
            )
        )
        assert score <= 0.99

    def test_score_never_below_floor(self):
        score = compute_document_confidence(
            _dr(source_module="", specifications=["a", "b", "c"], satisfied_specifications=[])
        )
        assert score >= 0.05

    def test_score_is_rounded_to_two_decimals(self):
        score = compute_document_confidence(_dr(source_module="02"))
        assert score == round(score, 2)


class TestAssignConfidenceScores:
    def test_sets_confidence_on_every_document_request(self):
        docs = [_dr(source_module="deterministic"), _dr(source_module="01"), _dr(source_module="05")]
        assign_confidence_scores(docs)
        assert all("confidence" in dr for dr in docs)
        assert all(isinstance(dr["confidence"], float) for dr in docs)

    def test_mutates_in_place_and_returns_none(self):
        docs = [_dr()]
        result = assign_confidence_scores(docs)
        assert result is None
        assert docs[0]["confidence"] == _CONFIDENCE_BASE_BY_SOURCE["deterministic"]

    def test_recomputing_after_satisfaction_changes_updates_score(self):
        # Simulates coborrower.build_party_document_requests re-scoring a
        # document against a different (per-party) submitted document set
        # after the whole-manifest pass already set an initial score.
        dr = _dr(source_module="02", specifications=["a"], satisfied_specifications=[])
        assign_confidence_scores([dr])
        first_score = dr["confidence"]

        dr["specifications"] = []
        dr["satisfied_specifications"] = [{"specification": "a", "reason": "ok"}]
        assign_confidence_scores([dr])
        second_score = dr["confidence"]

        assert second_score > first_score


class TestConfidenceSurvivesNormalization:
    def test_confidence_field_survives_normalize_document_structure(self):
        dr = _dr(source_module="deterministic")
        assign_confidence_scores([dr])
        stamped_score = dr["confidence"]

        normalized = normalize_document_structure(dr)

        assert "confidence" in normalized
        assert normalized["confidence"] == stamped_score

    def test_missing_confidence_falls_back_to_conservative_default(self):
        dr = _dr()
        dr.pop("confidence", None)  # never assigned (e.g. a bug upstream)

        normalized = normalize_document_structure(dr)

        assert normalized["confidence"] == 0.6
