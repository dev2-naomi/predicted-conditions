from __future__ import annotations

"""
SBIQ AI Predictive Conditions Orchestrator

Coordinates all 9 modules (00–08) to generate a consolidated, AUS-style set
of predictive underwriting conditions for a Non-QM loan.

Usage:
    from tools.orchestrator import run_predictive_conditions

    result = run_predictive_conditions(
        xml_path="data/input/xml/rothschild 3.xml",
        extracted_entities=None,        # or dict with documents + overlays
        guidelines_path=None,           # defaults to data/guidelines.md
    )

The result is a JSON-serializable dict matching the final output schema
defined in the orchestrator prompt.
"""

import json
from typing import Any, Optional

from tools.t00_scenario_builder import run_scenario_builder
from tools.t01_crosscutting import run_crosscutting
from tools.t02_income import run_income_conditions
from tools.t03_assets import run_assets_conditions
from tools.t04_credit import run_credit_conditions
from tools.t05_property_appraisal import run_property_appraisal_conditions
from tools.t06_title_closing import run_title_closing_conditions
from tools.t07_compliance import run_compliance_conditions
from tools.t08_merger_ranker import run_merger_ranker


# ═══════════════════════════════════════════════════════════════════════════
# Main Orchestrator
# ═══════════════════════════════════════════════════════════════════════════


def run_predictive_conditions(
    xml_path: str,
    extracted_entities: Optional[dict] = None,
    guidelines_path: Optional[str] = None,
) -> dict[str, Any]:
    """
    Run the full predictive conditions pipeline.

    Args:
        xml_path: Path to MISMO XML or FNM loan file.
        extracted_entities: Optional dict with 'documents' and 'overlays'
                           from the document processing pipeline.
        guidelines_path: Optional custom path to guidelines.md.

    Returns:
        Final consolidated JSON output with scenario_summary, conditions,
        seen_conflicts, and stats.
    """
    entities = extracted_entities or {"documents": [], "overlays": []}

    # ══════════════════════════════════════════════════════════════════
    # STEP 1 — Module 00: Scenario Builder
    # ══════════════════════════════════════════════════════════════════
    scenario_output = run_scenario_builder(
        xml_path=xml_path,
        extracted_entities=entities,
        guidelines_path=guidelines_path,
    )

    scenario_summary = scenario_output["scenario_summary"]
    missing_core_variables = scenario_output["missing_core_variables"]
    contradictions_detected = scenario_output["contradictions_detected"]
    docs_by_facet = scenario_output["docs_by_facet"]
    overlays_by_facet = scenario_output["overlays_by_facet"]
    guideline_section_refs = scenario_output["guideline_section_refs"]

    # Resolve overlay dicts from IDs for each facet
    all_overlays = entities.get("overlays", [])
    overlay_lookup = {ov.get("overlay_id"): ov for ov in all_overlays}

    def _resolve_overlays(facet: str) -> list[dict]:
        """Convert overlay IDs to full overlay dicts."""
        ids = overlays_by_facet.get(facet, [])
        return [overlay_lookup[oid] for oid in ids if oid in overlay_lookup]

    # Resolve document dicts from IDs for each facet
    all_docs = entities.get("documents", [])
    doc_lookup = {d.get("doc_id", d.get("filename", "")): d for d in all_docs}

    def _resolve_docs(facet: str) -> list[dict]:
        """Convert doc IDs to full document dicts."""
        ids = docs_by_facet.get(facet, [])
        return [doc_lookup[did] for did in ids if did in doc_lookup]

    # ══════════════════════════════════════════════════════════════════
    # STEP 2 — Module 01: Cross-Cutting Gatekeeper
    # ══════════════════════════════════════════════════════════════════
    crosscutting_overlays = _resolve_overlays("crosscutting") + _resolve_overlays("program")

    crosscutting_output = run_crosscutting(
        scenario_summary=scenario_summary,
        missing_core_variables=missing_core_variables,
        contradictions_detected=contradictions_detected,
        documents_subset=docs_by_facet.get("crosscutting", []),
        overlays_subset=crosscutting_overlays,
        guideline_sections=(
            guideline_section_refs.get("global", []) +
            guideline_section_refs.get("compliance", [])
        ),
        guidelines_path=guidelines_path,
    )

    # ══════════════════════════════════════════════════════════════════
    # STEP 3 — Modules 02–07: Domain-Focused Engines (parallel-safe)
    # ══════════════════════════════════════════════════════════════════

    # Module 02: Income
    income_output = run_income_conditions(
        scenario_summary=scenario_summary,
        documents_subset=_resolve_docs("income"),
        overlays_subset=_resolve_overlays("income"),
        guideline_sections=guideline_section_refs.get("income", []),
        guidelines_path=guidelines_path,
    )

    # Module 03: Assets
    assets_output = run_assets_conditions(
        scenario_summary=scenario_summary,
        documents_subset=_resolve_docs("assets"),
        overlays_subset=_resolve_overlays("assets"),
        guideline_sections=guideline_section_refs.get("assets", []),
        guidelines_path=guidelines_path,
    )

    # Module 04: Credit
    credit_output = run_credit_conditions(
        scenario_summary=scenario_summary,
        documents_subset=_resolve_docs("credit"),
        overlays_subset=_resolve_overlays("credit"),
        guideline_sections=guideline_section_refs.get("credit", []),
        guidelines_path=guidelines_path,
    )

    # Module 05: Property & Appraisal
    property_output = run_property_appraisal_conditions(
        scenario_summary=scenario_summary,
        documents_subset=_resolve_docs("property_appraisal"),
        overlays_subset=_resolve_overlays("property_appraisal"),
        guideline_sections=guideline_section_refs.get("property_appraisal", []),
        guidelines_path=guidelines_path,
    )

    # Module 06: Title & Closing
    title_output = run_title_closing_conditions(
        scenario_summary=scenario_summary,
        documents_subset=_resolve_docs("title_closing"),
        overlays_subset=_resolve_overlays("title_closing"),
        guideline_sections=guideline_section_refs.get("title_closing", []),
        guidelines_path=guidelines_path,
    )

    # Module 07: Compliance
    compliance_output = run_compliance_conditions(
        scenario_summary=scenario_summary,
        documents_subset=_resolve_docs("compliance"),
        overlays_subset=_resolve_overlays("compliance"),
        guideline_sections=guideline_section_refs.get("compliance", []),
        guidelines_path=guidelines_path,
    )

    # ══════════════════════════════════════════════════════════════════
    # STEP 4 — Module 08: Merger & Ranker
    # ══════════════════════════════════════════════════════════════════
    module_outputs = {
        "01": crosscutting_output,
        "02": income_output,
        "03": assets_output,
        "04": credit_output,
        "05": property_output,
        "06": title_output,
        "07": compliance_output,
    }

    final_output = run_merger_ranker(
        scenario_summary=scenario_summary,
        module_outputs=module_outputs,
    )

    return final_output


# ═══════════════════════════════════════════════════════════════════════════
# CLI Entry Point
# ═══════════════════════════════════════════════════════════════════════════


def main():
    """Command-line interface for running the predictive conditions engine."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="SBIQ AI Predictive Conditions Engine — "
                    "Generate AUS-style underwriting conditions for Non-QM loans."
    )
    parser.add_argument(
        "xml_path",
        help="Path to the MISMO XML or FNM loan file.",
    )
    parser.add_argument(
        "--entities",
        help="Path to extracted entities JSON file (optional).",
        default=None,
    )
    parser.add_argument(
        "--guidelines",
        help="Path to guidelines.md (optional; defaults to data/guidelines.md).",
        default=None,
    )
    parser.add_argument(
        "--output",
        help="Output file path (optional; defaults to stdout).",
        default=None,
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output.",
    )

    args = parser.parse_args()

    # Load entities if provided
    extracted_entities = None
    if args.entities:
        with open(args.entities, "r") as f:
            extracted_entities = json.load(f)

    # Run the pipeline
    result = run_predictive_conditions(
        xml_path=args.xml_path,
        extracted_entities=extracted_entities,
        guidelines_path=args.guidelines,
    )

    # Output
    indent = 2 if args.pretty else None
    output_json = json.dumps(result, indent=indent, default=str)

    if args.output:
        with open(args.output, "w") as f:
            f.write(output_json)
        print(f"Output written to {args.output}")
    else:
        print(output_json)


if __name__ == "__main__":
    main()
