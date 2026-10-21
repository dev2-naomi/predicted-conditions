"""Tests for the deterministic per-document confidence score.

Covers tools.merger_tools.compute_document_confidence / assign_confidence_scores,
and that the field survives tools.shared.normalize.normalize_document_structure's
field allow-list (the single choke point both generate_final_output and
coborrower.build_party_document_requests route through before returning
document_requests).
"""

from __future__ import annotations

import pytest

from tools.merger_tools import (
    _CONFIDENCE_BASE_BY_SOURCE,
    _CONFIDENCE_BASE_LLM_GROUNDED_MAX,
    _CONFIDENCE_BASE_LLM_GROUNDED_MIN,
    _CONFIDENCE_BASE_LLM_UNGROUNDED,
    _CONFIDENCE_BASE_UNKNOWN,
    _CONFIDENCE_CORROBORATION_BONUS,
    _CONFIDENCE_DOCUMENT_MATCH_BONUS,
    _GROUNDING_SPEC_COUNT_SATURATION,
    _grounded_llm_base,
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

    def test_other_numeric_modules_ungrounded_get_ungrounded_base(self):
        for mod in ("02", "03", "04", "05", "06", "07"):
            score = compute_document_confidence(_dr(source_module=mod))
            assert score == _CONFIDENCE_BASE_LLM_UNGROUNDED, f"module {mod} should use ungrounded base"

    def test_missing_source_module_gets_conservative_fallback(self):
        score = compute_document_confidence(_dr(source_module=""))
        assert score == _CONFIDENCE_BASE_UNKNOWN

    def test_none_source_module_gets_conservative_fallback(self):
        dr = _dr()
        dr.pop("source_module")
        score = compute_document_confidence(dr)
        assert score == _CONFIDENCE_BASE_UNKNOWN


class TestGroundedLLMBaseGrading:
    """_grounded_llm_base grades a guideline-grounded document's base score
    within [MIN, MAX] using thoroughness (resolved spec count) and
    specificity (fraction of those specs gated by a real, scenario-specific
    condition) rather than treating "grounded" as one more flat constant."""

    def test_thin_ungated_entry_lands_near_the_floor(self):
        # One resolved spec, no specific condition -- minimal thoroughness
        # and zero specificity.
        score = _grounded_llm_base(
            _dr(_guideline_grounded=True, _guideline_spec_count=1, _guideline_specificity_ratio=0.0)
        )
        assert score == pytest.approx(_CONFIDENCE_BASE_LLM_GROUNDED_MIN, abs=0.02)

    def test_thorough_fully_specific_entry_lands_near_the_ceiling(self):
        # Spec count at/above the saturation point, every resolved spec
        # gated by a real scenario-specific condition.
        score = _grounded_llm_base(
            _dr(
                _guideline_grounded=True,
                _guideline_spec_count=_GROUNDING_SPEC_COUNT_SATURATION * 2,
                _guideline_specificity_ratio=1.0,
            )
        )
        assert score == pytest.approx(_CONFIDENCE_BASE_LLM_GROUNDED_MAX, abs=0.001)

    def test_more_resolved_specs_increases_the_base(self):
        thin = _grounded_llm_base(
            _dr(_guideline_grounded=True, _guideline_spec_count=1, _guideline_specificity_ratio=0.0)
        )
        thorough = _grounded_llm_base(
            _dr(_guideline_grounded=True, _guideline_spec_count=8, _guideline_specificity_ratio=0.0)
        )
        assert thorough > thin

    def test_higher_specificity_ratio_increases_the_base(self):
        generic = _grounded_llm_base(
            _dr(_guideline_grounded=True, _guideline_spec_count=5, _guideline_specificity_ratio=0.0)
        )
        tailored = _grounded_llm_base(
            _dr(_guideline_grounded=True, _guideline_spec_count=5, _guideline_specificity_ratio=1.0)
        )
        assert tailored > generic

    def test_missing_grading_fields_falls_back_to_grounded_midpoint(self):
        dr = _dr(_guideline_grounded=True)  # no _guideline_spec_count/_guideline_specificity_ratio
        score = _grounded_llm_base(dr)
        midpoint = (_CONFIDENCE_BASE_LLM_GROUNDED_MIN + _CONFIDENCE_BASE_LLM_GROUNDED_MAX) / 2
        assert score == pytest.approx(midpoint)

    def test_score_never_exceeds_the_grounded_ceiling_even_with_extreme_inputs(self):
        score = _grounded_llm_base(
            _dr(_guideline_grounded=True, _guideline_spec_count=1000, _guideline_specificity_ratio=1.0)
        )
        assert score <= _CONFIDENCE_BASE_LLM_GROUNDED_MAX


class TestComputeDocumentConfidenceLLMTierSignals:
    """The two structural signals that give LLM-authored (source_module
    "02"-"07") requests a dynamic base instead of one flat constant."""

    def test_guideline_grounded_specs_get_higher_base_than_ungrounded(self):
        ungrounded = compute_document_confidence(_dr(source_module="02"))
        grounded = compute_document_confidence(
            _dr(
                source_module="02",
                _guideline_grounded=True,
                _guideline_spec_count=5,
                _guideline_specificity_ratio=0.5,
            )
        )
        assert ungrounded == _CONFIDENCE_BASE_LLM_UNGROUNDED
        assert _CONFIDENCE_BASE_LLM_GROUNDED_MIN <= grounded <= _CONFIDENCE_BASE_LLM_GROUNDED_MAX
        assert grounded > ungrounded

    def test_corroboration_by_two_plus_modules_adds_bonus(self):
        uncorroborated = compute_document_confidence(
            _dr(source_module="02", _corroborating_modules=["02"])
        )
        corroborated = compute_document_confidence(
            _dr(source_module="02", _corroborating_modules=["02", "05"])
        )
        assert round(corroborated - uncorroborated, 2) == _CONFIDENCE_CORROBORATION_BONUS

    def test_corroboration_by_single_module_gets_no_bonus(self):
        score = compute_document_confidence(
            _dr(source_module="02", _corroborating_modules=["02"])
        )
        assert score == _CONFIDENCE_BASE_LLM_UNGROUNDED

    def test_missing_corroborating_modules_field_gets_no_bonus(self):
        # merge_document_requests always sets this, but compute_document_confidence
        # must not blow up if it's ever absent (e.g. a hand-built test dict).
        dr = _dr(source_module="02")
        dr.pop("_corroborating_modules", None)
        score = compute_document_confidence(dr)
        assert score == _CONFIDENCE_BASE_LLM_UNGROUNDED

    def test_grounding_and_corroboration_stack(self):
        dr = _dr(
            source_module="02",
            _guideline_grounded=True,
            _guideline_spec_count=5,
            _guideline_specificity_ratio=0.5,
            _corroborating_modules=["02", "04"],
        )
        score = compute_document_confidence(dr)
        expected_grounded_base = _grounded_llm_base(dr)
        assert score == round(expected_grounded_base + _CONFIDENCE_CORROBORATION_BONUS, 2)

    def test_deterministic_and_01_sources_ignore_grounding_and_corroboration(self):
        # Fixed-base sources' risk profile doesn't depend on these LLM-tier
        # signals -- a "deterministic" or "01" request already has low
        # interpretive risk regardless of how many modules also proposed it.
        plain = compute_document_confidence(_dr(source_module="deterministic"))
        with_signals = compute_document_confidence(
            _dr(
                source_module="deterministic",
                _guideline_grounded=False,
                _corroborating_modules=["deterministic", "02", "05"],
            )
        )
        assert plain == with_signals == _CONFIDENCE_BASE_BY_SOURCE["deterministic"]


class TestComputeDocumentConfidenceEvidenceAdjustment:
    def test_no_specs_evaluated_leaves_score_at_base(self):
        dr = _dr(source_module="02", specifications=[], satisfied_specifications=[])
        score = compute_document_confidence(dr)
        assert score == _CONFIDENCE_BASE_LLM_UNGROUNDED

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
        assert all_outstanding == _CONFIDENCE_BASE_LLM_UNGROUNDED

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
        assert _CONFIDENCE_BASE_LLM_UNGROUNDED < mixed < all_satisfied


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


class TestMergeDocumentRequestsStampsConfidenceSignals:
    """Integration-level checks that merge_document_requests and
    doc_rules.apply_guideline_canonicalization actually stamp the two
    internal signals compute_document_confidence reads, end-to-end through
    the real STEP_08 merge tool (not just unit-tested in isolation above)."""

    def _run_merge(self, module_outputs: dict) -> list[dict]:
        from tools.merger_tools import merge_document_requests

        cmd = merge_document_requests.func(
            tool_call_id="call-1",
            state={"module_outputs": module_outputs, "scenario_summary": {}},
        )
        return cmd.update["module_outputs"]["08"]["merged_document_requests"]

    def test_two_modules_proposing_same_document_get_corroboration_stamped(self):
        merged = self._run_merge({
            "02": {"document_requests": [{"document_type": "Bank Statement", "specifications": []}]},
            "04": {"document_requests": [{"document_type": "Bank Statement", "specifications": []}]},
        })
        bank_stmt = next(dr for dr in merged if dr["document_type"].lower() == "bank statement")
        assert sorted(bank_stmt["_corroborating_modules"]) == ["02", "04"]

    def test_single_module_proposal_has_one_corroborating_module(self):
        merged = self._run_merge({
            "02": {"document_requests": [{"document_type": "Bank Statement", "specifications": []}]},
        })
        bank_stmt = next(dr for dr in merged if dr["document_type"].lower() == "bank statement")
        assert bank_stmt["_corroborating_modules"] == ["02"]

    def test_canonical_doc_spec_covered_type_gets_guideline_grounded_stamped(self):
        # "Appraisal Report" has an entry in data/canonical_doc_specs.json,
        # so apply_guideline_canonicalization (run inside merge_document_requests)
        # should replace its specifications and stamp _guideline_grounded
        # plus the two grading signals compute_document_confidence uses.
        merged = self._run_merge({
            "05": {"document_requests": [{"document_type": "Appraisal Report", "specifications": ["placeholder"]}]},
        })
        appraisal = next(dr for dr in merged if dr["document_type"].lower() == "appraisal report")
        assert appraisal.get("_guideline_grounded") is True
        # The LLM's placeholder spec should have been wholesale-replaced by
        # the real guideline-sourced list, not just appended to.
        assert "placeholder" not in appraisal["specifications"]
        assert len(appraisal["specifications"]) > 0
        # Grading signals: spec count should match the resolved specs list,
        # and specificity ratio should be a valid fraction.
        assert appraisal["_guideline_spec_count"] == len(appraisal["specifications"])
        assert 0.0 <= appraisal["_guideline_specificity_ratio"] <= 1.0


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
