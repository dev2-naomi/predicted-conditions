"""
xml_parser.py — Dynamic MISMO XML loan file parser.

Two-layer architecture:
  1. Generic recursive extraction  — walks the entire XML tree and captures
     every leaf value, grouped by MISMO parent container.
  2. Semantic field map             — declarative dict that maps canonical
     output field names to a prioritized list of (section, tag) candidates.

Repeating elements (liabilities, parties, assets, housing expenses, owned
properties) are collected as lists of dicts with ALL leaf values from each
element.

Public API:
  - ``parse_mismo_xml(xml_content)`` — returns the full parsed dict (same
    contract as before, plus ``raw_xml``).
  - ``xml_to_loan_profile(xml_content)`` — returns a dict shaped like
    sample_case.json ``metadata`` with an ``_xml_supplemental`` key for
    data that doesn't fit the profile shape.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NS_RE = re.compile(r"\{[^}]*\}")


def _strip_ns(tag: str) -> str:
    return _NS_RE.sub("", tag)


def _is_leaf(elem: ET.Element) -> bool:
    return len(elem) == 0


def _safe_float(s: Optional[str]) -> Optional[float]:
    if s is None:
        return None
    try:
        return float(s.replace(",", "").replace("%", ""))
    except ValueError:
        return None


def _bool_val(s: Optional[str]) -> Optional[bool]:
    if s is None:
        return None
    return s.strip().lower() in ("true", "yes", "1")


# ---------------------------------------------------------------------------
# Layer 1 — Generic recursive extraction
# ---------------------------------------------------------------------------

_REPEATING_CONTAINERS = frozenset({
    "LIABILITY", "ASSET", "PARTY", "HOUSING_EXPENSE",
    "OWNED_PROPERTY", "RESIDENCE", "EMPLOYER",
    "CREDIT_SCORE", "CREDIT_SCORE_DETAIL",
    "CURRENT_INCOME_ITEM", "PURCHASE_CREDIT",
    "CLOSING_ADJUSTMENT_ITEM", "COUNSELING_EVENT",
    "LOAN_IDENTIFIER",
})


def _collect_leaves(elem: ET.Element) -> Dict[str, str]:
    """Recursively collect all leaf text values under *elem*."""
    leaves: Dict[str, str] = {}
    for child in elem.iter():
        if child is elem:
            continue
        tag = _strip_ns(child.tag)
        if _is_leaf(child) and child.text and child.text.strip():
            leaves[tag] = child.text.strip()
    return leaves


def _extract_sections(root: ET.Element) -> Dict[str, Any]:
    """Walk the full XML tree and produce a section-grouped dict.

    * Singleton containers  -> ``{"TERMS_OF_LOAN": {"BaseLoanAmount": "..."}}``
    * Repeating containers  -> ``{"LIABILITY": [{"LiabilityType": "..."}, ...]}``
    """
    sections: Dict[str, Any] = {}

    for elem in root.iter():
        tag = _strip_ns(elem.tag)
        if _is_leaf(elem):
            continue

        if tag in _REPEATING_CONTAINERS:
            leaves = _collect_leaves(elem)
            if leaves:
                sections.setdefault(tag, []).append(leaves)
            continue

        for child in elem:
            child_tag = _strip_ns(child.tag)
            if _is_leaf(child) and child.text and child.text.strip():
                sections.setdefault(tag, {})
                if isinstance(sections[tag], dict):
                    sections[tag][child_tag] = child.text.strip()

    return sections


# ---------------------------------------------------------------------------
# Layer 2 — Semantic field map
# ---------------------------------------------------------------------------

_FIELD_MAP: Dict[str, List[tuple]] = {
    "loan_id": [
        ("LOAN_IDENTIFIER", "LoanIdentifier"),
    ],
    "purpose": [
        ("TERMS_OF_LOAN", "LoanPurposeType"),
        ("TERMS_OF_MORTGAGE", "LoanPurposeType"),
    ],
    "mortgage_type": [
        ("TERMS_OF_LOAN", "MortgageType"),
        ("TERMS_OF_MORTGAGE", "MortgageType"),
    ],
    "loan_amount": [
        ("TERMS_OF_LOAN", "BaseLoanAmount"),
        ("TERMS_OF_LOAN", "NoteAmount"),
        ("TERMS_OF_MORTGAGE", "NoteAmount"),
        ("TERMS_OF_MORTGAGE", "BaseLoanAmount"),
        ("URLA_DETAIL", "BorrowerRequestedLoanAmount"),
    ],
    "note_rate": [
        ("TERMS_OF_LOAN", "NoteRatePercent"),
        ("TERMS_OF_MORTGAGE", "NoteRatePercent"),
    ],
    "lien_priority": [
        ("TERMS_OF_LOAN", "LienPriorityType"),
        ("TERMS_OF_MORTGAGE", "LienPriorityType"),
    ],
    "amortization_type": [
        ("AMORTIZATION_RULE", "AmortizationType"),
        ("AMORTIZATION_RULE", "LoanAmortizationType"),
    ],
    "loan_term_months": [
        ("AMORTIZATION_RULE", "LoanAmortizationPeriodCount"),
        ("MATURITY_RULE", "LoanMaturityPeriodCount"),
    ],
    "interest_only": [
        ("LOAN_DETAIL", "InterestOnlyIndicator"),
    ],
    "prepay_penalty": [
        ("LOAN_DETAIL", "PrepaymentPenaltyIndicator"),
    ],
    "balloon": [
        ("LOAN_DETAIL", "BalloonIndicator"),
    ],
    "borrower_count_raw": [
        ("LOAN_DETAIL", "BorrowerCount"),
    ],
    "negative_amortization": [
        ("LOAN_DETAIL", "NegativeAmortizationIndicator"),
    ],
    "construction_loan": [
        ("LOAN_DETAIL", "ConstructionLoanIndicator"),
    ],
    "renovation_loan": [
        ("LOAN_DETAIL", "RenovationLoanIndicator"),
    ],
    "total_mortgaged_properties": [
        ("LOAN_DETAIL", "TotalMortgagedPropertiesCount"),
    ],
    "cash_out_amount": [
        ("REFINANCE", "RefinanceCashOutAmount"),
    ],
    "occupancy": [
        ("PROPERTY_DETAIL", "PropertyUsageType"),
        ("SUBJECT_PROPERTY_DETAIL", "PropertyUsageType"),
    ],
    "property_state": [
        ("ADDRESS", "StateCode"),
    ],
    "property_county": [
        ("ADDRESS", "CountyName"),
    ],
    "property_city": [
        ("ADDRESS", "CityName"),
    ],
    "property_zip": [
        ("ADDRESS", "PostalCode"),
    ],
    "property_address": [
        ("ADDRESS", "AddressLineText"),
    ],
    "year_built": [
        ("PROPERTY_DETAIL", "PropertyStructureBuiltYear"),
    ],
    "units": [
        ("PROPERTY_DETAIL", "FinancedUnitCount"),
    ],
    "pud_indicator": [
        ("PROPERTY_DETAIL", "PUDIndicator"),
    ],
    "in_project_indicator": [
        ("PROPERTY_DETAIL", "PropertyInProjectIndicator"),
    ],
    "mixed_use_indicator": [
        ("PROPERTY_DETAIL", "PropertyMixedUsageIndicator"),
    ],
    "attachment_type": [
        ("PROPERTY_DETAIL", "AttachmentType"),
    ],
    "appraised_value": [
        ("PROPERTY_VALUATION_DETAIL", "PropertyValuationAmount"),
        ("PROPERTY_DETAIL", "PropertyEstimatedValueAmount"),
    ],
    "purchase_price": [
        ("SALES_CONTRACT_DETAIL", "SalesContractAmount"),
        ("URLA_DETAIL", "PurchasePriceAmount"),
    ],
    "ltv_explicit": [
        ("LTV", "LTVRatioPercent"),
    ],
    "cltv_explicit": [
        ("COMBINED_LTV", "CombinedLTVRatioPercent"),
    ],
    "total_monthly_income": [
        ("QUALIFICATION", "TotalMonthlyIncomeAmount"),
    ],
    "total_monthly_liabilities": [
        ("QUALIFICATION", "TotalLiabilitiesMonthlyPaymentAmount"),
    ],
    "proposed_housing_expense": [
        ("QUALIFICATION", "TotalMonthlyProposedHousingExpenseAmount"),
        ("HOUSING_EXPENSE_SUMMARY", "HousingExpenseProposedTotalMonthlyPaymentAmount"),
    ],
    "citizenship": [
        ("DECLARATION_DETAIL", "CitizenshipResidencyType"),
    ],
    "military": [
        ("BORROWER_DETAIL", "SelfDeclaredMilitaryServiceIndicator"),
    ],
    "mi_coverage": [
        ("MI_DATA_DETAIL", "MICoveragePercent"),
    ],
    "application_date": [
        ("LOAN_DETAIL", "ApplicationReceivedDate"),
    ],
    "cash_from_borrower": [
        ("CLOSING_INFORMATION_DETAIL", "CashFromBorrowerAtClosingAmount"),
    ],
    "estimated_closing_costs": [
        ("URLA_DETAIL", "EstimatedClosingCostsAmount"),
    ],
    "prepaid_items": [
        ("URLA_DETAIL", "PrepaidItemsEstimatedAmount"),
    ],
    "property_estate_type": [
        ("PROPERTY_DETAIL", "PropertyEstateType"),
    ],
    "construction_method": [
        ("PROPERTY_DETAIL", "ConstructionMethodType"),
    ],
    "community_property_state": [
        ("PROPERTY_DETAIL", "CommunityPropertyStateIndicator"),
    ],
    "hmda_rate_spread": [
        ("HMDA_LOAN_DETAIL", "HMDARateSpreadPercent"),
    ],
}


def _resolve_scalar(
    sections: Dict[str, Any],
    candidates: List[tuple],
) -> Optional[str]:
    for section, tag in candidates:
        bucket = sections.get(section)
        if bucket is None:
            continue
        if isinstance(bucket, dict):
            val = bucket.get(tag)
            if val:
                return val
        elif isinstance(bucket, list):
            for item in bucket:
                val = item.get(tag)
                if val:
                    return val
    return None


def _resolve_str(
    sections: Dict[str, Any], field: str, default: str = "unknown",
) -> str:
    candidates = _FIELD_MAP.get(field, [])
    return _resolve_scalar(sections, candidates) or default


def _resolve_float(
    sections: Dict[str, Any], field: str,
) -> Optional[float]:
    candidates = _FIELD_MAP.get(field, [])
    return _safe_float(_resolve_scalar(sections, candidates))


def _resolve_bool(
    sections: Dict[str, Any], field: str,
) -> Optional[bool]:
    candidates = _FIELD_MAP.get(field, [])
    return _bool_val(_resolve_scalar(sections, candidates))


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------


def detect_format(root: ET.Element) -> str:
    tag = _strip_ns(root.tag)
    if tag in ("LOAN_APPLICATION", "MESSAGE"):
        return "ilad2"
    if tag in ("DEAL_SETS", "DEAL_SET", "DEAL", "LOANS"):
        return "fnm3"
    for elem in root.iter():
        if _strip_ns(elem.tag) == "URLA_DETAIL":
            return "fnm3"
    return "ilad2"


# ---------------------------------------------------------------------------
# Property type derivation
# ---------------------------------------------------------------------------


def _derive_property_type(
    unit_count: Optional[int],
    pud: Optional[bool],
    in_project: Optional[bool],
    mixed_use: Optional[bool],
    attachment_type: Optional[str],
) -> str:
    if mixed_use:
        return "Mixed-Use"
    if unit_count is not None:
        if unit_count >= 5:
            return "Multi 5-8"
        if 2 <= unit_count <= 4:
            return "2-4 Unit"
        if unit_count == 1:
            if pud:
                return "PUD"
            if in_project:
                return "Condo"
            return "SFR"
    return "Unknown"


# ---------------------------------------------------------------------------
# Layer 3 — Repeating element extractors (field-agnostic)
# ---------------------------------------------------------------------------

def _extract_borrowers(sections: Dict[str, Any]) -> dict:
    parties = sections.get("PARTY", [])
    borrower_parties = [
        p for p in parties
        if p.get("PartyRoleType", "").lower() == "borrower"
    ]

    names: List[str] = []
    ssns: List[str] = []
    dobs: List[str] = []
    self_employed_flags: List[bool] = []
    borrower_details: List[Dict] = []

    for party in borrower_parties:
        first = party.get("FirstName", "")
        last = party.get("LastName", "")
        middle = party.get("MiddleName", "")
        suffix = party.get("SuffixName", "")
        full_name = " ".join(filter(None, [first, middle, last, suffix]))
        if full_name:
            names.append(full_name)

        ssn = party.get("TaxpayerIdentifierValue")
        if ssn:
            ssns.append(ssn)

        dob = party.get("BorrowerBirthDate")
        if dob:
            dobs.append(dob)

        se = party.get("EmploymentBorrowerSelfEmployedIndicator")
        if se:
            self_employed_flags.append(_bool_val(se) or False)

        borrower_details.append({
            "first_name": first,
            "middle_name": middle,
            "last_name": last,
            "suffix": suffix,
            "dob": dob,
            "ssn": ssn,
            "self_employed": _bool_val(se) if se else None,
            "employer": party.get("FullName"),
            "position": party.get("EmploymentPositionDescription"),
            "employment_start": party.get("EmploymentStartDate"),
            "marital_status": party.get("MaritalStatusType"),
            "dependents": _safe_float(party.get("DependentCount")),
        })

    return {
        "names": names,
        "ssns": ssns,
        "dobs": dobs,
        "self_employed": any(self_employed_flags) if self_employed_flags else None,
        "details": borrower_details,
    }


def _extract_liabilities(sections: Dict[str, Any]) -> List[Dict]:
    raw = sections.get("LIABILITY", [])
    result = []
    for item in raw:
        result.append({
            "type": item.get("LiabilityType"),
            "monthly_payment": _safe_float(item.get("LiabilityMonthlyPaymentAmount")),
            "unpaid_balance": _safe_float(item.get("LiabilityUnpaidBalanceAmount")),
            "payoff_status": item.get("LiabilityPayoffStatusIndicator"),
            "holder": item.get("FullName"),
            "excluded": _bool_val(item.get("LiabilityExclusionIndicator")),
            "remaining_months": _safe_float(item.get("LiabilityRemainingTermMonthsCount")),
            "account_id": item.get("LiabilityAccountIdentifier"),
        })
    return result


def _extract_owned_properties(sections: Dict[str, Any]) -> List[Dict]:
    raw = sections.get("OWNED_PROPERTY", [])
    owned = []
    for item in raw:
        owned.append({
            "address": item.get("AddressLineText"),
            "value": _safe_float(
                item.get("PropertyEstimatedValueAmount")
                or item.get("PropertyCurrentValueAmount")
            ),
            "lien_upb": _safe_float(
                item.get("OwnedPropertyLienUPBAmount")
                or item.get("LiabilityUnpaidBalanceAmount")
            ),
            "rental_income": _safe_float(
                item.get("OwnedPropertyRentalIncomeNetAmount")
                or item.get("OwnedPropertyRentalIncomeGrossAmount")
                or item.get("RentalEstimatedMonthlyRentAmount")
            ),
            "disposition": (
                item.get("OwnedPropertyDispositionStatusType")
                or item.get("PropertyDispositionType")
            ),
            "is_subject": _bool_val(
                item.get("OwnedPropertySubjectIndicator")
                or item.get("SubjectIndicator")
            ),
            "property_usage": item.get("PropertyCurrentUsageType") or item.get("PropertyUsageType"),
            "state": item.get("StateCode"),
            "city": item.get("CityName"),
        })
    return owned


def _extract_housing_expenses(sections: Dict[str, Any]) -> Dict[str, List[Dict]]:
    raw = sections.get("HOUSING_EXPENSE", [])
    expenses: Dict[str, List[Dict]] = {"present": [], "proposed": []}
    for item in raw:
        timing = (item.get("HousingExpenseTimingType") or "").lower()
        entry = {
            "type": item.get("HousingExpenseType", ""),
            "amount": _safe_float(item.get("HousingExpensePaymentAmount")),
        }
        if timing == "present":
            expenses["present"].append(entry)
        elif timing == "proposed":
            expenses["proposed"].append(entry)
    return expenses


def _extract_credit_scores(sections: Dict[str, Any]) -> List[Dict]:
    raw = sections.get("CREDIT_SCORE_DETAIL", []) or sections.get("CREDIT_SCORE", [])
    scores = []
    for item in raw:
        score_val = _safe_float(item.get("CreditScoreValue"))
        if score_val and score_val > 0:
            scores.append({
                "score": int(score_val),
                "source": item.get("CreditRepositorySourceType"),
                "model": item.get("CreditScoreModelType"),
            })
    return scores


def _extract_assets(sections: Dict[str, Any]) -> List[Dict]:
    raw = sections.get("ASSET", [])
    assets = []
    for item in raw:
        assets.append({
            "type": item.get("AssetType"),
            "value": _safe_float(item.get("AssetCashOrMarketValueAmount")),
            "account_id": item.get("AssetAccountIdentifier"),
            "holder": item.get("FullName"),
        })
    return assets


def _extract_declarations(sections: Dict[str, Any]) -> Dict[str, Optional[bool]]:
    decl_section = sections.get("DECLARATION_DETAIL", {})
    if isinstance(decl_section, list):
        merged: Dict[str, str] = {}
        for d in decl_section:
            merged.update(d)
        decl_section = merged

    declarations: Dict[str, Any] = {}
    for tag, val in decl_section.items():
        if tag == "EXTENSION":
            continue
        if isinstance(val, str) and val.lower() in ("true", "false", "yes", "no", "1", "0"):
            declarations[tag] = _bool_val(val)
        else:
            declarations[tag] = val
    return declarations


def _extract_employers(sections: Dict[str, Any]) -> List[Dict]:
    raw = sections.get("EMPLOYER", [])
    employers = []
    for item in raw:
        employers.append({
            "name": item.get("FullName"),
            "position": item.get("EmploymentPositionDescription"),
            "start_date": item.get("EmploymentStartDate"),
            "status": item.get("EmploymentStatusType"),
            "classification": item.get("EmploymentClassificationType"),
            "self_employed": _bool_val(item.get("EmploymentBorrowerSelfEmployedIndicator")),
            "months_in_line": _safe_float(item.get("EmploymentTimeInLineOfWorkMonthsCount")),
            "ownership_interest": item.get("OwnershipInterestType"),
        })
    return employers


def _extract_residences(sections: Dict[str, Any]) -> List[Dict]:
    raw = sections.get("RESIDENCE", [])
    residences = []
    for item in raw:
        residences.append({
            "address": item.get("AddressLineText"),
            "city": item.get("CityName"),
            "state": item.get("StateCode"),
            "zip": item.get("PostalCode"),
            "basis": item.get("BorrowerResidencyBasisType"),
            "type": item.get("BorrowerResidencyType"),
            "duration_months": _safe_float(item.get("BorrowerResidencyDurationMonthsCount")),
            "monthly_rent": _safe_float(item.get("MonthlyRentAmount")),
        })
    return residences


# ---------------------------------------------------------------------------
# Main parser — parse_mismo_xml
# ---------------------------------------------------------------------------


def parse_mismo_xml(xml_content: str) -> Dict[str, Any]:
    """Parse a MISMO XML loan file and return a structured dict.

    Return contract is compatible with the previous implementation so
    ``scenario_tools.py`` works without changes.  A new ``raw_xml`` key
    contains the full section-grouped extraction for LLM reasoning.
    """
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError as e:
        return {"parse_error": str(e)}

    fmt = detect_format(root)
    sections = _extract_sections(root)

    purpose = _resolve_str(sections, "purpose")
    mortgage_type = _resolve_str(sections, "mortgage_type")
    loan_amount = _resolve_float(sections, "loan_amount")
    note_rate = _resolve_float(sections, "note_rate")
    lien_priority = _resolve_str(sections, "lien_priority")
    amort_type = _resolve_str(sections, "amortization_type")
    loan_term = _resolve_float(sections, "loan_term_months")
    interest_only = _resolve_bool(sections, "interest_only")
    prepay_penalty = _resolve_bool(sections, "prepay_penalty")
    balloon = _resolve_bool(sections, "balloon")
    cash_out_amount = _resolve_float(sections, "cash_out_amount")
    occupancy = _resolve_str(sections, "occupancy")
    prop_state = _resolve_str(sections, "property_state")
    prop_county = _resolve_str(sections, "property_county")
    prop_city = _resolve_str(sections, "property_city")
    prop_zip = _resolve_str(sections, "property_zip")
    prop_address = _resolve_str(sections, "property_address")
    year_built = _resolve_float(sections, "year_built")
    units = _resolve_float(sections, "units")
    appraised_value = _resolve_float(sections, "appraised_value")
    purchase_price = _resolve_float(sections, "purchase_price")
    citizenship = _resolve_str(sections, "citizenship")
    military = _resolve_bool(sections, "military")
    total_monthly_income = _resolve_float(sections, "total_monthly_income")
    total_monthly_liabilities_val = _resolve_float(sections, "total_monthly_liabilities")
    proposed_housing_expense = _resolve_float(sections, "proposed_housing_expense")
    borrower_count_raw = _resolve_float(sections, "borrower_count_raw")

    pud = _resolve_bool(sections, "pud_indicator")
    in_project = _resolve_bool(sections, "in_project_indicator")
    mixed_use = _resolve_bool(sections, "mixed_use_indicator")
    attachment = _resolve_str(sections, "attachment_type", default="")
    units_int = int(units) if units is not None else None
    property_type = _derive_property_type(
        units_int, pud, in_project, mixed_use, attachment or None,
    )

    ltv = None
    if loan_amount and appraised_value and appraised_value > 0:
        ltv = round(loan_amount / appraised_value * 100, 3)
    ltv_explicit = _resolve_float(sections, "ltv_explicit")
    if ltv_explicit:
        ltv = ltv_explicit
    cltv = _resolve_float(sections, "cltv_explicit")

    dti = None
    if total_monthly_income and total_monthly_income > 0:
        total_liabs = total_monthly_liabilities_val or 0
        dti = round(total_liabs / total_monthly_income * 100, 2)

    borrower_data = _extract_borrowers(sections)
    credit_scores = _extract_credit_scores(sections)
    fico = min(cs["score"] for cs in credit_scores) if credit_scores else None
    housing_expenses = _extract_housing_expenses(sections)
    liabilities = _extract_liabilities(sections)
    owned_properties = _extract_owned_properties(sections)
    declarations = _extract_declarations(sections)
    assets = _extract_assets(sections)
    employers = _extract_employers(sections)
    residences = _extract_residences(sections)

    loan_id = "unknown"
    loan_id_entries = sections.get("LOAN_IDENTIFIER", [])
    if isinstance(loan_id_entries, list):
        for entry in loan_id_entries:
            lid = entry.get("LoanIdentifier")
            if lid:
                loan_id = lid
                break

    borrower_count = int(borrower_count_raw) if borrower_count_raw else None
    if not borrower_count and borrower_data["names"]:
        borrower_count = len(borrower_data["names"])

    result: Dict[str, Any] = {
        "format": fmt,
        "loan_id": loan_id,
        "purpose": purpose,
        "mortgage_type": mortgage_type,
        "loan_amount": loan_amount,
        "note_rate": note_rate,
        "amortization_type": amort_type,
        "loan_term_months": loan_term,
        "interest_only": interest_only,
        "prepay_penalty": prepay_penalty,
        "balloon": balloon,
        "occupancy": occupancy,
        "property_state": prop_state,
        "property_county": prop_county,
        "property_city": prop_city,
        "property_zip": prop_zip,
        "property_address": prop_address,
        "units": units,
        "property_type": property_type,
        "year_built": year_built,
        "appraised_value": appraised_value,
        "purchase_price": purchase_price,
        "lien_priority": lien_priority,
        "fico": fico,
        "ltv": ltv,
        "cltv": cltv,
        "borrower_count": borrower_count,
        "self_employed": borrower_data["self_employed"],
        "borrower_names": borrower_data["names"],
        "borrower_ssns": borrower_data["ssns"],
        "borrower_dobs": borrower_data["dobs"],
        "citizenship": citizenship,
        "military": military,
        "declarations": declarations,
        "liabilities": liabilities,
        "owned_properties": owned_properties,
        "credit_scores": credit_scores,
        "cash_out_amount": cash_out_amount,
        "total_monthly_income": total_monthly_income,
        "total_monthly_liabilities": total_monthly_liabilities_val,
        "proposed_housing_expense": proposed_housing_expense,
        "housing_expenses": housing_expenses,
        "assets": assets,
        "employers": employers,
        "residences": residences,
        "borrower_details": borrower_data["details"],
        "raw_xml": sections,
    }

    if dti is not None:
        result["dti"] = dti

    return result


# ---------------------------------------------------------------------------
# xml_to_loan_profile — produces sample_case.json-shaped output
# ---------------------------------------------------------------------------

_LIEN_MAP = {
    "FirstLien": "First Lien",
    "SecondLien": "Second Lien",
}

_CITIZENSHIP_MAP = {
    "USCitizen": "US Citizen",
    "PermanentResidentAlien": "Permanent Resident",
    "NonPermanentResidentAlien": "Non-Permanent Resident",
}


def xml_to_loan_profile(xml_content: str) -> Dict[str, Any]:
    """Parse MISMO XML and return a dict shaped like sample_case.json.

    The ``metadata`` block mirrors the fields from the platform JSON.
    Fields that cannot be derived from the XML are set to ``None``.
    An ``_xml_supplemental`` key carries rich data (liabilities,
    declarations, housing expenses, etc.) that the profile shape
    doesn't accommodate.
    """
    parsed = parse_mismo_xml(xml_content)

    if "parse_error" in parsed:
        return {"parse_error": parsed["parse_error"]}

    borrower_details = parsed.get("borrower_details", [])
    primary = borrower_details[0] if borrower_details else {}
    co_borrower = borrower_details[1] if len(borrower_details) > 1 else None

    property_value = parsed.get("appraised_value") or parsed.get("purchase_price")

    metadata: Dict[str, Any] = {
        "dti": parsed.get("dti"),
        "dscr": None,
        "fico": parsed.get("fico"),
        "itin": None,
        "state": parsed.get("property_state"),
        "units": int(parsed["units"]) if parsed.get("units") else 0,
        "county": parsed.get("property_county"),
        "escrow": None,
        "channel": None,
        "ltv_pct": parsed.get("ltv"),
        "purpose": parsed.get("purpose"),
        "borrower": {
            "suffix": primary.get("suffix", ""),
            "last_name": primary.get("last_name", ""),
            "first_name": primary.get("first_name", ""),
            "middle_name": primary.get("middle_name", ""),
        },
        "cltv_pct": parsed.get("cltv") or 0,
        "loan_type": _LIEN_MAP.get(parsed.get("lien_priority", ""), parsed.get("lien_priority")),
        "occupancy": parsed.get("occupancy"),
        "income_doc": None,
        "citizenship": _CITIZENSHIP_MAP.get(
            parsed.get("citizenship", ""), parsed.get("citizenship"),
        ),
        "co_borrower": {
            "suffix": co_borrower.get("suffix", ""),
            "last_name": co_borrower.get("last_name", ""),
            "first_name": co_borrower.get("first_name", ""),
            "middle_name": co_borrower.get("middle_name", ""),
        } if co_borrower else None,
        "loan_amount": parsed.get("loan_amount"),
        "loan_number": parsed.get("loan_id"),
        "is_us_credit": None,
        "loan_program": {
            "id": None,
            "name": None,
            "rate": parsed.get("note_rate"),
            "type": parsed.get("mortgage_type"),
            "rateRange": None,
            "originalApiKey": None,
        },
        "borrower_type": None,
        "property_type": parsed.get("property_type"),
        "property_value": property_value,
        "rural_property": None,
        "months_reserves": None,
        "property_address": parsed.get("property_address"),
        "secondary_income": None,
        "first_lien_amount": None,
        "prepayment_penalty": parsed.get("prepay_penalty"),
        "total_liquid_assets": None,
        "secondary_income_doc": None,
        "subordinate_amount_pct": None,
        "total_investment_assets": None,
        "total_retirement_assets": None,
        "closed_end_second_amount": None,
        "from_peoples_republic_of_china": None,
        "self_employed": parsed.get("self_employed"),
        "military": parsed.get("military"),
    }

    supplemental: Dict[str, Any] = {
        "liabilities": parsed.get("liabilities", []),
        "declarations": parsed.get("declarations", {}),
        "housing_expenses": parsed.get("housing_expenses", {}),
        "owned_properties": parsed.get("owned_properties", []),
        "credit_scores": parsed.get("credit_scores", []),
        "assets": parsed.get("assets", []),
        "employers": parsed.get("employers", []),
        "residences": parsed.get("residences", []),
        "borrower_ssns": parsed.get("borrower_ssns", []),
        "borrower_dobs": parsed.get("borrower_dobs", []),
        "loan_terms": {
            "amortization_type": parsed.get("amortization_type"),
            "term_months": parsed.get("loan_term_months"),
            "interest_only": parsed.get("interest_only"),
            "prepay_penalty": parsed.get("prepay_penalty"),
            "balloon": parsed.get("balloon"),
            "lien_priority": parsed.get("lien_priority"),
        },
        "cash_out_amount": parsed.get("cash_out_amount"),
        "total_monthly_income": parsed.get("total_monthly_income"),
        "total_monthly_liabilities": parsed.get("total_monthly_liabilities"),
        "proposed_housing_expense": parsed.get("proposed_housing_expense"),
        "year_built": parsed.get("year_built"),
        "property_city": parsed.get("property_city"),
        "property_zip": parsed.get("property_zip"),
        "format": parsed.get("format"),
        "raw_sections": parsed.get("raw_xml", {}),
    }

    return {
        "id": None,
        "workspace_id": None,
        "template_id": None,
        "template_version_id": None,
        "external_ref": None,
        "title": "Loan",
        "status": "pending",
        "metadata": metadata,
        "_xml_supplemental": supplemental,
        "created_at": None,
        "updated_at": None,
        "deleted_at": None,
    }
