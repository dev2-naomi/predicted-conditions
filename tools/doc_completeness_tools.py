"""
doc_completeness_tools.py — Tools for STEP_00b: Submission Document Completeness Check.

Deterministic check (no LLM) that compares the documents submitted in
the manifest against the required document list.

When eligibility engine data is available, the required document list
comes exclusively from the eligibility JSON's ``expected`` dicts
(keys = document category names like "EMD Check", "URLA 1003", etc.).
These are fuzzy-matched against the manifest's ``category_name`` values.

When no eligibility data is available, falls back to the hardcoded
checklists derived from submission_documents.md.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command
from typing_extensions import Annotated


# ─────────────────────────────────────────────────────────────────────────
# Explicit alias map: eligibility expected name → manifest category_name
# Used when substring matching alone would be ambiguous or miss.
# ─────────────────────────────────────────────────────────────────────────

_ELIG_TO_MANIFEST_ALIASES: dict[str, list[str]] = {
    "emd check": ["emd docs", "emd"],
    "loan pricing": ["lock confirmation", "smartfees", "loan estimate"],
    "title invoice": ["appraisal invoice", "title invoice"],
    "borrower certifications and disclosure": [
        "borrower certifications", "disclosure notices",
        "borrower certification", "compliance report",
    ],
    "anti steering disclosure": [
        "anti steering", "anti-steering",
    ],
    "consolidated 1099": ["1099", "1099-nec", "form 1099"],
    "1099 forms": ["consolidated 1099", "form 1099-nec", "1099"],
    "1099": ["consolidated 1099", "form 1099-nec", "1099-nec"],
    "verification of income": [
        "income calculations worksheet", "award letter",
        "verification of income",
    ],
    "asset": ["bank statement", "investment statement"],
    "bank statements": ["bank statement"],
    "bank statement": ["bank statements"],
    "paystub": ["paystub", "paystubs", "pay stub"],
    "paystubs": ["paystub"],
    "w2": ["w2", "w-2"],
    "purchase contract": ["purchase agreement", "sales contract"],
    "urla 1003": ["loan application", "1003"],
    "appraisal": ["appraisal report"],
    "credit report": ["tri-merge credit report", "tri-merge credit"],
}


def _normalize_doc_name(raw: str) -> str:
    """Strip parenthetical qualifiers, trailing noise, and collapse whitespace."""
    import re
    s = raw.strip().lower()
    s = re.sub(r"\(.*?\)", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _matches_manifest_name(elig_name: str, manifest_name: str) -> bool:
    """Check if an eligibility expected doc name matches a manifest category_name.

    Strategy:
      1. Exact case-insensitive match
      2. Either string is a substring of the other
      3. Normalized (parenthetical-stripped) substring match
      4. Explicit alias map lookup (both directions)
    """
    e_lower = elig_name.strip().lower()
    m_lower = manifest_name.strip().lower()

    if e_lower == m_lower:
        return True
    if e_lower in m_lower or m_lower in e_lower:
        return True

    e_norm = _normalize_doc_name(elig_name)
    m_norm = _normalize_doc_name(manifest_name)
    if e_norm and m_norm and (e_norm in m_norm or m_norm in e_norm):
        return True

    for key in (e_lower, e_norm):
        aliases = _ELIG_TO_MANIFEST_ALIASES.get(key, [])
        for alias in aliases:
            if alias in m_lower or m_lower in alias:
                return True

    for key in (m_lower, m_norm):
        aliases = _ELIG_TO_MANIFEST_ALIASES.get(key, [])
        for alias in aliases:
            if alias in e_lower or e_lower in alias:
                return True

    return False


# ─────────────────────────────────────────────────────────────────────────
# Fallback: hardcoded required document definitions (submission_documents.md)
# ─────────────────────────────────────────────────────────────────────────

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


def _doc_names_present(submitted_docs: list[dict]) -> set[str]:
    """Collect all document category_name values present (lowercased)."""
    names: set[str] = set()
    for d in submitted_docs:
        name = d.get("name", "")
        if name:
            names.add(name.strip().lower())
    return names


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


def _check_eligibility_docs(
    required_categories: list[str],
    submitted_docs: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Check required doc categories from the eligibility engine against
    the submitted manifest documents by fuzzy name matching.

    Returns (missing, satisfied) where each entry is:
        {"label": str, "matched_manifest_name": str | None}
    """
    manifest_names = _doc_names_present(submitted_docs)

    missing: list[dict] = []
    satisfied: list[dict] = []

    for elig_name in required_categories:
        found_match: str | None = None
        for m_name in manifest_names:
            if _matches_manifest_name(elig_name, m_name):
                found_match = m_name
                break

        entry = {"label": elig_name, "matched_manifest_name": found_match}
        if found_match is not None:
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
    present.

    When the eligibility engine provides a required_doc_categories list,
    those document names are the sole checklist — each is fuzzy-matched
    against the manifest's category_name values.

    When no eligibility data is available, falls back to the hardcoded
    checklists from submission_documents.md.
    """
    s = state or {}
    ss = s.get("scenario_summary", {})
    submitted_docs = ss.get("_submitted_docs", [])
    eligibility_data = ss.get("_eligibility_data", {})
    required_doc_categories = eligibility_data.get("required_doc_categories", [])

    conditions: list[dict[str, Any]] = []

    # ── Eligibility-driven path ──────────────────────────────────────
    if required_doc_categories:
        checklist_scope = ["eligibility_engine"]
        elig_missing, elig_satisfied = _check_eligibility_docs(
            required_doc_categories, submitted_docs,
        )

        deduped_missing = elig_missing
        deduped_satisfied = elig_satisfied

        for item in deduped_missing:
            label = item["label"]
            conditions.append({
                "category": "Document Completeness",
                "severity": "HARD-STOP",
                "priority": "P1",
                "title": f"Missing: {label}",
                "description": (
                    f"The submission package is missing '{label}', "
                    f"which is required by the eligibility engine."
                ),
                "required_documents": [label],
                "required_data_elements": [],
                "condition_family_id": f"doc_completeness_{label.lower().replace(' ', '_')}",
                "source_module": "00b",
                "guideline_ref": "eligibility_engine",
            })

    # ── Fallback: hardcoded checklist path ───────────────────────────
    else:
        present = _doc_types_present(submitted_docs)
        occupancy = (ss.get("occupancy") or "").lower()
        purpose = (ss.get("purpose") or "").lower()
        income_profile = ss.get("income_profile", {})
        primary_income = income_profile.get("primary_income_type", "unknown")
        borrower_type = (ss.get("borrower_type") or "").lower()

        all_missing: list[dict] = []
        all_satisfied: list[dict] = []
        checklist_scope: list[str] = ["base"]

        m, sat = _check_requirements(BASE_REQUIRED, present)
        all_missing.extend(m)
        all_satisfied.extend(sat)

        if "purchase" in purpose:
            checklist_scope.append("purchase")
            m, sat = _check_requirements(PURCHASE_REQUIRED, present)
            all_missing.extend(m)
            all_satisfied.extend(sat)

        is_investment = occupancy in ("investment", "investor", "non-owner occupied")
        is_llc = "llc" in borrower_type or "entity" in borrower_type
        if is_investment and is_llc:
            checklist_scope.append("investment_llc")
            m, sat = _check_requirements(LLC_INVESTMENT_REQUIRED, present)
            all_missing.extend(m)
            all_satisfied.extend(sat)

        income_reqs = INCOME_DOC_TYPE_REQUIRED.get(primary_income)
        if income_reqs:
            checklist_scope.append(f"income_{primary_income}")
            m, sat = _check_requirements(income_reqs, present)
            all_missing.extend(m)
            all_satisfied.extend(sat)

        income_types = income_profile.get("income_types", [])
        for itype in income_types:
            if itype != primary_income and itype in INCOME_DOC_TYPE_REQUIRED:
                checklist_scope.append(f"income_{itype}")
                secondary_reqs = INCOME_DOC_TYPE_REQUIRED[itype]
                m, sat = _check_requirements(secondary_reqs, present)
                all_missing.extend(m)
                all_satisfied.extend(sat)

        seen_labels: set[str] = set()
        deduped_missing_list: list[dict] = []
        for item in all_missing:
            if item["label"] not in seen_labels:
                seen_labels.add(item["label"])
                deduped_missing_list.append(item)

        seen_labels_sat: set[str] = set()
        deduped_satisfied_list: list[dict] = []
        for item in all_satisfied:
            if item["label"] not in seen_labels_sat:
                seen_labels_sat.add(item["label"])
                deduped_satisfied_list.append(item)

        deduped_missing = deduped_missing_list
        deduped_satisfied = deduped_satisfied_list

        _base_labels = {lbl for lbl, _ in BASE_REQUIRED}
        _purchase_labels = {lbl for lbl, _ in PURCHASE_REQUIRED}
        _llc_labels = {lbl for lbl, _ in LLC_INVESTMENT_REQUIRED}
        _income_labels: set[str] = set()
        for reqs in INCOME_DOC_TYPE_REQUIRED.values():
            for lbl, _ in reqs:
                _income_labels.add(lbl)

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
