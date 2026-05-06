"""
tools/__init__.py — Exports ALL_TOOLS and per-step tool lists (v2 Document-Centric).

ALL_TOOLS is imported by agent.py and step_loader.py.
"""

from __future__ import annotations

from tools.general import (
    get_workflow_status,
    save_step_report,
    write_todo,
)
from tools.scenario_tools import (
    build_scenario_summary,
    detect_contradictions,
    load_doctype_masterlist,
    parse_eligibility_output,
    parse_loan_file,
    parse_manifest_documents,
    route_to_facets,
)
from tools.crosscutting_tools import (
    check_overlay_conflicts,
    generate_crosscutting_document_requests,
)
from tools.income_tools import generate_income_document_requests, load_guideline_sections
from tools.assets_tools import generate_asset_document_requests
from tools.credit_tools import generate_credit_document_requests
from tools.property_tools import generate_property_document_requests
from tools.title_tools import generate_title_document_requests
from tools.compliance_tools import generate_compliance_document_requests
from tools.merger_tools import generate_final_output, merge_document_requests, rank_document_requests

# General tools — always available regardless of step
GENERAL_TOOLS = [
    write_todo,
    save_step_report,
    get_workflow_status,
]

# Per-step tool lists (mirrors registry.py)
STEP_TOOLS = {
    "STEP_00": [
        parse_loan_file,
        parse_manifest_documents,
        parse_eligibility_output,
        load_doctype_masterlist,
        build_scenario_summary,
        detect_contradictions,
        route_to_facets,
    ],
    "STEP_01": [
        load_guideline_sections,
        check_overlay_conflicts,
        generate_crosscutting_document_requests,
    ],
    "STEP_02": [
        load_guideline_sections,
        generate_income_document_requests,
    ],
    "STEP_03": [
        load_guideline_sections,
        generate_asset_document_requests,
    ],
    "STEP_04": [
        load_guideline_sections,
        generate_credit_document_requests,
    ],
    "STEP_05": [
        load_guideline_sections,
        generate_property_document_requests,
    ],
    "STEP_06": [
        load_guideline_sections,
        generate_title_document_requests,
    ],
    "STEP_07": [
        load_guideline_sections,
        generate_compliance_document_requests,
    ],
    "STEP_08": [
        merge_document_requests,
        rank_document_requests,
        generate_final_output,
    ],
}

# All tools — full flat list used for agent initialization
ALL_TOOLS = list(
    {t.name: t for t in GENERAL_TOOLS + [t for step in STEP_TOOLS.values() for t in step]}.values()
)
