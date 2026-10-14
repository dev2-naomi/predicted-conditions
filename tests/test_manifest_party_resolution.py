"""Unit tests for the manifest-derived party-name fallback in
tools.coborrower.resolve_parties() / _manifest_borrower_names().

Behavior under test (2026-10-14 fix):
  - resolve_parties() previously only sourced borrower identity from
    scenario_summary.borrowers, eligibility_json, and a raw-XML-string
    fallback that's dead in practice (state["loan_file_xml"] has already
    been overwritten with the parsed profile dict by the time
    apply_coborrower_pass runs post-pipeline).
  - Loans whose loan_file_xml uses the flat-JSON shape never carry borrower
    names at all (by design -- see tools.shared.xml_parser's
    _flat_loan_json_to_profile), so scenario_summary.borrowers ends up
    empty. If eligibility_json also isn't supplied, resolve_parties()
    returned zero parties entirely -- even a single primary -- causing
    apply_coborrower_pass to fall into its single-party branch and force
    every borrower-specific document_request to "party": "borrower", with
    no distinction for a real co-borrower on the loan (confirmed live on a
    real production run: two separate checklist items tagged
    "borrower": "primary" / "borrower": "co" collapsed into one merged
    "Asset" document_request, satisfied only by the primary's own
    documents).
  - The manifest documents (URLA 1003's new1003Borrowers array, the
    Prequal Response Form's borrowers array) almost always carry the full,
    cleanly-structured borrower list regardless of loan_file_xml format or
    eligibility_json presence. _manifest_borrower_names() now reads these
    as a fallback source (checked last, after the more explicit sources),
    so resolve_parties() correctly finds both parties on loans like this.

Run with: python3 -m pytest tests/test_manifest_party_resolution.py -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_DIR))

from tools.coborrower import _manifest_borrower_names, resolve_parties


def _manifest(documents: list[dict]) -> str:
    return json.dumps({"documents": documents})


class TestManifestBorrowerNamesExtraction:
    def test_extracts_both_names_from_new1003borrowers(self):
        manifest = _manifest([
            {
                "id": "doc-1",
                "metadata": {
                    "new1003Borrowers": [
                        {"name": {"firstName": "Michael", "lastName": "Kelly"}},
                        {"name": {"firstName": "Meryl", "lastName": "Goldberg"}},
                    ]
                },
            }
        ])
        names = _manifest_borrower_names({"manifest_json": manifest})
        assert names == ["Michael Kelly", "Meryl Goldberg"]

    def test_extracts_both_names_from_borrowers_key(self):
        manifest = _manifest([
            {
                "id": "doc-2",
                "metadata": {
                    "borrowers": [
                        {"firstName": "Grant", "lastName": "Long"},
                        {"firstName": "Megan", "lastName": "Long"},
                    ]
                },
            }
        ])
        names = _manifest_borrower_names({"manifest_json": manifest})
        assert names == ["Grant Long", "Megan Long"]

    def test_blank_placeholder_names_are_skipped(self):
        # Observed on real data: some documents (e.g. a signature-only page)
        # carry a "borrowers" array with "[BLANK]" placeholders instead of
        # real names. Those entries must not produce a bogus party.
        manifest = _manifest([
            {
                "id": "doc-blank",
                "metadata": {
                    "borrowers": [
                        {"firstName": "[BLANK]", "lastName": "[BLANK]"},
                        {"firstName": "[BLANK]", "lastName": "[BLANK]"},
                    ]
                },
            },
            {
                "id": "doc-real",
                "metadata": {
                    "borrowers": [
                        {"firstName": "Michael", "lastName": "Kelly"},
                        {"firstName": "Meryl", "lastName": "Goldberg"},
                    ]
                },
            },
        ])
        names = _manifest_borrower_names({"manifest_json": manifest})
        assert names == ["Michael Kelly", "Meryl Goldberg"]

    def test_single_borrower_returns_one_name(self):
        manifest = _manifest([
            {
                "id": "doc-1",
                "metadata": {
                    "new1003Borrowers": [
                        {"name": {"firstName": "Jane", "lastName": "Doe"}},
                    ]
                },
            }
        ])
        names = _manifest_borrower_names({"manifest_json": manifest})
        assert names == ["Jane Doe"]

    def test_no_matching_keys_returns_empty_list(self):
        manifest = _manifest([{"id": "doc-1", "metadata": {"group_name": "Assets"}}])
        assert _manifest_borrower_names({"manifest_json": manifest}) == []

    def test_manifest_json_missing_returns_empty_list(self):
        assert _manifest_borrower_names({}) == []

    def test_manifest_json_as_dict_not_string_also_works(self):
        manifest_dict = {
            "documents": [
                {
                    "id": "doc-1",
                    "metadata": {
                        "borrowers": [
                            {"firstName": "Michael", "lastName": "Kelly"},
                            {"firstName": "Meryl", "lastName": "Goldberg"},
                        ]
                    },
                }
            ]
        }
        names = _manifest_borrower_names({"manifest_json": manifest_dict})
        assert names == ["Michael Kelly", "Meryl Goldberg"]

    def test_malformed_manifest_json_degrades_gracefully(self):
        assert _manifest_borrower_names({"manifest_json": "not json{{{"}) == []


class TestResolvePartiesFallsBackToManifest:
    def test_two_parties_resolved_purely_from_manifest(self):
        """The exact real-world gap this fix closes: flat-JSON
        loan_file_xml (no names), no eligibility_json -- the manifest is
        the only source with identity data."""
        state = {
            "scenario_summary": {"borrowers": []},
            "loan_file_xml": {"some": "already-parsed dict, not a string"},
            "manifest_json": _manifest([
                {
                    "id": "doc-1",
                    "metadata": {
                        "new1003Borrowers": [
                            {"name": {"firstName": "Michael", "lastName": "Kelly"}},
                            {"name": {"firstName": "Meryl", "lastName": "Goldberg"}},
                        ]
                    },
                }
            ]),
        }
        parties = resolve_parties(state)
        assert parties == [
            {"name": "Michael Kelly", "role": "primary", "tag": "borrower"},
            {"name": "Meryl Goldberg", "role": "co-borrower", "tag": "coborrower"},
        ]

    def test_more_explicit_sources_still_take_priority_over_manifest(self):
        """When scenario_summary.borrowers already has real names, the
        manifest (even if present with different names) must not override
        them."""
        state = {
            "scenario_summary": {
                "borrowers": [
                    {"name": "Explicit Primary", "role": "primary"},
                    {"name": "Explicit Co", "role": "co-borrower"},
                ]
            },
            "manifest_json": _manifest([
                {
                    "id": "doc-1",
                    "metadata": {
                        "borrowers": [
                            {"firstName": "Manifest", "lastName": "Primary"},
                            {"firstName": "Manifest", "lastName": "Co"},
                        ]
                    },
                }
            ]),
        }
        parties = resolve_parties(state)
        assert parties[0]["name"] == "Explicit Primary"
        assert parties[1]["name"] == "Explicit Co"

    def test_manifest_fills_in_only_the_missing_half(self):
        """scenario_summary already has a primary but no co-borrower; the
        manifest should fill in just the co-borrower, not override the
        already-known primary."""
        state = {
            "scenario_summary": {
                "borrowers": [{"name": "Known Primary", "role": "primary"}]
            },
            "manifest_json": _manifest([
                {
                    "id": "doc-1",
                    "metadata": {
                        "borrowers": [
                            {"firstName": "Manifest", "lastName": "Primary"},
                            {"firstName": "Manifest", "lastName": "Co"},
                        ]
                    },
                }
            ]),
        }
        parties = resolve_parties(state)
        assert parties[0]["name"] == "Known Primary"
        assert parties[1]["name"] == "Manifest Co"

    def test_single_party_loan_with_no_coborrower_in_manifest(self):
        state = {
            "scenario_summary": {"borrowers": []},
            "manifest_json": _manifest([
                {
                    "id": "doc-1",
                    "metadata": {
                        "new1003Borrowers": [
                            {"name": {"firstName": "Solo", "lastName": "Borrower"}},
                        ]
                    },
                }
            ]),
        }
        parties = resolve_parties(state)
        assert parties == [
            {"name": "Solo Borrower", "role": "primary", "tag": "borrower"}
        ]

    def test_no_identity_data_anywhere_still_returns_empty(self):
        state = {"scenario_summary": {"borrowers": []}}
        assert resolve_parties(state) == []

    def test_manifest_same_name_for_both_does_not_create_a_duplicate_party(self):
        # Guards the existing _norm_name(co) != _norm_name(primary) check
        # still applies when both names come from the manifest fallback.
        state = {
            "scenario_summary": {"borrowers": []},
            "manifest_json": _manifest([
                {
                    "id": "doc-1",
                    "metadata": {
                        "new1003Borrowers": [
                            {"name": {"firstName": "Same", "lastName": "Person"}},
                            {"name": {"firstName": "Same", "lastName": "Person"}},
                        ]
                    },
                }
            ]),
        }
        parties = resolve_parties(state)
        assert len(parties) == 1
        assert parties[0]["name"] == "Same Person"
