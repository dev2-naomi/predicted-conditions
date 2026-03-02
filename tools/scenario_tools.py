"""
scenario_tools.py — Tools for STEP_00: Scenario Builder.

Parses the MISMO XML and extracted-entities JSON, builds the scenario_summary,
detects contradictions, and routes docs/overlays to facets.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import InjectedToolArg, tool
from langgraph.types import Command
from typing_extensions import Annotated

from tools.shared.xml_parser import parse_mismo_xml


# ---------------------------------------------------------------------------
# Program routing
# ---------------------------------------------------------------------------

_PROGRAM_ROUTING: list[tuple[str, str]] = [
    # (signal description, program)
    # Evaluated in order; first match wins.
]


def _infer_program(parsed: dict, docs: list[dict], overlays: list[dict]) -> str:
    """
    Infer NQMF program from loan characteristics per 00_ScenarioBuilder.md logic.
    """
    # Check overlays / extracted entities first
    for overlay in overlays:
        rt = overlay.get("rule_text", "")
        for prog in [
            "DSCR Supreme", "Investor DSCR", "No Ratio DSCR", "Multi 5-8 DSCR",
            "Flex Supreme", "Flex Select", "Super Jumbo", "ITIN",
            "Foreign National", "Second Lien Select",
        ]:
            if prog.lower() in rt.lower():
                return prog

    occupancy = (parsed.get("occupancy") or "").lower()
    units = parsed.get("units")
    lien = (parsed.get("lien_priority") or "").lower()
    citizenship = (parsed.get("citizenship") or "").lower()
    loan_amount = parsed.get("loan_amount") or 0

    doc_types = {d.get("doc_type", "").lower() for d in docs}

    has_income_docs = bool(
        doc_types & {"paystub", "w2", "tax_return", "1099", "p_and_l", "voe"}
    )
    has_bank_stmts = "bank_statement" in doc_types
    has_lease = "lease" in doc_types
    has_asset_utilization = any("asset" in dt for dt in doc_types)

    if "itin" in citizenship or any(
        d.get("doc_type", "") == "ID" and "itin" in d.get("filename", "").lower()
        for d in docs
    ):
        return "ITIN"

    if "foreignnational" in citizenship.replace(" ", "").lower():
        return "Foreign National"

    if "secondlien" in lien.replace(" ", "").lower():
        return "Second Lien Select"

    if occupancy == "investment" and units and int(units) >= 5:
        return "Multi 5-8 DSCR"

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
    state: Annotated[dict, InjectedToolArg] = None,
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
            }]
        })

    parsed = parse_mismo_xml(xml_content)
    return Command(update={"scenario_summary": {"_parsed_xml": parsed}})


@tool
def parse_extracted_entities(
    state: Annotated[dict, InjectedToolArg] = None,
) -> Command:
    """
    Parse the extracted-entities JSON from state.
    Returns structured documents list and overlays list.
    """
    raw = (state or {}).get("extracted_entities_json", "")
    if not raw:
        entities = {"documents": [], "overlays": []}
    else:
        try:
            entities = json.loads(raw)
        except json.JSONDecodeError as e:
            return Command(update={
                "flags": [{
                    "substep": "0.2",
                    "title": "Invalid extracted entities JSON",
                    "severity": "SOFT-STOP",
                    "detail": f"JSON parse error: {e}",
                }],
                "scenario_summary": {"_extracted_entities": {"documents": [], "overlays": []}},
            })

    return Command(update={"scenario_summary": {"_extracted_entities": entities}})


@tool
def build_scenario_summary(
    state: Annotated[dict, InjectedToolArg] = None,
) -> Command:
    """
    Build the full scenario_summary from parsed XML and extracted entities.
    Also identifies missing_core_variables.
    """
    s = state or {}
    ss = s.get("scenario_summary", {})
    parsed: dict = ss.get("_parsed_xml", {})
    entities: dict = ss.get("_extracted_entities", {})
    docs: list[dict] = entities.get("documents", [])
    overlays: list[dict] = entities.get("overlays", [])

    doc_types = [d.get("doc_type") for d in docs if d.get("doc_type")]

    income_types: list[str] = []
    for dt in doc_types:
        if dt in ("paystub", "W2", "VOE"):
            if "W2" not in income_types:
                income_types.append("W2")
        elif dt == "bank_statement":
            income_types.append("bank_statement")
        elif dt == "tax_return":
            income_types.append("self_employed")
        elif dt == "1099":
            income_types.append("1099")
        elif dt == "P_and_L":
            income_types.append("P_and_L")
        elif dt == "lease":
            income_types.append("DSCR")

    if not income_types and not docs:
        income_types = ["unknown"]

    program = _infer_program(parsed, docs, overlays)
    property_type = parsed.get("property_type", "Unknown")

    summary: dict[str, Any] = {
        "program": program,
        "product_variant": "unknown",
        "purpose": parsed.get("purpose", "unknown"),
        "occupancy": parsed.get("occupancy", "unknown"),
        "property": {
            "address": parsed.get("property_address", "unknown"),
            "state": parsed.get("property_state", "unknown"),
            "county": parsed.get("property_county", "unknown"),
            "city": parsed.get("property_city", "unknown"),
            "zip": parsed.get("property_zip", "unknown"),
            "units": parsed.get("units"),
            "property_type": property_type,
            "year_built": parsed.get("year_built"),
        },
        "numbers": {
            "loan_amount": parsed.get("loan_amount"),
            "purchase_price": parsed.get("purchase_price"),
            "appraised_value": parsed.get("appraised_value"),
            "note_rate": parsed.get("note_rate"),
            "LTV": parsed.get("ltv"),
            "CLTV": parsed.get("cltv"),
            "DTI": "unknown",
        },
        "loan_terms": {
            "amortization_type": parsed.get("amortization_type", "unknown"),
            "term_months": parsed.get("loan_term_months"),
            "interest_only": parsed.get("interest_only"),
            "prepay_penalty": parsed.get("prepay_penalty"),
            "balloon": parsed.get("balloon"),
            "lien_priority": parsed.get("lien_priority", "unknown"),
        },
        "credit": {
            "fico": parsed.get("fico"),
            "fico_source": "xml" if parsed.get("fico") else "unknown",
            "mortgage_history_flags": [],
            "credit_events": [],
            "declarations": parsed.get("declarations", {}),
        },
        "borrowers": [
            {
                "name": name,
                "role": "primary" if i == 0 else "co-borrower",
                "self_employed": parsed.get("self_employed"),
                "citizenship": parsed.get("citizenship", "unknown"),
                "military": parsed.get("military"),
            }
            for i, name in enumerate(parsed.get("borrower_names", []))
        ],
        "income_profile": {
            "income_types": income_types,
            "primary_income_type": income_types[0] if income_types else "unknown",
        },
        "asset_profile": {
            "has_bank_statements": "bank_statement" in doc_types,
            "has_large_deposit_flags": any(
                "large_deposit" in (d.get("flags") or []) for d in docs
            ),
            "has_gift_indicators": any(
                "gift" in str(d.get("extracted_fields", {})).lower() for d in docs
            ),
            "has_reserves_indicators": False,
        },
        "reo_summary": {
            "total_properties_owned": len(parsed.get("owned_properties", [])),
            "total_lien_balance": None,
            "subject_property_rental_income": None,
        },
        "doc_profile": list(set(doc_types)),
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

    return Command(update={
        "scenario_summary": summary,
        "missing_core_variables": missing,
        "guideline_section_refs": guideline_refs,
    })


@tool
def detect_contradictions(
    state: Annotated[dict, InjectedToolArg] = None,
) -> Command:
    """
    Compare XML-sourced fields against extracted entity fields.
    Returns a list of contradictions_detected.
    """
    s = state or {}
    ss = s.get("scenario_summary", {})
    parsed: dict = ss.get("_parsed_xml", {})
    entities: dict = ss.get("_extracted_entities", {})
    docs: list[dict] = entities.get("documents", [])

    contradictions = []

    xml_address = parsed.get("property_address", "")
    xml_value = parsed.get("appraised_value")

    for doc in docs:
        fields = doc.get("extracted_fields", {})
        flags = doc.get("flags", [])

        # Name mismatch
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

        # Address mismatch
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

        # Value mismatch (appraisal)
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

    return Command(update={"contradictions_detected": contradictions})


def _safe_float_local(val) -> float | None:
    if val is None:
        return None
    try:
        return float(str(val).replace(",", ""))
    except (ValueError, TypeError):
        return None


@tool
def route_to_facets(
    state: Annotated[dict, InjectedToolArg] = None,
) -> Command:
    """
    Partition extracted entity documents and overlays into per-facet buckets.
    Returns docs_by_facet and overlays_by_facet.
    """
    s = state or {}
    ss = s.get("scenario_summary", {})
    entities: dict = ss.get("_extracted_entities", {})
    docs: list[dict] = entities.get("documents", [])
    overlays: list[dict] = entities.get("overlays", [])

    program = ss.get("program", "unknown") if isinstance(ss, dict) else "unknown"
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
        "business_license": ["income"],
        "lease": ["income"],
        "affidavit": ["compliance"],
        "compliance_notice": ["compliance"],
        "ID": ["crosscutting"],
        "other": ["crosscutting"],
    }

    for doc in docs:
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

    for overlay in overlays:
        oid = overlay.get("overlay_id", "")
        scope = overlay.get("scope", "crosscutting")
        if scope == "program":
            overlays_by_facet["program"].append(oid)
        elif scope in overlays_by_facet:
            overlays_by_facet[scope].append(oid)
        else:
            overlays_by_facet["crosscutting"].append(oid)

    return Command(update={
        "docs_by_facet": docs_by_facet,
        "overlays_by_facet": overlays_by_facet,
    })
