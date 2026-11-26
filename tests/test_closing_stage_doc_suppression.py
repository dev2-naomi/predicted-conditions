"""Unit tests for the closing-stage document suppression gate in
tools/doc_rules.py::apply_negative_gates (PC-01/PC-04 follow-up).

Background: the 17 Nov 2026 UAT accuracy report (PC-01/PC-04) found that
"Deed of Trust", "Grant Deed", and "Owner Occupancy Certification" were
being predicted on every loan, even though all three are closing-stage
documents that cannot exist in a pre-closing file. The initial fix
(2026-11-19) removed them from the unconditional document floor
(mandatory_docs()/conditional_docs()).

A live post-fix run against compiled_inputs/nyarko (2026-11-23) showed
that fix was INCOMPLETE: the per-module LLM steps (01-07) independently
re-proposed "Deed of Trust" and "Owner Occupancy Certification" anyway
(status "needed", zero document_ids), since the floor removal only
stopped the deterministic injection, not LLM-originated candidates.
This test file covers the follow-up fix: an unconditional
_CLOSING_STAGE_SUPPRESSED gate in apply_negative_gates that strips these
three document types regardless of which layer produced them.

Run with: python3 -m pytest tests/test_closing_stage_doc_suppression.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_DIR))

from dotenv import load_dotenv
load_dotenv(REPO_DIR / ".env")

from tools.doc_rules import apply_negative_gates, derive_flags

_CLOSING_STAGE_TYPES = [
    "Deed of Trust", "Grant Deed", "Owner Occupancy Certification",
]


def _doc(document_type: str) -> dict:
    return {
        "document_type": document_type,
        "document_category": "Property",
        "priority": "P2",
        "severity": "SOFT-STOP",
        "specifications": ["some spec"],
        "reasons_needed": ["some reason"],
    }


def _flags(**overrides) -> dict:
    """Minimal scenario_summary -> flags, with sane defaults a real loan
    would have (purchase, owner-occupied, full-doc, no REO/gifts/etc.)."""
    ss = {
        "purpose": "Purchase",
        "occupancy": "Owner-Occupied",
        "income_profile": {"income_types": ["W2"]},
        "asset_profile": {},
        "reo_summary": {"total_properties_owned": 0},
    }
    ss.update(overrides)
    return derive_flags(ss)


class TestClosingStageDocsAlwaysSuppressed:
    """Deed of Trust / Grant Deed / Owner Occupancy Certification must be
    dropped regardless of which layer (LLM-merged input) produced them,
    and regardless of loan scenario (purchase vs refi, DSCR vs full-doc,
    owner-occupied vs investment)."""

    def test_dropped_on_a_plain_purchase_full_doc_owner_occupied_loan(self):
        docs = [_doc(t) for t in _CLOSING_STAGE_TYPES] + [_doc("Credit Report")]
        flags = _flags()
        out, removed = apply_negative_gates(docs, flags)
        out_types = {d["document_type"] for d in out}
        assert out_types == {"Credit Report"}
        assert set(removed) == set(_CLOSING_STAGE_TYPES)

    def test_dropped_on_a_dscr_investment_purchase_loan(self):
        """The exact scenario the live nyarko run exercised: DSCR,
        investment/NOO, purchase."""
        docs = [_doc(t) for t in _CLOSING_STAGE_TYPES] + [_doc("Hazard Insurance")]
        flags = _flags(
            occupancy="Investment",
            income_profile={"income_types": ["DSCR"]},
        )
        out, removed = apply_negative_gates(docs, flags)
        out_types = {d["document_type"] for d in out}
        assert out_types == {"Hazard Insurance"}
        assert set(removed) == set(_CLOSING_STAGE_TYPES)

    def test_dropped_on_a_refinance_loan(self):
        docs = [_doc(t) for t in _CLOSING_STAGE_TYPES]
        flags = _flags(purpose="Cash-OutRefinance")
        out, removed = apply_negative_gates(docs, flags)
        assert out == []
        assert set(removed) == set(_CLOSING_STAGE_TYPES)

    def test_case_insensitive_match(self):
        docs = [_doc("deed of trust"), _doc("GRANT DEED"), _doc("Owner occupancy certification")]
        flags = _flags()
        out, removed = apply_negative_gates(docs, flags)
        assert out == []
        assert len(removed) == 3


class TestUnrelatedDocsUnaffected:
    def test_other_title_docs_survive(self):
        docs = [_doc("Title Commitment"), _doc("Preliminary Title Report")]
        flags = _flags()
        out, removed = apply_negative_gates(docs, flags)
        assert {d["document_type"] for d in out} == {
            "Title Commitment", "Preliminary Title Report",
        }
        assert removed == []
