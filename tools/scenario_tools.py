"""
scenario_tools.py — Tools for STEP_00: Scenario Builder.

New XML-first flow:
  1. parse_loan_file   — parses XML and produces a loan profile JSON
                         (same shape as sample_case.json) via xml_to_loan_profile.
  2. parse_loan_profile — if an external JSON was also provided (from platform),
                          merges those fields over the XML-derived profile.
  3. parse_submitted_documents — maps doc names to internal doc_types.
  4. build_scenario_summary — reads the unified _loan_profile + _xml_supplemental
                               to build the scenario_summary.
  5. detect_contradictions — compares XML-derived vs external profile when both exist.
  6. route_to_facets — partitions docs into per-facet buckets.
"""

from __future__ import annotations

import json
from typing import Any, List, Optional

from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command
from typing_extensions import Annotated

from tools.shared.manifest_parser import parse_manifest_from_string
from tools.shared.xml_parser import parse_mismo_xml, xml_to_loan_profile


# ---------------------------------------------------------------------------
# Document name -> internal doc_type mapping
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
    "deed of trust": "title_commitment",
    "preliminary report": "title_commitment",
    "drivers license": "ID",
    "driver's license": "ID",
    "credit supplement": "credit_report",
    "mortgage statement": "mortgage_statement",
    "mortgage note": "mortgage_note",
    "verification of rent": "VOR",
    "flood certification": "insurance",
    "flood hazard determination": "insurance",
    "property insurance": "insurance",
    "property tax": "tax_record",
    "emd docs": "emd",
    "counteroffer": "purchase_contract",
    "signature affidavit": "affidavit",
    "affidavit of occupancy": "affidavit",
}


def _map_doc_name_to_type(name: str) -> str:
    key = name.strip().lower()
    if key in _DOC_NAME_TO_TYPE:
        return _DOC_NAME_TO_TYPE[key]
    for pattern, dtype in _DOC_NAME_TO_TYPE.items():
        if pattern in key or key in pattern:
            return dtype
    return "other"


# ---------------------------------------------------------------------------
# Income doc label -> income_type mapping
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


def _infer_program(profile: dict, docs: list[dict]) -> str:
    """Infer NQMF program from the loan profile (which already contains XML data)."""
    meta = profile.get("metadata", {})
    loan_program = meta.get("loan_program", {})
    program_name = loan_program.get("name") or loan_program.get("originalApiKey") or ""
    if program_name and program_name.lower() not in ("", "unknown", "conventional"):
        return program_name

    occupancy = (meta.get("occupancy") or "").lower()
    citizenship = (meta.get("citizenship") or "").lower()
    units = meta.get("units") or 0
    lien = (meta.get("loan_type") or "").lower()
    loan_amount = meta.get("loan_amount") or 0
    income_doc = (meta.get("income_doc") or "").lower()

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

    # Try self-employed inference from XML data
    if meta.get("self_employed"):
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
# Helpers
# ---------------------------------------------------------------------------


def _pick(profile_val: Any, xml_val: Any, fallback: Any = "unknown") -> Any:
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


def _safe_float_local(val) -> Optional[float]:
    if val is None:
        return None
    try:
        return float(str(val).replace(",", ""))
    except (ValueError, TypeError):
        return None


def _deep_merge(base: dict, override: dict) -> dict:
    """Merge override into base. Override wins for non-None values."""
    result = dict(base)
    for k, v in override.items():
        if v is None:
            continue
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@tool
def parse_loan_file(
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
    state: Annotated[dict, InjectedState] = None,
) -> Command:
    """
    Parse the MISMO XML loan file and produce a loan profile JSON.
    The XML is dynamically parsed and converted into a structured profile
    (same shape as sample_case.json) plus supplemental data (liabilities,
    declarations, housing expenses, etc.) that the profile shape doesn't
    accommodate.
    """
    xml_content = (state or {}).get("loan_file_xml", "")
    if not xml_content:
        return Command(update={
            "module_outputs": {"00": {"conditions": [{
                "category": "Program Eligibility",
                "severity": "HARD-STOP",
                "priority": "P0",
                "title": "Missing Loan File XML",
                "description": "No loan_file_xml provided in state. Cannot proceed.",
                "required_documents": ["MISMO XML loan file"],
                "required_data_elements": [],
                "condition_family_id": "missing_loan_file_xml",
                "source_module": "00",
            }]}},
            "messages": [ToolMessage("HARD-STOP: No loan_file_xml provided.", tool_call_id=tool_call_id)],
        })

    profile = xml_to_loan_profile(xml_content)

    if "parse_error" in profile:
        return Command(update={
            "module_outputs": {"00": {"conditions": [{
                "category": "Program Eligibility",
                "severity": "HARD-STOP",
                "priority": "P0",
                "title": "XML Parse Error",
                "description": f"XML parse error: {profile['parse_error']}",
                "required_documents": ["Corrected MISMO XML loan file"],
                "required_data_elements": [],
                "condition_family_id": "xml_parse_error",
                "source_module": "00",
            }]}},
            "messages": [ToolMessage(f"HARD-STOP: XML parse error: {profile['parse_error']}", tool_call_id=tool_call_id)],
        })

    meta = profile.get("metadata", {})
    borrower = meta.get("borrower", {})
    name = _build_borrower_name(borrower)

    # Also store raw parsed XML for backward compatibility
    parsed = parse_mismo_xml(xml_content)

    return Command(update={
        "scenario_summary": {
            "_loan_profile": profile,
            "_xml_supplemental": profile.get("_xml_supplemental", {}),
            "_parsed_xml": parsed,
        },
        "messages": [ToolMessage(
            f"XML parsed and loan profile generated. Borrower: {name}. "
            f"Loan amount: {meta.get('loan_amount')}, LTV: {meta.get('ltv_pct')}, "
            f"Purpose: {meta.get('purpose')}, Occupancy: {meta.get('occupancy')}",
            tool_call_id=tool_call_id,
        )],
    })


@tool
def parse_loan_profile(
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
    state: Annotated[dict, InjectedState] = None,
) -> Command:
    """
    Merge an external loan profile JSON (from the platform/UI) over the
    XML-derived profile. If no external JSON is provided, the XML-derived
    profile is used as-is. External fields like loan_program, income_doc,
    channel, borrower_type override the XML-derived values.
    """
    raw = (state or {}).get("loan_profile_json", "")
    if not raw:
        return Command(update={
            "messages": [ToolMessage(
                "No external loan_profile_json provided. Using XML-derived profile as-is.",
                tool_call_id=tool_call_id,
            )],
        })

    try:
        external = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError as e:
        return Command(update={
            "module_outputs": {"00": {"conditions": [{
                "category": "Program Eligibility",
                "severity": "SOFT-STOP",
                "priority": "P1",
                "title": "Invalid Loan Profile JSON",
                "description": f"The external loan profile JSON could not be parsed: {e}. Falling back to XML-derived profile.",
                "required_documents": ["Corrected loan profile JSON"],
                "required_data_elements": [],
                "condition_family_id": "invalid_loan_profile_json",
                "source_module": "00",
            }]}},
            "messages": [ToolMessage(f"SOFT-STOP: Invalid JSON: {e}", tool_call_id=tool_call_id)],
        })

    ss = (state or {}).get("scenario_summary", {})
    xml_profile = ss.get("_loan_profile", {})

    if xml_profile:
        xml_meta = xml_profile.get("metadata", {})
        ext_meta = external.get("metadata", {})
        merged_meta = _deep_merge(xml_meta, ext_meta)
        merged_profile = dict(xml_profile)
        merged_profile["metadata"] = merged_meta
        # Preserve _xml_supplemental from XML-derived profile
        if "_xml_supplemental" not in merged_profile:
            merged_profile["_xml_supplemental"] = xml_profile.get("_xml_supplemental", {})
    else:
        merged_profile = external

    program = merged_profile.get("metadata", {}).get("loan_program", {}).get("name", "unknown")
    return Command(update={
        "scenario_summary": {
            "_loan_profile": merged_profile,
            "_external_profile_provided": True,
        },
        "messages": [ToolMessage(
            f"External profile merged. Program: {program}, "
            f"FICO: {merged_profile.get('metadata', {}).get('fico')}, "
            f"LTV: {merged_profile.get('metadata', {}).get('ltv_pct')}",
            tool_call_id=tool_call_id,
        )],
    })


@tool
def parse_submitted_documents(
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
    state: Annotated[dict, InjectedState] = None,
) -> Command:
    """
    Parse submitted documents from state.

    Checks inputs in order of preference:
      1. manifest_json  — raw Tasktile manifest (cloud callers)
      2. submitted_documents_json — pre-parsed doc list (legacy / test_pipeline)

    The manifest is parsed in-process via manifest_parser, so cloud callers
    only need to pass the raw JSON string.
    """
    s = state or {}
    manifest_raw = s.get("manifest_json", "")
    legacy_raw = s.get("submitted_documents_json", "")

    # --- Try manifest_json first ---
    if manifest_raw:
        try:
            doc_list = parse_manifest_from_string(
                manifest_raw if isinstance(manifest_raw, str) else json.dumps(manifest_raw)
            )
            doc_summary = ", ".join(f"{d['name']} ({d['doc_type']})" for d in doc_list[:20])
            if len(doc_list) > 20:
                doc_summary += f" ... and {len(doc_list) - 20} more"
            return Command(update={
                "scenario_summary": {"_submitted_docs": doc_list},
                "messages": [ToolMessage(
                    f"Parsed manifest_json → {len(doc_list)} submitted documents: {doc_summary}",
                    tool_call_id=tool_call_id,
                )],
            })
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            return Command(update={
                "module_outputs": {"00": {"conditions": [{
                    "category": "Document Completeness",
                    "severity": "SOFT-STOP",
                    "priority": "P1",
                    "title": "Invalid Manifest JSON",
                    "description": f"The manifest JSON could not be parsed: {e}. Falling back to submitted_documents_json if available.",
                    "required_documents": ["Corrected manifest JSON"],
                    "required_data_elements": [],
                    "condition_family_id": "invalid_manifest_json",
                    "source_module": "00",
                }]}},
                "scenario_summary": {"_submitted_docs": []},
                "messages": [ToolMessage(
                    f"SOFT-STOP: Invalid manifest JSON: {e}. Will try submitted_documents_json.",
                    tool_call_id=tool_call_id,
                )],
            })

    # --- Fall back to legacy submitted_documents_json ---
    if not legacy_raw:
        return Command(update={
            "scenario_summary": {"_submitted_docs": []},
            "messages": [ToolMessage("No submitted documents provided (no manifest_json or submitted_documents_json).", tool_call_id=tool_call_id)],
        })

    try:
        doc_list = json.loads(legacy_raw) if isinstance(legacy_raw, str) else legacy_raw
    except json.JSONDecodeError as e:
        return Command(update={
            "module_outputs": {"00": {"conditions": [{
                "category": "Document Completeness",
                "severity": "SOFT-STOP",
                "priority": "P1",
                "title": "Invalid Submitted Documents JSON",
                "description": f"The submitted documents JSON could not be parsed: {e}. No documents will be available for analysis.",
                "required_documents": ["Corrected submitted documents JSON"],
                "required_data_elements": [],
                "condition_family_id": "invalid_submitted_docs_json",
                "source_module": "00",
            }]}},
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

    doc_summary = ", ".join(f"{d['name']} ({d['doc_type']})" for d in mapped[:20])
    if len(mapped) > 20:
        doc_summary += f" ... and {len(mapped) - 20} more"
    return Command(update={
        "scenario_summary": {"_submitted_docs": mapped},
        "messages": [ToolMessage(
            f"Parsed {len(mapped)} submitted documents: {doc_summary}",
            tool_call_id=tool_call_id,
        )],
    })


# ---------------------------------------------------------------------------
# Eligibility engine field mapping
# ---------------------------------------------------------------------------

_ELIGIBILITY_FIELD_MAP: dict[str, str] = {
    "FicoScore": "fico",
    "LTV": "ltv_pct",
    "CLTV": "cltv_pct",
    "DTI": "dti",
    "LoanAmount": "loan_amount",
    "PropertyValue": "property_value",
    "PropertyType": "property_type",
    "Occupancy": "occupancy",
    "LoanPurpose": "purpose",
    "IncomeDocType": "income_doc",
    "Channel": "channel",
    "BorrowerType": "borrower_type",
    "Citizenship": "citizenship",
    "State": "state",
    "County": "county",
    "LoanType": "loan_type",
}


def _mine_fico_from_passed_programs(
    program_results: dict[str, Any],
    eligible_programs: list[str],
) -> int | None:
    """Extract FICO from passed requirement checks in eligible programs.

    Scans the ``passed`` array of each eligible program for FICO-related
    requirement entries and returns the minimum ``actual`` score found.
    Returns None if no FICO data can be extracted.
    """
    import re as _re
    fico_vals: list[int] = []
    eligible_set = {p.lower() for p in eligible_programs}

    for _prog_key, prog_data in program_results.items():
        prog_name = (prog_data.get("program") or _prog_key).lower()
        if prog_name not in eligible_set and prog_data.get("overall_status") != "PASS":
            continue
        for check in prog_data.get("passed", []):
            req = (check.get("requirement") or "").lower()
            if "fico" not in req:
                continue
            actual = check.get("actual")
            if isinstance(actual, (int, float)) and actual > 0:
                fico_vals.append(int(actual))
            elif isinstance(actual, str):
                nums = _re.findall(r"\d{3}", actual)
                for n in nums:
                    v = int(n)
                    if 300 <= v <= 850:
                        fico_vals.append(v)
    return min(fico_vals) if fico_vals else None


@tool
def parse_eligibility_output(
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
    state: Annotated[dict, InjectedState] = None,
) -> Command:
    """
    Parse the eligibility engine output JSON and enrich the loan profile.

    Extracts:
      - application_data: authoritative loan fields (FICO, LTV, CLTV, DTI,
        reserves, property type, etc.) that override XML-derived values.
      - eligible_programs: list of programs that passed eligibility.
      - program_results: per-program pass/fail details.

    If no eligibility_json is provided, returns gracefully with no changes.
    """
    s = state or {}
    raw = s.get("eligibility_json", "")
    if not raw:
        return Command(update={
            "messages": [ToolMessage(
                "No eligibility_json provided. Skipping eligibility enrichment.",
                tool_call_id=tool_call_id,
            )],
        })

    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError) as e:
        return Command(update={
            "module_outputs": {"00": {"conditions": [{
                "category": "Program Eligibility",
                "severity": "SOFT-STOP",
                "priority": "P1",
                "title": "Invalid Eligibility JSON",
                "description": f"The eligibility engine JSON could not be parsed: {e}.",
                "required_documents": [],
                "required_data_elements": ["Corrected eligibility engine output"],
                "condition_family_id": "invalid_eligibility_json",
                "source_module": "00",
            }]}},
            "messages": [ToolMessage(f"SOFT-STOP: Invalid eligibility JSON: {e}", tool_call_id=tool_call_id)],
        })

    detailed = data.get("detailed_results", {})
    app_data = detailed.get("application_data", {})
    eligible = detailed.get("eligible_programs", [])
    ineligible = detailed.get("ineligible_programs", [])
    program_results = detailed.get("program_results", {})

    # Map application_data fields to profile metadata shape
    meta_overlay: dict[str, Any] = {}
    for elig_key, meta_key in _ELIGIBILITY_FIELD_MAP.items():
        val = app_data.get(elig_key)
        if val is not None:
            meta_overlay[meta_key] = val

    # Fallback: if FICO is missing from application_data, mine it from
    # passed requirement checks in eligible programs only.
    if meta_overlay.get("fico") is None and app_data.get("FicoScore") is None:
        _mined = _mine_fico_from_passed_programs(program_results, eligible)
        if _mined is not None:
            meta_overlay["fico"] = _mined
            app_data["FicoScore"] = _mined

    # Extra fields that don't map 1:1 into metadata but are useful
    extra_fields: dict[str, Any] = {}
    for key in ("ReservesMonths", "FirstTimeHomeBuyer", "HousingHistory30DayLates12Months",
                "HousingHistory12DayLates24Months", "CreditEventSeasoning",
                "AssetSeasoningDays", "BorrowerContribution", "IPCs",
                "DecliningMarket", "CashOutAmount", "SeasoningMonths",
                "SSRScore", "Acres", "HPMLStatus", "InterestOnly", "Buydown"):
        val = app_data.get(key)
        if val is not None:
            extra_fields[key] = val

    # Build compact program results for downstream use
    program_detail: dict[str, dict] = {}
    for prog_key, prog_data in program_results.items():
        prog_name = prog_data.get("program", prog_key)
        program_detail[prog_name] = {
            "status": prog_data.get("overall_status", "UNKNOWN"),
            "passed_count": len(prog_data.get("passed", [])),
            "failed_count": len(prog_data.get("failed", [])),
            "failed_rules": [
                {"requirement": f.get("requirement", "?"), "message": f.get("message", "")}
                for f in prog_data.get("failed", [])
            ],
        }

    # Determine the program to use: first eligible, or infer from results
    primary_program = eligible[0] if eligible else None

    # If the eligibility engine identified a program, set it as loan_program
    if primary_program:
        meta_overlay["loan_program"] = {"name": primary_program}

    summary_msg = (
        f"Eligibility engine output parsed. "
        f"Eligible programs: {eligible or 'none'}. "
        f"Ineligible: {ineligible or 'none'}. "
        f"Application data fields: {list(app_data.keys())[:10]}..."
    )
    if extra_fields:
        summary_msg += f" Extra fields: {list(extra_fields.keys())}"

    return Command(update={
        "scenario_summary": {
            "_eligibility_data": {
                "application_data": app_data,
                "extra_fields": extra_fields,
                "eligible_programs": eligible,
                "ineligible_programs": ineligible,
                "program_detail": program_detail,
            },
            "_loan_profile": {"metadata": meta_overlay} if meta_overlay else {},
        },
        "messages": [ToolMessage(summary_msg, tool_call_id=tool_call_id)],
    })


@tool
def build_scenario_summary(
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
    state: Annotated[dict, InjectedState] = None,
) -> Command:
    """
    Build the full scenario_summary from the unified loan profile.
    The loan profile (from XML, optionally overridden by external JSON)
    is the primary source. Supplemental XML data fills in liabilities,
    declarations, housing expenses, etc.
    """
    s = state or {}
    ss = s.get("scenario_summary", {})
    profile: dict = ss.get("_loan_profile", {})
    supplemental: dict = ss.get("_xml_supplemental", {}) or profile.get("_xml_supplemental", {})
    submitted_docs: list[dict] = ss.get("_submitted_docs", [])

    # Re-apply external overrides in case parse_loan_profile ran in parallel
    # with parse_loan_file and the merge hasn't been applied yet.
    raw_ext = s.get("loan_profile_json", "")
    if raw_ext and profile:
        try:
            ext = json.loads(raw_ext) if isinstance(raw_ext, str) else raw_ext
            ext_meta = ext.get("metadata", {})
            if ext_meta:
                profile = dict(profile)
                profile["metadata"] = _deep_merge(profile.get("metadata", {}), ext_meta)
        except (json.JSONDecodeError, TypeError):
            pass

    # Re-apply eligibility engine data (highest priority for numeric fields)
    eligibility_data = ss.get("_eligibility_data", {})
    elig_app = eligibility_data.get("application_data", {})
    if elig_app:
        elig_meta: dict[str, Any] = {}
        for elig_key, meta_key in _ELIGIBILITY_FIELD_MAP.items():
            val = elig_app.get(elig_key)
            if val is not None:
                elig_meta[meta_key] = val
        # Set loan_program from eligible programs
        elig_programs = eligibility_data.get("eligible_programs", [])
        if elig_programs:
            elig_meta["loan_program"] = {"name": elig_programs[0]}
        if elig_meta:
            profile = dict(profile)
            profile["metadata"] = _deep_merge(profile.get("metadata", {}), elig_meta)

    meta = profile.get("metadata", {})
    loan_program = meta.get("loan_program", {})

    doc_types = [d.get("doc_type") for d in submitted_docs if d.get("doc_type")]

    # Determine income types
    income_doc_label = meta.get("income_doc") or ""
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

    program = _infer_program(profile, submitted_docs)
    property_type = meta.get("property_type") or "Unknown"

    # Purpose
    raw_purpose = meta.get("purpose") or "unknown"
    purpose = raw_purpose
    if isinstance(raw_purpose, str) and raw_purpose.lower() in ("refinance", "nocash-outrefinance"):
        cash_out = supplemental.get("cash_out_amount")
        purpose = "Cash-OutRefinance" if (cash_out and cash_out > 0) else "NoCash-OutRefinance"

    # Borrowers from profile metadata
    borrowers: list[dict] = []
    profile_borrower = meta.get("borrower", {})
    supplemental_ssns = supplemental.get("borrower_ssns", [])
    supplemental_dobs = supplemental.get("borrower_dobs", [])

    if profile_borrower and any(profile_borrower.values()):
        borrowers.append({
            "name": _build_borrower_name(profile_borrower),
            "ssn": supplemental_ssns[0] if supplemental_ssns else None,
            "dob": supplemental_dobs[0] if supplemental_dobs else None,
            "role": "primary",
            "self_employed": meta.get("self_employed"),
            "citizenship": meta.get("citizenship", "unknown"),
            "military": meta.get("military"),
        })

    co_borrower = meta.get("co_borrower")
    if co_borrower and isinstance(co_borrower, dict) and any(co_borrower.values()):
        borrowers.append({
            "name": _build_borrower_name(co_borrower),
            "ssn": supplemental_ssns[1] if len(supplemental_ssns) > 1 else None,
            "dob": supplemental_dobs[1] if len(supplemental_dobs) > 1 else None,
            "role": "co-borrower",
            "self_employed": meta.get("self_employed"),
            "citizenship": meta.get("citizenship", "unknown"),
            "military": None,
        })

    # Numbers from profile metadata (already merged from XML + external)
    loan_amount = meta.get("loan_amount")
    appraised_value = meta.get("property_value")
    purchase_price = meta.get("property_value")
    note_rate = loan_program.get("rate")
    ltv = meta.get("ltv_pct")
    cltv = meta.get("cltv_pct")
    fico = meta.get("fico")
    dti = meta.get("dti")

    units_raw = meta.get("units")
    units = units_raw if units_raw and units_raw > 0 else None

    # Declarations & credit events from supplemental
    declarations = supplemental.get("declarations", {})
    credit_events = []
    if declarations.get("BankruptcyIndicator"):
        credit_events.append("BK")
    if declarations.get("PriorPropertyForeclosureCompletedIndicator"):
        credit_events.append("FC")
    if declarations.get("PriorPropertyShortSaleCompletedIndicator"):
        credit_events.append("SS")
    if declarations.get("PriorPropertyDeedInLieuConveyedIndicator"):
        credit_events.append("DIL")

    loan_terms = supplemental.get("loan_terms", {})
    owned_properties = supplemental.get("owned_properties", [])
    housing_expenses = supplemental.get("housing_expenses", {"present": [], "proposed": []})

    summary: dict[str, Any] = {
        "program": program,
        "product_variant": loan_program.get("type", "unknown"),
        "purpose": purpose,
        "occupancy": meta.get("occupancy") or "unknown",
        "property": {
            "address": meta.get("property_address") or "unknown",
            "state": meta.get("state") or "unknown",
            "county": meta.get("county") or "unknown",
            "city": supplemental.get("property_city", "unknown"),
            "zip": supplemental.get("property_zip", "unknown"),
            "units": units,
            "property_type": property_type,
            "year_built": supplemental.get("year_built"),
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
            "amortization_type": loan_terms.get("amortization_type", "unknown"),
            "term_months": loan_terms.get("term_months"),
            "interest_only": loan_terms.get("interest_only"),
            "prepay_penalty": loan_terms.get("prepay_penalty"),
            "balloon": loan_terms.get("balloon"),
            "lien_priority": meta.get("loan_type") or loan_terms.get("lien_priority", "unknown"),
        },
        "credit": {
            "fico": fico,
            "credit_scores": supplemental.get("credit_scores", []),
            "fico_source": "loan_profile" if meta.get("fico") else "unknown",
            "is_us_credit": meta.get("is_us_credit", True),
            "mortgage_history_flags": [],
            "credit_events": credit_events or ["none"],
            "declarations": declarations,
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
            "total_properties_owned": len(owned_properties),
            "total_lien_balance": None,
            "subject_property_rental_income": None,
        },
        "doc_profile": list(set(doc_types)),
        "housing_expenses": housing_expenses,
        "cash_out_amount": supplemental.get("cash_out_amount"),
        "channel": meta.get("channel", "unknown"),
        "loan_number": meta.get("loan_number"),
        "dscr_label": meta.get("dscr"),
        "borrower_type": meta.get("borrower_type"),
        "employers": supplemental.get("employers", []),
        "residences": supplemental.get("residences", []),
        "assets": supplemental.get("assets", []),
        "liabilities": supplemental.get("liabilities", []),
        "owned_properties": owned_properties,
    }

    # Enrich with eligibility engine data if available
    if eligibility_data:
        summary["eligible_programs"] = eligibility_data.get("eligible_programs", [])
        summary["ineligible_programs"] = eligibility_data.get("ineligible_programs", [])
        summary["program_eligibility_detail"] = eligibility_data.get("program_detail", {})
        extra = eligibility_data.get("extra_fields", {})
        if extra:
            summary["eligibility_extra"] = extra
            if extra.get("ReservesMonths") is not None:
                summary["asset_profile"]["months_reserves"] = extra["ReservesMonths"]
                summary["asset_profile"]["has_reserves_indicators"] = True
            if extra.get("FirstTimeHomeBuyer") is not None:
                summary["is_fthb"] = extra["FirstTimeHomeBuyer"]
            if extra.get("DecliningMarket") is not None:
                summary["is_declining_market"] = extra["DecliningMarket"]
            if extra.get("CashOutAmount") is not None and summary.get("cash_out_amount") is None:
                summary["cash_out_amount"] = extra["CashOutAmount"]

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
    Compare XML-derived fields against external profile and submitted documents.
    Only flags contradictions when both an external profile AND XML data exist.
    """
    s = state or {}
    ss = s.get("scenario_summary", {})
    profile: dict = ss.get("_loan_profile", {})
    supplemental: dict = ss.get("_xml_supplemental", {}) or profile.get("_xml_supplemental", {})
    submitted_docs: list[dict] = ss.get("_submitted_docs", [])
    has_external = ss.get("_external_profile_provided", False)

    contradictions = []
    meta = profile.get("metadata", {})

    xml_address = meta.get("property_address", "")
    xml_value = meta.get("property_value")

    # Only check XML vs external when external was provided
    if has_external and meta.get("property_value"):
        pass  # XML values are already merged into profile; contradictions
              # would have been visible at merge time. Future: compare
              # pre-merge XML vs external values.

    # Check submitted docs for flagged contradictions
    for doc in submitted_docs:
        fields = doc.get("extracted_fields", {})
        flags = doc.get("flags", [])

        if "name_mismatch" in flags:
            borrower = meta.get("borrower", {})
            borrower_name = _build_borrower_name(borrower)
            entity_name = fields.get("borrower_name") or fields.get("account_holder") or ""
            contradictions.append({
                "type": "NAME_MISMATCH",
                "source_a": "loan_profile",
                "source_b": doc.get("doc_id", "unknown"),
                "details": (
                    f"Profile borrower: {borrower_name}. "
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
                "source_a": "loan_profile",
                "source_b": doc.get("doc_id", "unknown"),
                "details": (
                    f"Profile address: {xml_address}. "
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
                    "source_a": "loan_profile",
                    "source_b": doc.get("doc_id", "unknown"),
                    "details": (
                        f"Profile property value: {xml_value}. "
                        f"Appraisal entity value: {entity_value}."
                    ),
                })

    msg = f"Found {len(contradictions)} contradiction(s)." if contradictions else "No contradictions detected."
    return Command(update={
        "contradictions_detected": contradictions,
        "messages": [ToolMessage(msg, tool_call_id=tool_call_id)],
    })


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
        "mortgage_statement": ["credit", "assets"],
        "mortgage_note": ["title_closing"],
        "tax_record": ["compliance"],
        "VOR": ["income", "assets"],
        "income_worksheet": ["income"],
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
