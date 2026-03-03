"""
scenario_tools.py — Tools for STEP_00: Scenario Builder.

Parses the MISMO XML loan file and loan profile JSON, maps submitted
documents to doc_types, builds the scenario_summary, detects
contradictions, and routes docs/overlays to facets.
"""

from __future__ import annotations

import json
from typing import Any, List

from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command
from typing_extensions import Annotated

from tools.shared.xml_parser import parse_mismo_xml


# ---------------------------------------------------------------------------
# Document name → internal doc_type mapping
# ---------------------------------------------------------------------------

_DOC_NAME_TO_TYPE: dict[str, str] = {
    "credit report": "credit_report",
    "purchase contract": "purchase_contract",
    "bank statement": "bank_statement",
    "rental agreement": "lease",
    "lease": "lease",
    "lease agreement": "lease",
    "emd check": "emd",
    "earnest money deposit": "emd",
    "borrower certifications and disclosure": "compliance_notice",
    "borrower certification": "compliance_notice",
    "articles of organization": "business_license",
    "certificate of good standing": "business_license",
    "federal tax id number": "compliance_notice",
    "ein letter": "compliance_notice",
    "smartfees": "other",
    "appraisal": "appraisal",
    "title commitment": "title_commitment",
    "title report": "title_commitment",
    "payoff statement": "payoff_statement",
    "closing disclosure": "closing_disclosure",
    "homeowners insurance": "insurance",
    "hazard insurance": "insurance",
    "flood insurance": "insurance",
    "insurance": "insurance",
    "paystub": "paystub",
    "pay stub": "paystub",
    "w-2": "W2",
    "w2": "W2",
    "tax return": "tax_return",
    "1099": "1099",
    "1040": "tax_return",
    "profit and loss": "P_and_L",
    "p&l": "P_and_L",
    "p&l statement": "P_and_L",
    "voe": "VOE",
    "verification of employment": "VOE",
    "business license": "business_license",
    "id": "ID",
    "driver license": "ID",
    "passport": "ID",
    "government id": "ID",
    "itin letter": "ID",
    "affidavit": "affidavit",
    "compliance notice": "compliance_notice",
    "hoa questionnaire": "hoa_questionnaire",
    "condo questionnaire": "hoa_questionnaire",
    "operating agreement": "business_license",
    "1003": "loan_application",
    "uniform residential loan application": "loan_application",
    "vesting deed": "title_commitment",
    "grant deed": "title_commitment",
    "warranty deed": "title_commitment",
}


def _map_doc_name_to_type(name: str) -> str:
    """Map a human-readable document name to an internal doc_type."""
    key = name.strip().lower()
    if key in _DOC_NAME_TO_TYPE:
        return _DOC_NAME_TO_TYPE[key]
    for pattern, dtype in _DOC_NAME_TO_TYPE.items():
        if pattern in key or key in pattern:
            return dtype
    return "other"


# ---------------------------------------------------------------------------
# Income doc label → income_type mapping
# ---------------------------------------------------------------------------

_INCOME_DOC_MAP: dict[str, str] = {
    "full doc": "W2",
    "full documentation": "W2",
    "w2": "W2",
    "bank statement": "bank_statement",
    "12 month bank statement": "bank_statement",
    "24 month bank statement": "bank_statement",
    "1099": "1099",
    "p&l": "P_and_L",
    "profit and loss": "P_and_L",
    "dscr": "DSCR",
    "dscr / no ratio dscr": "DSCR",
    "no ratio": "no_ratio",
    "no ratio dscr": "no_ratio",
    "asset utilization": "asset_utilization",
    "wvoe": "WVOE",
    "voe only": "WVOE",
}


def _map_income_doc_label(label: str) -> str:
    key = label.strip().lower()
    if key in _INCOME_DOC_MAP:
        return _INCOME_DOC_MAP[key]
    for pattern, itype in _INCOME_DOC_MAP.items():
        if pattern in key:
            return itype
    return "unknown"


# ---------------------------------------------------------------------------
# Program routing
# ---------------------------------------------------------------------------


def _infer_program(
    parsed: dict,
    profile: dict,
    docs: list[dict],
    overlays: list[dict],
) -> str:
    """
    Infer NQMF program from loan characteristics.
    Priority: loan_profile_json program > overlay signals > XML inference.
    """
    # 1. Loan profile JSON has explicit program
    profile_meta = profile.get("metadata", {})
    loan_program = profile_meta.get("loan_program", {})
    program_name = loan_program.get("name") or loan_program.get("originalApiKey") or ""
    if program_name and program_name.lower() not in ("", "unknown", "conventional"):
        return program_name

    # 2. Check overlays
    for overlay in overlays:
        rt = overlay.get("rule_text", "")
        for prog in [
            "DSCR Supreme", "Investor DSCR", "No Ratio DSCR", "Multi 5-8 DSCR",
            "Flex Supreme", "Flex Select", "Super Jumbo", "ITIN",
            "Foreign National", "Second Lien Select", "Select ITIN",
        ]:
            if prog.lower() in rt.lower():
                return prog

    # 3. Infer from combined data
    occupancy = (
        profile_meta.get("occupancy")
        or parsed.get("occupancy")
        or ""
    ).lower()
    citizenship = (
        profile_meta.get("citizenship")
        or parsed.get("citizenship")
        or ""
    ).lower()
    units = profile_meta.get("units") or parsed.get("units")
    lien = (
        profile_meta.get("loan_type")
        or parsed.get("lien_priority")
        or ""
    ).lower()
    loan_amount = (
        profile_meta.get("loan_amount")
        or parsed.get("loan_amount")
        or 0
    )
    income_doc = (profile_meta.get("income_doc") or "").lower()

    doc_types = {d.get("doc_type", "").lower() for d in docs}

    has_income_docs = bool(
        doc_types & {"paystub", "w2", "tax_return", "1099", "p_and_l", "voe"}
    )
    has_bank_stmts = "bank_statement" in doc_types
    has_lease = "lease" in doc_types

    if "itin" in citizenship or "itin" in income_doc:
        return "ITIN"

    if "foreign" in citizenship:
        return "Foreign National"

    if "second" in lien and "lien" in lien:
        return "Second Lien Select"

    if occupancy == "investment" and units and int(units) >= 5:
        return "Multi 5-8 DSCR"

    if "dscr" in income_doc or "no ratio" in income_doc:
        return "DSCR Supreme"

    if occupancy == "investment" and not has_income_docs:
        if has_lease:
            return "DSCR Supreme"
        return "No Ratio DSCR"

    if loan_amount > 3_000_000:
        return "Super Jumbo"

    if has_bank_stmts and not has_income_docs:
        return "Flex Supreme"

    if has_income_docs:
        return "Flex Supreme"

    return "unknown"


def _guideline_section_refs(program: str, income_types: list[str], property_type: str) -> dict:
    refs: dict[str, list[str]] = {
        "global": [
            "GENERAL UNDERWRITING REQUIREMENTS",
            "OCCUPANCY TYPES",
            "TRANSACTION TYPES",
        ],
        "income": [],
        "assets": ["ASSETS", "RESERVES"],
        "credit": [
            "CREDIT",
            "HOUSING HISTORY",
            "HOUSING EVENTS AND PRIOR BANKRUPTCY",
            "LIABILITIES",
        ],
        "property_appraisal": [
            "APPRAISALS",
            "PROPERTY CONSIDERATIONS",
            "PROPERTY TYPES",
        ],
        "title_closing": ["PROPERTY INSURANCE", "TITLE INSURANCE"],
        "compliance": ["COMPLIANCE", "BORROWER ELIGIBILITY", "VESTING AND OWNERSHIP"],
    }

    prog_lower = program.lower()
    if "dscr" in prog_lower:
        refs["income"] = [
            "DSCR RATIOS AND RENTAL INCOME REQUIREMENTS",
            "DSCR PRODUCT TERMS",
        ]
    elif any(it in income_types for it in ["bank_statement", "P_and_L", "1099", "WVOE", "asset_utilization"]):
        refs["income"] = [
            "ALTERNATIVE DOCUMENTATION (ALT DOC)",
            "RATIOS AND QUALIFYING – FULL AND ALT DOC",
        ]
    else:
        refs["income"] = [
            "FULL DOCUMENTATION",
            "EMPLOYMENT",
            "RATIOS AND QUALIFYING – FULL AND ALT DOC",
        ]

    if program == "ITIN":
        refs["income"] += ["ITIN", "ITIN – DOCUMENTATION REQUIREMENTS", "ITIN - ELIGIBILITY"]
    if program == "Foreign National":
        refs["income"] += ["FOREIGN NATIONALS"]
    if "Second Lien" in program:
        refs["income"] += ["SECOND LIEN", "SECOND LIEN SELECT SENIOR LIEN QUALIFYING TERMS"]

    pt = property_type.lower()
    if "condo" in pt:
        refs["property_appraisal"] += [
            "CONDOMINIUMS - GENERAL",
            "WARRANTABLE CONDOMINIUMS",
            "NON-WARRANTABLE CONDOMINIUMS",
        ]
    if "co-op" in pt:
        refs["property_appraisal"] += ["COOPERATIVES (CO-OP)"]

    return refs


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@tool
def parse_loan_file(
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
    state: Annotated[dict, InjectedState] = None,
) -> Command:
    """
    Parse the MISMO XML loan file from state and return structured loan fields.
    Supports both iLAD 2.0 and FNM 3.0 formats.
    """
    xml_content = (state or {}).get("loan_file_xml", "")
    if not xml_content:
        return Command(update={
            "flags": [{
                "substep": "0.1",
                "title": "Missing loan file XML",
                "severity": "HARD-STOP",
                "detail": "No loan_file_xml provided in state. Cannot proceed.",
            }],
            "messages": [ToolMessage("HARD-STOP: No loan_file_xml provided.", tool_call_id=tool_call_id)],
        })

    parsed = parse_mismo_xml(xml_content)
    names = parsed.get("borrower_names", [])
    return Command(update={
        "scenario_summary": {"_parsed_xml": parsed},
        "messages": [ToolMessage(
            f"XML parsed successfully. Borrowers: {names}. "
            f"Loan amount: {parsed.get('loan_amount')}, LTV: {parsed.get('ltv')}",
            tool_call_id=tool_call_id,
        )],
    })


@tool
def parse_loan_profile(
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
    state: Annotated[dict, InjectedState] = None,
) -> Command:
    """
    Parse the loan profile JSON (case JSON) from state.
    Extracts structured loan metadata: program, borrower, FICO, LTV,
    occupancy, property info, income doc type, etc.
    """
    raw = (state or {}).get("loan_profile_json", "")
    if not raw:
        return Command(update={
            "flags": [{
                "substep": "0.2",
                "title": "Missing loan profile JSON",
                "severity": "SOFT-STOP",
                "detail": "No loan_profile_json provided in state.",
            }],
            "scenario_summary": {"_loan_profile": {}},
            "messages": [ToolMessage("SOFT-STOP: No loan_profile_json provided.", tool_call_id=tool_call_id)],
        })

    try:
        profile = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError as e:
        return Command(update={
            "flags": [{
                "substep": "0.2",
                "title": "Invalid loan profile JSON",
                "severity": "SOFT-STOP",
                "detail": f"JSON parse error: {e}",
            }],
            "scenario_summary": {"_loan_profile": {}},
            "messages": [ToolMessage(f"SOFT-STOP: Invalid JSON: {e}", tool_call_id=tool_call_id)],
        })

    meta = profile.get("metadata", {})
    program = meta.get("loan_program", {}).get("name", "unknown")
    return Command(update={
        "scenario_summary": {"_loan_profile": profile},
        "messages": [ToolMessage(
            f"Loan profile parsed. Program: {program}, FICO: {meta.get('fico')}, "
            f"LTV: {meta.get('ltv_pct')}, Occupancy: {meta.get('occupancy')}",
            tool_call_id=tool_call_id,
        )],
    })


@tool
def parse_submitted_documents(
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
    state: Annotated[dict, InjectedState] = None,
) -> Command:
    """
    Parse the submitted documents JSON from state.
    Each entry has an id and name (e.g. {"doc_id": "117", "name": "Credit Report"}).
    Maps each document name to an internal doc_type for facet routing.
    """
    raw = (state or {}).get("submitted_documents_json", "")
    if not raw:
        return Command(update={
            "scenario_summary": {"_submitted_docs": []},
            "messages": [ToolMessage("No submitted documents provided.", tool_call_id=tool_call_id)],
        })

    try:
        doc_list = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError as e:
        return Command(update={
            "flags": [{
                "substep": "0.2b",
                "title": "Invalid submitted documents JSON",
                "severity": "SOFT-STOP",
                "detail": f"JSON parse error: {e}",
            }],
            "scenario_summary": {"_submitted_docs": []},
            "messages": [ToolMessage(f"SOFT-STOP: Invalid JSON: {e}", tool_call_id=tool_call_id)],
        })

    if not isinstance(doc_list, list):
        doc_list = []

    mapped: list[dict] = []
    for doc in doc_list:
        doc_id = str(doc.get("doc_id") or doc.get("id") or "")
        name = doc.get("name", "")
        doc_type = doc.get("doc_type") or _map_doc_name_to_type(name)
        mapped.append({
            "doc_id": doc_id,
            "doc_type": doc_type,
            "name": name,
            "extracted_fields": doc.get("extracted_fields", {}),
            "flags": doc.get("flags", []),
        })

    doc_summary = ", ".join(f"{d['name']} ({d['doc_type']})" for d in mapped)
    return Command(update={
        "scenario_summary": {"_submitted_docs": mapped},
        "messages": [ToolMessage(
            f"Parsed {len(mapped)} submitted documents: {doc_summary}",
            tool_call_id=tool_call_id,
        )],
    })


def _pick(profile_val: Any, xml_val: Any, fallback: Any = "unknown") -> Any:
    """Pick the best non-null/non-unknown value. Profile wins when present."""
    for v in (profile_val, xml_val):
        if v is not None and v != "unknown" and v != "" and v != 0:
            return v
    return fallback


def _build_borrower_name(b: dict) -> str:
    parts = [
        b.get("first_name", ""),
        b.get("middle_name", ""),
        b.get("last_name", ""),
    ]
    suffix = b.get("suffix", "")
    name = " ".join(p for p in parts if p).strip()
    if suffix:
        name = f"{name} {suffix}"
    return name or "unknown"


@tool
def build_scenario_summary(
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
    state: Annotated[dict, InjectedState] = None,
) -> Command:
    """
    Build the full scenario_summary by merging data from:
    1. Parsed XML (_parsed_xml)
    2. Loan profile JSON (_loan_profile)
    3. Submitted documents (_submitted_docs)
    Also identifies missing_core_variables.
    """
    s = state or {}
    ss = s.get("scenario_summary", {})
    parsed: dict = ss.get("_parsed_xml", {})
    profile: dict = ss.get("_loan_profile", {})
    submitted_docs: list[dict] = ss.get("_submitted_docs", [])

    meta = profile.get("metadata", {})
    loan_program = meta.get("loan_program", {})

    doc_types = [d.get("doc_type") for d in submitted_docs if d.get("doc_type")]

    # Determine income types from income_doc label and submitted docs
    income_doc_label = meta.get("income_doc", "")
    primary_income_from_label = _map_income_doc_label(income_doc_label) if income_doc_label else "unknown"

    income_types: list[str] = []
    if primary_income_from_label != "unknown":
        income_types.append(primary_income_from_label)

    for dt in doc_types:
        if dt in ("paystub", "W2", "VOE"):
            if "W2" not in income_types:
                income_types.append("W2")
        elif dt == "bank_statement" and "bank_statement" not in income_types:
            income_types.append("bank_statement")
        elif dt == "tax_return" and "self_employed" not in income_types:
            income_types.append("self_employed")
        elif dt == "1099" and "1099" not in income_types:
            income_types.append("1099")
        elif dt == "P_and_L" and "P_and_L" not in income_types:
            income_types.append("P_and_L")
        elif dt == "lease" and "DSCR" not in income_types:
            income_types.append("DSCR")

    if not income_types:
        income_types = ["unknown"]

    program = _infer_program(parsed, profile, submitted_docs, [])
    property_type = _pick(meta.get("property_type"), parsed.get("property_type"), "Unknown")

    # Purpose — profile wins, then XML
    raw_purpose = _pick(meta.get("purpose"), parsed.get("purpose"))
    purpose = raw_purpose
    if isinstance(raw_purpose, str) and raw_purpose.lower() in ("refinance", "nocash-outrefinance"):
        cash_out = parsed.get("cash_out_amount")
        purpose = "Cash-OutRefinance" if (cash_out and cash_out > 0) else "NoCash-OutRefinance"

    # Borrowers — merge profile borrower info with XML borrower data
    borrowers: list[dict] = []
    profile_borrower = meta.get("borrower", {})
    xml_names = parsed.get("borrower_names", [])
    xml_ssns = parsed.get("borrower_ssns", [])
    xml_dobs = parsed.get("borrower_dobs", [])
    profile_citizenship = meta.get("citizenship", "unknown")

    if profile_borrower:
        borrowers.append({
            "name": _build_borrower_name(profile_borrower),
            "ssn": xml_ssns[0] if xml_ssns else None,
            "dob": xml_dobs[0] if xml_dobs else None,
            "role": "primary",
            "self_employed": parsed.get("self_employed"),
            "citizenship": profile_citizenship,
            "military": parsed.get("military"),
        })
    elif xml_names:
        borrowers.append({
            "name": xml_names[0],
            "ssn": xml_ssns[0] if xml_ssns else None,
            "dob": xml_dobs[0] if xml_dobs else None,
            "role": "primary",
            "self_employed": parsed.get("self_employed"),
            "citizenship": _pick(profile_citizenship, parsed.get("citizenship")),
            "military": parsed.get("military"),
        })

    profile_coborrower = meta.get("co_borrower")
    if profile_coborrower and isinstance(profile_coborrower, dict):
        borrowers.append({
            "name": _build_borrower_name(profile_coborrower),
            "ssn": xml_ssns[1] if len(xml_ssns) > 1 else None,
            "dob": xml_dobs[1] if len(xml_dobs) > 1 else None,
            "role": "co-borrower",
            "self_employed": parsed.get("self_employed"),
            "citizenship": profile_citizenship,
            "military": None,
        })
    elif len(xml_names) > 1:
        for i, name in enumerate(xml_names[1:], start=1):
            borrowers.append({
                "name": name,
                "ssn": xml_ssns[i] if i < len(xml_ssns) else None,
                "dob": xml_dobs[i] if i < len(xml_dobs) else None,
                "role": "co-borrower",
                "self_employed": parsed.get("self_employed"),
                "citizenship": _pick(profile_citizenship, parsed.get("citizenship")),
                "military": None,
            })

    # Numbers — profile wins for explicit fields
    loan_amount = _pick(meta.get("loan_amount"), parsed.get("loan_amount"), None)
    property_value = meta.get("property_value")
    appraised_value = _pick(property_value, parsed.get("appraised_value"), None)
    purchase_price = _pick(property_value, parsed.get("purchase_price"), None)
    note_rate = _pick(
        loan_program.get("rate"), parsed.get("note_rate"), None
    )
    ltv = _pick(meta.get("ltv_pct"), parsed.get("ltv"), None)
    cltv = _pick(meta.get("cltv_pct"), parsed.get("cltv"), None)
    fico = _pick(meta.get("fico"), parsed.get("fico"), None)
    dti = _pick(meta.get("dti"), parsed.get("dti"), "unknown")

    # Units — 0 means not specified in profile
    units_raw = meta.get("units")
    units = _pick(
        units_raw if units_raw and units_raw > 0 else None,
        parsed.get("units"),
        None,
    )

    summary: dict[str, Any] = {
        "program": program,
        "product_variant": loan_program.get("type", "unknown"),
        "purpose": purpose,
        "occupancy": _pick(meta.get("occupancy"), parsed.get("occupancy")),
        "property": {
            "address": _pick(meta.get("property_address"), parsed.get("property_address")),
            "state": _pick(meta.get("state"), parsed.get("property_state")),
            "county": _pick(meta.get("county"), parsed.get("property_county")),
            "city": parsed.get("property_city", "unknown"),
            "zip": parsed.get("property_zip", "unknown"),
            "units": units,
            "property_type": property_type,
            "year_built": parsed.get("year_built"),
            "rural_property": meta.get("rural_property", False),
        },
        "numbers": {
            "loan_amount": loan_amount,
            "purchase_price": purchase_price,
            "appraised_value": appraised_value,
            "note_rate": note_rate,
            "LTV": ltv,
            "CLTV": cltv,
            "DTI": dti,
        },
        "loan_terms": {
            "amortization_type": parsed.get("amortization_type", "unknown"),
            "term_months": parsed.get("loan_term_months"),
            "interest_only": parsed.get("interest_only"),
            "prepay_penalty": parsed.get("prepay_penalty"),
            "balloon": parsed.get("balloon"),
            "lien_priority": _pick(meta.get("loan_type"), parsed.get("lien_priority")),
        },
        "credit": {
            "fico": fico,
            "credit_scores": parsed.get("credit_scores", []),
            "fico_source": "loan_profile" if meta.get("fico") else ("xml" if parsed.get("fico") else "unknown"),
            "is_us_credit": meta.get("is_us_credit", True),
            "mortgage_history_flags": [],
            "credit_events": [],
            "declarations": parsed.get("declarations", {}),
        },
        "borrowers": borrowers,
        "income_profile": {
            "income_types": income_types,
            "primary_income_type": income_types[0] if income_types else "unknown",
            "income_doc_label": income_doc_label,
            "secondary_income": meta.get("secondary_income", "N/A"),
            "secondary_income_doc": meta.get("secondary_income_doc", "N/A"),
        },
        "asset_profile": {
            "has_bank_statements": "bank_statement" in doc_types,
            "has_large_deposit_flags": any(
                "large_deposit" in (d.get("flags") or []) for d in submitted_docs
            ),
            "has_gift_indicators": any(
                "gift" in str(d.get("extracted_fields", {})).lower() for d in submitted_docs
            ),
            "has_reserves_indicators": bool(meta.get("months_reserves")),
            "months_reserves": meta.get("months_reserves"),
            "total_liquid_assets": meta.get("total_liquid_assets"),
            "total_investment_assets": meta.get("total_investment_assets"),
            "total_retirement_assets": meta.get("total_retirement_assets"),
        },
        "reo_summary": {
            "total_properties_owned": len(parsed.get("owned_properties", [])),
            "total_lien_balance": None,
            "subject_property_rental_income": None,
        },
        "doc_profile": list(set(doc_types)),
        "housing_expenses": parsed.get("housing_expenses", {"present": [], "proposed": []}),
        "cash_out_amount": parsed.get("cash_out_amount"),
        "channel": meta.get("channel", "unknown"),
        "loan_number": meta.get("loan_number"),
        "dscr_label": meta.get("dscr"),
        "borrower_type": meta.get("borrower_type"),
    }

    # Credit events from declarations
    decl = parsed.get("declarations", {})
    credit_events = []
    if decl.get("BankruptcyIndicator"):
        credit_events.append("BK")
    if decl.get("PriorPropertyForeclosureCompletedIndicator"):
        credit_events.append("FC")
    if decl.get("PriorPropertyShortSaleCompletedIndicator"):
        credit_events.append("SS")
    if decl.get("PriorPropertyDeedInLieuConveyedIndicator"):
        credit_events.append("DIL")
    summary["credit"]["credit_events"] = credit_events or ["none"]

    # Missing core variables
    missing: list[str] = []
    required_fields = [
        ("purpose", summary["purpose"]),
        ("occupancy", summary["occupancy"]),
        ("property_state", summary["property"]["state"]),
        ("loan_amount", summary["numbers"]["loan_amount"]),
        ("LTV", summary["numbers"]["LTV"]),
        ("FICO", summary["credit"]["fico"]),
        ("program", summary["program"]),
        ("income_documentation_type", summary["income_profile"]["primary_income_type"]),
    ]
    for field, val in required_fields:
        if val in (None, "unknown"):
            missing.append(field)

    guideline_refs = _guideline_section_refs(program, income_types, property_type)

    missing_str = ", ".join(missing) if missing else "none"
    return Command(update={
        "scenario_summary": summary,
        "missing_core_variables": missing,
        "guideline_section_refs": guideline_refs,
        "messages": [ToolMessage(
            f"Scenario summary built. Program: {program}, Purpose: {purpose}, "
            f"Occupancy: {summary['occupancy']}, LTV: {ltv}, FICO: {fico}, "
            f"Property: {property_type} in {summary['property']['state']}. "
            f"Income types: {income_types}. Missing core variables: {missing_str}",
            tool_call_id=tool_call_id,
        )],
    })


@tool
def detect_contradictions(
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
    state: Annotated[dict, InjectedState] = None,
) -> Command:
    """
    Compare XML-sourced fields against loan profile and submitted document fields.
    Returns a list of contradictions_detected.
    """
    s = state or {}
    ss = s.get("scenario_summary", {})
    parsed: dict = ss.get("_parsed_xml", {})
    profile: dict = ss.get("_loan_profile", {})
    submitted_docs: list[dict] = ss.get("_submitted_docs", [])

    contradictions = []
    meta = profile.get("metadata", {})

    xml_address = parsed.get("property_address", "")
    xml_value = parsed.get("appraised_value")

    # XML vs profile contradictions
    if meta.get("property_value") and xml_value:
        profile_value = float(meta["property_value"])
        if abs(profile_value - xml_value) / max(profile_value, xml_value) > 0.01:
            contradictions.append({
                "type": "VALUE_MISMATCH",
                "source_a": "xml",
                "source_b": "loan_profile",
                "details": (
                    f"XML appraised value: {xml_value}. "
                    f"Loan profile property value: {profile_value}."
                ),
            })

    # Check submitted docs for flagged contradictions
    for doc in submitted_docs:
        fields = doc.get("extracted_fields", {})
        flags = doc.get("flags", [])

        if "name_mismatch" in flags:
            xml_names = parsed.get("borrower_names", [])
            entity_name = fields.get("borrower_name") or fields.get("account_holder") or ""
            contradictions.append({
                "type": "NAME_MISMATCH",
                "source_a": "xml",
                "source_b": doc.get("doc_id", "unknown"),
                "details": (
                    f"XML borrower names: {xml_names}. "
                    f"Entity name: {entity_name}. "
                    f"Document flagged name_mismatch."
                ),
            })

        if "address_mismatch" in flags:
            entity_address = (
                fields.get("subject_address")
                or fields.get("property_address")
                or ""
            )
            contradictions.append({
                "type": "ADDRESS_MISMATCH",
                "source_a": "xml",
                "source_b": doc.get("doc_id", "unknown"),
                "details": (
                    f"XML address: {xml_address}. "
                    f"Entity address: {entity_address}."
                ),
            })

        if doc.get("doc_type") == "appraisal":
            entity_value = _safe_float_local(fields.get("appraised_value"))
            if (
                xml_value
                and entity_value
                and abs(xml_value - entity_value) / max(xml_value, entity_value) > 0.01
            ):
                contradictions.append({
                    "type": "VALUE_MISMATCH",
                    "source_a": "xml",
                    "source_b": doc.get("doc_id", "unknown"),
                    "details": (
                        f"XML appraised value: {xml_value}. "
                        f"Appraisal entity value: {entity_value}."
                    ),
                })

    msg = f"Found {len(contradictions)} contradiction(s)." if contradictions else "No contradictions detected."
    return Command(update={
        "contradictions_detected": contradictions,
        "messages": [ToolMessage(msg, tool_call_id=tool_call_id)],
    })


def _safe_float_local(val) -> float | None:
    if val is None:
        return None
    try:
        return float(str(val).replace(",", ""))
    except (ValueError, TypeError):
        return None


@tool
def route_to_facets(
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
    state: Annotated[dict, InjectedState] = None,
) -> Command:
    """
    Partition submitted documents into per-facet buckets based on doc_type.
    Returns docs_by_facet and overlays_by_facet.
    """
    s = state or {}
    ss = s.get("scenario_summary", {})
    submitted_docs: list[dict] = ss.get("_submitted_docs", [])

    is_bank_stmt_income = "bank_statement" in (
        ss.get("income_profile", {}).get("income_types", []) if isinstance(ss, dict) else []
    )

    facets = ["crosscutting", "income", "assets", "credit", "property_appraisal", "title_closing", "compliance"]
    docs_by_facet: dict[str, list[str]] = {f: [] for f in facets}
    overlays_by_facet: dict[str, list[str]] = {f: [] for f in facets}
    overlays_by_facet["program"] = []

    _DOC_FACET_MAP = {
        "credit_report": ["credit"],
        "appraisal": ["property_appraisal"],
        "title_commitment": ["title_closing"],
        "payoff_statement": ["title_closing"],
        "closing_disclosure": ["title_closing"],
        "insurance": ["title_closing"],
        "paystub": ["income"],
        "W2": ["income"],
        "VOE": ["income"],
        "tax_return": ["income"],
        "1099": ["income"],
        "P_and_L": ["income"],
        "business_license": ["income", "compliance"],
        "lease": ["income"],
        "affidavit": ["compliance"],
        "compliance_notice": ["compliance"],
        "ID": ["crosscutting"],
        "loan_application": ["crosscutting"],
        "purchase_contract": ["title_closing", "crosscutting"],
        "emd": ["assets"],
        "hoa_questionnaire": ["property_appraisal"],
        "other": ["crosscutting"],
    }

    for doc in submitted_docs:
        dt = doc.get("doc_type", "other")
        doc_id = doc.get("doc_id", "")
        mapped_facets = _DOC_FACET_MAP.get(dt, ["crosscutting"])

        if dt == "bank_statement":
            mapped_facets = ["assets"]
            if is_bank_stmt_income:
                mapped_facets = ["assets", "income"]

        for f in mapped_facets:
            if f in docs_by_facet and doc_id not in docs_by_facet[f]:
                docs_by_facet[f].append(doc_id)

    facet_summary = ", ".join(f"{f}: {len(ids)}" for f, ids in docs_by_facet.items() if ids)
    return Command(update={
        "docs_by_facet": docs_by_facet,
        "overlays_by_facet": overlays_by_facet,
        "messages": [ToolMessage(
            f"Documents routed to facets: {facet_summary}",
            tool_call_id=tool_call_id,
        )],
    })
