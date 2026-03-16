"""
doc_completeness_tools.py — Tools for STEP_00b: Submission Document Completeness Check.

Deterministic check (no LLM) that compares the documents submitted in
the manifest/JSON against the required checklist defined in
data/submission_documents.md.

Two layers:
  1. Base required documents — always required for every transaction.
     If occupancy is Investment and entity type is LLC, the LLC docs
     (Articles of Organization, Operating Agreement, etc.) are added.
  2. Income-doc-type required documents — appended based on the
     income documentation type derived from the scenario summary.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command
from typing_extensions import Annotated


# ─────────────────────────────────────────────────────────────────────────
# Required document definitions (derived from submission_documents.md)
# ─────────────────────────────────────────────────────────────────────────

# Each entry: (label, list of doc_types that satisfy it)
# The doc_types correspond to the pipeline's canonical doc_type values
# from manifest_parser.py / scenario_tools.py.

BASE_REQUIRED: list[tuple[str, list[str]]] = [
    ("Initial 1003 (Loan Application)", ["loan_application"]),
    ("Most Recent Bank Statement", ["bank_statement"]),
    ("Credit Report (dated within 90 days)", ["credit_report"]),
    ("Appraisal", ["appraisal"]),
]

PURCHASE_REQUIRED: list[tuple[str, list[str]]] = [
    ("Purchase Contract", ["purchase_contract"]),
    ("Copy of EMD Check / Receipt", ["emd"]),
]

LLC_INVESTMENT_REQUIRED: list[tuple[str, list[str]]] = [
    ("Articles of Organization", ["articles_of_organization"]),
    ("Operating Agreement", ["operating_agreement"]),
    ("Federal Tax ID", ["federal_tax_id"]),
    ("Certificate of Good Standing", ["certificate_of_good_standing"]),
]

# ─────────────────────────────────────────────────────────────────────────
# Income-doc-type specific requirements
# Key = income_documentation_type slug from scenario_summary
# ─────────────────────────────────────────────────────────────────────────

INCOME_DOC_TYPE_REQUIRED: dict[str, list[tuple[str, list[str]]]] = {
    "W2": [
        ("Most recent paystub(s) reflecting 30 days of pay", ["paystub"]),
        ("Most recent 1 or 2 years W-2", ["W2", "tax_return"]),
    ],
    "self_employed": [
        ("Proof of 2 years Self-Employment", ["business_license", "CPA_letter", "articles_of_organization"]),
        ("1 or 2 years most recent tax returns (personal & business)", ["tax_return"]),
    ],
    "bank_statement": [
        ("Proof of 2 years Self-Employment", ["business_license", "CPA_letter", "articles_of_organization"]),
        ("Most recent 12 or 24 months bank statements", ["bank_statement"]),
        ("3rd Party Expense Statement or P&L", ["P_and_L", "expense_statement"]),
    ],
    "P_and_L": [
        ("Proof of 2 years Self-Employment", ["business_license", "CPA_letter", "articles_of_organization"]),
        ("Most recent 12 or 24 months 3rd Party P&L statement", ["P_and_L"]),
    ],
    "1099": [
        ("Most recent 1 or 2 years 1099 statements", ["1099"]),
    ],
    "WVOE": [
        ("Written VOE directly from the employer", ["VOE", "WVOE"]),
        ("Most recent 2 months bank statements", ["bank_statement"]),
    ],
    "asset_utilization": [
        ("Most recent 3 months asset statements for qualifying accounts", ["bank_statement", "investment_statement"]),
    ],
    "DSCR": [
        ("Proof of Rental Income: Current Lease or 1007", ["lease", "rental_analysis"]),
    ],
    "foreign_national": [
        ("Valid Unexpired Passport & VISA", ["ID", "passport"]),
    ],
    "ITIN": [
        ("ITIN Approval Letter (CP-565)", ["ITIN_letter"]),
        ("Unexpired ID (VISA, Passport or Driver's License)", ["ID", "passport"]),
    ],
}


def _doc_types_present(submitted_docs: list[dict]) -> set[str]:
    """Collect all doc_types present in submitted documents."""
    return {d.get("doc_type", "other") for d in submitted_docs if d.get("doc_type")}


def _check_requirements(
    requirements: list[tuple[str, list[str]]],
    present: set[str],
) -> tuple[list[dict], list[dict]]:
    """
    Check a list of requirements against the set of doc_types present.
    Returns (missing, satisfied) where each is a list of
    {"label": str, "accepted_doc_types": list[str]}.
    """
    missing: list[dict] = []
    satisfied: list[dict] = []
    for label, accepted in requirements:
        entry = {"label": label, "accepted_doc_types": accepted}
        if any(dt in present for dt in accepted):
            satisfied.append(entry)
        else:
            missing.append(entry)
    return missing, satisfied


@tool
def check_submission_completeness(
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
    state: Annotated[dict, InjectedState] = None,
) -> Command:
    """
    Deterministic check of whether the required submission documents are
    present, based on the transaction type and income documentation type.

    Reads from scenario_summary (occupancy, purpose, income_profile,
    doc_profile, _submitted_docs) to build the required checklist,
    then compares against the submitted documents.

    Stores results in module_outputs["00b"] with:
      - missing_documents: list of docs required but not found
      - satisfied_documents: list of docs that were found
      - checklist_scope: which requirement sets were applied
    """
    s = state or {}
    ss = s.get("scenario_summary", {})
    submitted_docs = ss.get("_submitted_docs", [])
    present = _doc_types_present(submitted_docs)

    occupancy = (ss.get("occupancy") or "").lower()
    purpose = (ss.get("purpose") or "").lower()
    income_profile = ss.get("income_profile", {})
    primary_income = income_profile.get("primary_income_type", "unknown")
    borrower_type = (ss.get("borrower_type") or "").lower()

    all_missing: list[dict] = []
    all_satisfied: list[dict] = []
    checklist_scope: list[str] = ["base"]

    # 1. Base required documents
    m, sat = _check_requirements(BASE_REQUIRED, present)
    all_missing.extend(m)
    all_satisfied.extend(sat)

    # 1b. Purchase-specific
    if "purchase" in purpose:
        checklist_scope.append("purchase")
        m, sat = _check_requirements(PURCHASE_REQUIRED, present)
        all_missing.extend(m)
        all_satisfied.extend(sat)

    # 1c. Investment LLC docs
    is_investment = occupancy in ("investment", "investor", "non-owner occupied")
    is_llc = "llc" in borrower_type or "entity" in borrower_type
    if is_investment and is_llc:
        checklist_scope.append("investment_llc")
        m, sat = _check_requirements(LLC_INVESTMENT_REQUIRED, present)
        all_missing.extend(m)
        all_satisfied.extend(sat)

    # 2. Income doc type requirements
    income_reqs = INCOME_DOC_TYPE_REQUIRED.get(primary_income)
    if income_reqs:
        checklist_scope.append(f"income_{primary_income}")
        m, sat = _check_requirements(income_reqs, present)
        all_missing.extend(m)
        all_satisfied.extend(sat)

    # For mixed income, also check secondary
    income_types = income_profile.get("income_types", [])
    for itype in income_types:
        if itype != primary_income and itype in INCOME_DOC_TYPE_REQUIRED:
            checklist_scope.append(f"income_{itype}")
            secondary_reqs = INCOME_DOC_TYPE_REQUIRED[itype]
            m, sat = _check_requirements(secondary_reqs, present)
            all_missing.extend(m)
            all_satisfied.extend(sat)

    # De-duplicate by label
    seen_labels: set[str] = set()
    deduped_missing: list[dict] = []
    for item in all_missing:
        if item["label"] not in seen_labels:
            seen_labels.add(item["label"])
            deduped_missing.append(item)

    seen_labels_sat: set[str] = set()
    deduped_satisfied: list[dict] = []
    for item in all_satisfied:
        if item["label"] not in seen_labels_sat:
            seen_labels_sat.add(item["label"])
            deduped_satisfied.append(item)

    # Build real conditions from missing documents so they flow into
    # the merger/ranker (STEP_09) alongside all other conditions.
    _base_labels = {lbl for lbl, _ in BASE_REQUIRED}
    _purchase_labels = {lbl for lbl, _ in PURCHASE_REQUIRED}
    _llc_labels = {lbl for lbl, _ in LLC_INVESTMENT_REQUIRED}
    _income_labels: set[str] = set()
    for reqs in INCOME_DOC_TYPE_REQUIRED.values():
        for lbl, _ in reqs:
            _income_labels.add(lbl)

    conditions: list[dict[str, Any]] = []
    for item in deduped_missing:
        label = item["label"]
        if label in _base_labels:
            reason = "required for all transactions"
        elif label in _purchase_labels:
            reason = "required for Purchase transactions"
        elif label in _llc_labels:
            reason = "required for LLC/Entity investment borrowers"
        elif label in _income_labels:
            income_slug = primary_income or "unknown"
            reason = f"required for {income_slug} income documentation"
        else:
            reason = "required for this transaction type"

        conditions.append({
            "category": "Document Completeness",
            "severity": "HARD-STOP",
            "priority": "P1",
            "title": f"Missing: {label}",
            "description": (
                f"The submission package is missing '{label}', "
                f"which is {reason}. Accepted document types: "
                f"{', '.join(item['accepted_doc_types'])}."
            ),
            "required_documents": [label],
            "required_data_elements": [],
            "condition_family_id": f"doc_completeness_{label.lower().replace(' ', '_')}",
            "source_module": "00b",
            "guideline_ref": "submission_documents.md",
        })

    module_output: dict[str, Any] = {
        "missing_documents": deduped_missing,
        "satisfied_documents": deduped_satisfied,
        "checklist_scope": checklist_scope,
        "total_submitted": len(submitted_docs),
        "doc_types_found": sorted(present - {"other"}),
        "conditions": conditions,
    }

    missing_labels = [d["label"] for d in deduped_missing]
    msg = (
        f"Document completeness check: {len(deduped_satisfied)} satisfied, "
        f"{len(deduped_missing)} missing → {len(conditions)} conditions generated. "
        f"Scope: {checklist_scope}."
    )
    if missing_labels:
        msg += f" Missing: {missing_labels}"

    return Command(update={
        "module_outputs": {"00b": module_output},
        "current_step": "STEP_00b",
        "messages": [ToolMessage(msg, tool_call_id=tool_call_id)],
    })
