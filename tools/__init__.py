"""
tools/__init__.py — Exports ALL_TOOLS and per-step tool lists.

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
    parse_loan_file,
    parse_loan_profile,
    parse_submitted_documents,
    route_to_facets,
)
from tools.doc_completeness_tools import check_submission_completeness
from tools.matrix_eligibility_tools import check_matrix_eligibility, generate_matrix_conditions, load_program_matrix
from tools.crosscutting_tools import (
    check_overlay_conflicts,
    generate_crosscutting_conditions,
)
from tools.income_tools import generate_income_conditions, load_guideline_sections
from tools.assets_tools import generate_asset_conditions
from tools.credit_tools import generate_credit_conditions
from tools.property_tools import generate_property_conditions
from tools.title_tools import generate_title_conditions
from tools.compliance_tools import generate_compliance_conditions
from tools.merger_tools import generate_final_output, merge_conditions, rank_conditions

# General tools — always available regardless of step
GENERAL_TOOLS = [
    write_todo,
    save_step_report,
    get_workflow_status,
]

# Per-step tool lists (mirrors registry.py for convenience)
STEP_TOOLS = {
    "STEP_00": [
        parse_loan_file,
        parse_loan_profile,
        parse_submitted_documents,
        build_scenario_summary,
        detect_contradictions,
        route_to_facets,
    ],
    "STEP_00b": [
        check_submission_completeness,
    ],
    "STEP_01": [
        check_overlay_conflicts,
        generate_crosscutting_conditions,
    ],
    "STEP_02": [
        load_guideline_sections,
        generate_income_conditions,
    ],
    "STEP_03": [
        load_guideline_sections,
        generate_asset_conditions,
    ],
    "STEP_04": [
        load_guideline_sections,
        generate_credit_conditions,
    ],
    "STEP_05": [
        load_guideline_sections,
        generate_property_conditions,
    ],
    "STEP_06": [
        load_guideline_sections,
        generate_title_conditions,
    ],
    "STEP_07": [
        load_guideline_sections,
        generate_compliance_conditions,
    ],
    "STEP_08": [
        check_matrix_eligibility,
        load_program_matrix,
        generate_matrix_conditions,
    ],
    "STEP_09": [
        merge_conditions,
        rank_conditions,
        generate_final_output,
    ],
}

# All tools — full flat list used for agent initialization
ALL_TOOLS = list(
    {t.name: t for t in GENERAL_TOOLS + [t for step in STEP_TOOLS.values() for t in step]}.values()
)
