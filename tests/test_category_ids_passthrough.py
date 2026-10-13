"""Unit tests for the `category_ids` field on document_requests.

Behavior under test (2026-10-13 feature):
  - `data/doctype_masterlist.json` / the manifest's upstream classifier use
    a shared `category_id` numbering space (e.g. "117" = Credit Report,
    "323" = Drivers License). That id should now show up on every
    document_request's new `category_ids` field, merged from two sources:

    1. The manifest documents that ended up satisfying the request
       (document_ids -> document_inventory's `doc_id`).
    2. `required_documents_json`'s per-item `category_ids` array, when the
       request was floor-injected from that checklist
       (tools.doc_rules.required_documents_floor).

  - `tools.shared.normalize.normalize_document_structure` must not silently
    drop this field (it is now a _CANONICAL_FIELDS entry).
  - `tools.merger_tools.generate_final_output` is responsible for the
    final merge/dedup pass across both sources.

Run with: python3 -m pytest tests/test_category_ids_passthrough.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_DIR))

import pytest

from tools.doc_rules import _doc, apply_deterministic_rules, required_documents_floor
from tools.shared.normalize import normalize_document_structure
from tools.merger_tools import generate_final_output
from registry import GENERAL_TOOL_NAMES, STEP_CONFIG
from step_loader import resolve_tools_for_step


class TestParseRequiredDocumentsIsReachableByTheOrchestrator:
    """Regression test for a bug found during live dev verification: the
    parse_required_documents tool existed in tools/scenario_tools.py, was
    imported into tools.ALL_TOOLS, and was listed in agent.py's default
    initial prompt's STEP_00 sequence -- but was NEVER added to
    config/workflow_config.json / registry.py's STEP_00 tool list. Since
    step_loader.resolve_tools_for_step() only binds tools present in the
    current step's registry entry (or GENERAL_TOOL_NAMES), the orchestrator
    LLM could never actually call it, regardless of prompt wording -- a
    live run confirmed required_documents_json was provided but
    scenario_summary._required_documents_data stayed None because the tool
    was silently absent from the model's available-tools list every turn.

    Fixed by adding it to config/workflow_config.json's STEP_00 substeps
    and registry.py's generated STEP_CONFIG (hand-applied to registry.py
    rather than via config/generate.py, since workflow_config.json is
    currently stale in unrelated ways -- missing STEP_09 and
    inspect_raw_xml_data entirely -- and a full regen would have silently
    regressed those).
    """

    def test_parse_required_documents_in_step_00_registry_tools(self):
        assert "parse_required_documents" in STEP_CONFIG["STEP_00"]["tools"]

    def test_parse_required_documents_bound_by_resolve_tools_for_step(self):
        tools = resolve_tools_for_step({"current_step": "STEP_00"})
        names = {t.name for t in tools}
        assert "parse_required_documents" in names

    def test_other_step_00_tools_and_general_tools_still_present(self):
        """Guard against a careless future edit (e.g. a full
        config/generate.py regen against the still-stale workflow_config.json)
        silently dropping unrelated tools again."""
        step_00_tools = set(STEP_CONFIG["STEP_00"]["tools"])
        for expected in (
            "parse_loan_file", "parse_manifest_documents",
            "parse_eligibility_output", "load_doctype_masterlist",
            "build_scenario_summary", "detect_contradictions", "route_to_facets",
        ):
            assert expected in step_00_tools
        assert "STEP_09" in STEP_CONFIG
        assert "inspect_raw_xml_data" in GENERAL_TOOL_NAMES


class TestRequiredDocumentsFloorPassesThroughCategoryIds:
    def test_category_ids_carried_onto_floor_doc(self):
        ss = {
            "_required_documents_data": {
                "program_name": "Flex Supreme",
                "documents": [
                    {
                        "label": "Drivers License",
                        "category": "identification",
                        "category_ids": [323, 349],
                        "status": "pending",
                    },
                ],
            }
        }
        docs = required_documents_floor(ss)
        assert len(docs) == 1
        assert docs[0]["category_ids"] == ["323", "349"]

    def test_missing_category_ids_does_not_crash_and_is_absent(self):
        ss = {
            "_required_documents_data": {
                "program_name": "Flex Supreme",
                "documents": [
                    {"label": "Some Item", "category": "other", "status": "completed"},
                ],
            }
        }
        docs = required_documents_floor(ss)
        assert len(docs) == 1
        # _doc() only sets the key when category_ids is truthy.
        assert docs[0].get("category_ids", []) == []

    def test_doc_helper_stringifies_and_drops_nones(self):
        d = _doc(
            "X", "Cross-Cutting", "P1", "HARD-STOP", [], [],
            category_ids=[117, None, "349"],
        )
        assert d["category_ids"] == ["117", "349"]


class TestApplyDeterministicRulesMergesCategoryIdsIntoExistingDoc:
    """Regression test for a bug found during live dev verification: when a
    required_documents_json checklist item's document_type canon-matches a
    doc the LLM already drafted (e.g. "Bank Statement"), apply_deterministic_rules's
    _inject(bypass_negative_gates=True) merge path only merged
    reasons_needed into the existing entry and silently dropped the
    checklist item's category_ids. Fixed via _merge_category_ids.
    """

    _BASE_SS = {
        "occupancy": "Primary",
        "purpose": "Purchase",
        "program": "Flex Supreme",
        "property": {"property_type": "SFR"},
        "reo_summary": {"total_properties_owned": 0},
        "asset_profile": {},
        "income_profile": {"income_doc_label": "Bank Statement"},
    }

    def test_checklist_category_ids_merge_into_preexisting_llm_doc(self):
        llm_docs = [
            {
                "document_type": "Bank Statement",
                "document_category": "Income",
                "priority": "P1",
                "severity": "HARD-STOP",
                "specifications": ["12 months of statements"],
                "reasons_needed": ["Bank-statement income qualification"],
            },
        ]
        ss = dict(self._BASE_SS, _required_documents_data={
            "program_name": "Test",
            "documents": [
                {
                    "label": "Bank Statements",
                    "category": "bank_statement",
                    "category_ids": [169, 170],
                    "status": "completed",
                },
            ],
        })
        docs, _stats = apply_deterministic_rules(llm_docs, ss)
        bank_stmt = next(d for d in docs if d["document_type"] == "Bank Statement")
        assert bank_stmt["category_ids"] == ["169", "170"]
        # reasons_needed merge must still work alongside the category_ids merge
        assert "Bank-statement income qualification" in bank_stmt["reasons_needed"]
        assert any("Submission Requirements checklist" in r for r in bank_stmt["reasons_needed"])

    def test_checklist_category_ids_dedup_against_existing(self):
        llm_docs = [
            {
                "document_type": "Bank Statement",
                "document_category": "Income",
                "priority": "P1",
                "severity": "HARD-STOP",
                "specifications": [],
                "reasons_needed": [],
                "category_ids": ["169"],
            },
        ]
        ss = dict(self._BASE_SS, _required_documents_data={
            "program_name": "Test",
            "documents": [
                {
                    "label": "Bank Statements",
                    "category": "bank_statement",
                    "category_ids": [169, 170],
                    "status": "completed",
                },
            ],
        })
        docs, _stats = apply_deterministic_rules(llm_docs, ss)
        bank_stmt = next(d for d in docs if d["document_type"] == "Bank Statement")
        assert bank_stmt["category_ids"] == ["169", "170"]


class TestNormalizePreservesCategoryIds:
    def test_normalize_keeps_existing_category_ids(self):
        dr = {"document_type": "Drivers License", "category_ids": ["323", "349"]}
        out = normalize_document_structure(dr)
        assert out["category_ids"] == ["323", "349"]

    def test_normalize_defaults_to_empty_list(self):
        out = normalize_document_structure({"document_type": "X"})
        assert out["category_ids"] == []

    def test_normalize_coerces_string_to_list(self):
        out = normalize_document_structure({"document_type": "X", "category_ids": "117"})
        assert out["category_ids"] == ["117"]


class TestGenerateFinalOutputMergesCategoryIds:
    """Exercises the real STEP_08 merge point against a fake state, the
    same shape agent.py would pass in (document_inventory + module_outputs).
    """

    def _invoke(self, state: dict) -> list[dict]:
        cmd = generate_final_output.func(state=state)
        return cmd.update["final_output"]["document_requests"]

    def test_category_id_derived_from_satisfying_document(self):
        state = {
            "document_inventory": [
                {"doc_id": "117", "document_ids": ["uuid-credit-1"]},
            ],
            "module_outputs": {
                "08": {
                    "ranked_document_requests": [
                        {
                            "document_type": "Credit Report",
                            "document_category": "Credit",
                            "document_ids": ["uuid-credit-1"],
                        },
                    ]
                }
            },
            "scenario_summary": {},
        }
        drs = self._invoke(state)
        assert drs[0]["category_ids"] == ["117"]

    def test_checklist_category_ids_survive_with_no_matching_document(self):
        state = {
            "document_inventory": [],
            "module_outputs": {
                "08": {
                    "ranked_document_requests": [
                        {
                            "document_type": "Drivers License (checklist)",
                            "document_category": "Cross-Cutting",
                            "document_ids": [],
                            "category_ids": ["323", "349"],
                        },
                    ]
                }
            },
            "scenario_summary": {},
        }
        drs = self._invoke(state)
        assert drs[0]["category_ids"] == ["323", "349"]

    def test_doc_extraction_and_checklist_ids_are_merged_and_deduped(self):
        state = {
            "document_inventory": [
                {"doc_id": "323", "document_ids": ["uuid-license-1", "uuid-license-2"]},
            ],
            "module_outputs": {
                "08": {
                    "ranked_document_requests": [
                        {
                            "document_type": "Government-Issued Photo ID",
                            "document_category": "Cross-Cutting",
                            "document_ids": ["uuid-license-1", "uuid-license-2"],
                            # Same id as the manifest-derived one -- must dedup,
                            # not duplicate.
                            "category_ids": ["323"],
                        },
                    ]
                }
            },
            "scenario_summary": {},
        }
        drs = self._invoke(state)
        assert drs[0]["category_ids"] == ["323"]

    def test_no_matching_document_and_no_checklist_ids_yields_empty_list(self):
        """Even the name-lookup fallback should yield nothing for a document
        type that genuinely has no masterlist entry."""
        state = {
            "document_inventory": [],
            "module_outputs": {
                "08": {
                    "ranked_document_requests": [
                        {
                            "document_type": "No Match Doc",
                            "document_category": "Other",
                            "document_ids": [],
                        },
                    ]
                }
            },
            "scenario_summary": {},
        }
        drs = self._invoke(state)
        assert drs[0]["category_ids"] == []


class TestGenerateFinalOutputFallsBackToMasterlistNameLookup:
    """2026-10-13 follow-up: sources (a) doc-extraction and (b) checklist
    both require some upstream signal (a satisfying uploaded document, or
    a borrower checklist entry). Deterministic/LLM-drafted document_requests
    with neither still deserve a category_id if the masterlist has one for
    their exact (canonicalized) document_type name -- this is the 3rd,
    last-resort source, confirmed against real dev data to resolve ~89%
    of the requests that sources (a)/(b) left empty.
    """

    def _invoke(self, state: dict) -> list[dict]:
        cmd = generate_final_output.func(state=state)
        return cmd.update["final_output"]["document_requests"]

    def test_falls_back_to_name_lookup_when_no_document_or_checklist_ids(self):
        state = {
            "document_inventory": [],
            "module_outputs": {
                "08": {
                    "ranked_document_requests": [
                        {
                            "document_type": "Appraisal Report",
                            "document_category": "Property",
                            "document_ids": [],
                        },
                    ]
                }
            },
            "scenario_summary": {},
        }
        drs = self._invoke(state)
        assert drs[0]["category_ids"] == ["162"]

    def test_name_lookup_goes_through_canonical_alias_resolution(self):
        """'Flood Certificate' has no direct masterlist entry under that
        exact name, but merger_tools._canonical_doc_type() already maps it
        to 'flood hazard determination', which does."""
        state = {
            "document_inventory": [],
            "module_outputs": {
                "08": {
                    "ranked_document_requests": [
                        {
                            "document_type": "Flood Certificate",
                            "document_category": "Property",
                            "document_ids": [],
                        },
                    ]
                }
            },
            "scenario_summary": {},
        }
        drs = self._invoke(state)
        assert drs[0]["category_ids"] == ["1800"]

    def test_name_lookup_only_runs_when_both_other_sources_are_empty(self):
        """Doc-extraction/checklist ids (even a single one) must win outright
        -- the name-lookup fallback must never run or append on top."""
        state = {
            "document_inventory": [
                {"doc_id": "999", "document_ids": ["uuid-1"]},
            ],
            "module_outputs": {
                "08": {
                    "ranked_document_requests": [
                        {
                            "document_type": "Appraisal Report",
                            "document_category": "Property",
                            "document_ids": ["uuid-1"],
                        },
                    ]
                }
            },
            "scenario_summary": {},
        }
        drs = self._invoke(state)
        # Must be exactly the doc-extraction id (999), NOT also/instead 162.
        assert drs[0]["category_ids"] == ["999"]

    def test_umbrella_doc_type_resolves_to_all_acceptable_form_ids(self):
        """'Government-Issued Photo ID' canonicalizes to 'government id',
        which has no single masterlist row of its own (it's an umbrella for
        several distinct real documents). Rather than leaving category_ids
        empty, resolve every acceptable-form name variant known via
        _get_aliases()/_DOCTYPE_ALIASES against the masterlist and return
        the full set: Drivers License (323) and Passport (845). A real
        uploaded ID document still resolves its own single id via source
        (a) when one has actually been matched."""
        state = {
            "document_inventory": [],
            "module_outputs": {
                "08": {
                    "ranked_document_requests": [
                        {
                            "document_type": "Government-Issued Photo ID",
                            "document_category": "Cross-Cutting",
                            "document_ids": [],
                        },
                    ]
                }
            },
            "scenario_summary": {},
        }
        drs = self._invoke(state)
        assert drs[0]["category_ids"] == ["323", "845"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
