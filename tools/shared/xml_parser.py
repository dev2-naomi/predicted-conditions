"""
xml_parser.py — MISMO XML loan file parser.

Supports both iLAD 2.0 and FNM 3.0 formats as specified in 00_ScenarioBuilder.md.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strip_ns(tag: str) -> str:
    """Remove XML namespace prefix from a tag name."""
    return re.sub(r"\{[^}]*\}", "", tag)


def _find_text(root: ET.Element, *paths: str) -> str | None:
    """Try multiple tag-path candidates and return the first non-empty text found."""
    for path in paths:
        parts = path.split(">")
        node = root
        for part in parts:
            part = part.strip()
            found = None
            for child in node:
                if _strip_ns(child.tag) == part:
                    found = child
                    break
            if found is None:
                node = None
                break
            node = found
        if node is not None and node.text and node.text.strip():
            return node.text.strip()
    return None


def _find_all(root: ET.Element, tag: str) -> list[ET.Element]:
    """Recursively find all elements with the given (namespace-stripped) tag."""
    results = []
    for elem in root.iter():
        if _strip_ns(elem.tag) == tag:
            results.append(elem)
    return results


def _elem_text(elem: ET.Element, tag: str) -> str | None:
    child = elem.find(f".//{tag}")
    if child is None:
        for c in elem.iter():
            if _strip_ns(c.tag) == tag:
                child = c
                break
    if child is not None and child.text:
        return child.text.strip()
    return None


def _bool_val(s: str | None) -> bool | None:
    if s is None:
        return None
    return s.strip().lower() in ("true", "yes", "1")


def _safe_float(s: str | None) -> float | None:
    if s is None:
        return None
    try:
        return float(s.replace(",", "").replace("%", ""))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------


def detect_format(root: ET.Element) -> str:
    """Return 'ilad2' or 'fnm3' based on root tag or namespace."""
    tag = _strip_ns(root.tag)
    if tag in ("LOAN_APPLICATION", "MESSAGE"):
        return "ilad2"
    if tag in ("DEAL_SETS", "DEAL_SET", "DEAL", "LOANS"):
        return "fnm3"
    # Fallback: check for FNM 3.0 markers
    if _find_all(root, "URLA_DETAIL"):
        return "fnm3"
    return "ilad2"


# ---------------------------------------------------------------------------
# Property type derivation
# ---------------------------------------------------------------------------


def _derive_property_type(
    unit_count: int | None,
    pud: bool | None,
    in_project: bool | None,
    mixed_use: bool | None,
    attachment_type: str | None,
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
# Main parser
# ---------------------------------------------------------------------------


def parse_mismo_xml(xml_content: str) -> dict[str, Any]:
    """
    Parse a MISMO XML loan file (iLAD 2.0 or FNM 3.0) and return a flat dict
    of loan fields as defined in 00_ScenarioBuilder.md.
    """
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError as e:
        return {"parse_error": str(e)}

    fmt = detect_format(root)

    result: dict[str, Any] = {
        "format": fmt,
        "loan_id": "unknown",
        "purpose": "unknown",
        "mortgage_type": "unknown",
        "loan_amount": None,
        "note_rate": None,
        "amortization_type": "unknown",
        "loan_term_months": None,
        "interest_only": None,
        "prepay_penalty": None,
        "balloon": None,
        "occupancy": "unknown",
        "property_state": "unknown",
        "property_county": "unknown",
        "property_city": "unknown",
        "property_zip": "unknown",
        "property_address": "unknown",
        "units": None,
        "property_type": "Unknown",
        "year_built": None,
        "appraised_value": None,
        "purchase_price": None,
        "lien_priority": "unknown",
        "fico": None,
        "ltv": None,
        "cltv": None,
        "borrower_count": None,
        "self_employed": None,
        "borrower_names": [],
        "borrower_ssns": [],
        "borrower_dobs": [],
        "citizenship": "unknown",
        "military": None,
        "declarations": {},
        "liabilities": [],
        "owned_properties": [],
    }

    # ---- Loan ID ----
    loan_id = _find_text(
        root,
        "LOAN_IDENTIFIERS > LOAN_IDENTIFIER > LoanIdentifier",
    )
    if loan_id:
        result["loan_id"] = loan_id

    # ---- Terms of Loan ----
    result["purpose"] = (
        _find_text(root, "TERMS_OF_LOAN > LoanPurposeType")
        or _find_text(root, "TERMS_OF_MORTGAGE > LoanPurposeType")
        or "unknown"
    )
    result["mortgage_type"] = (
        _find_text(root, "TERMS_OF_LOAN > MortgageType")
        or _find_text(root, "TERMS_OF_MORTGAGE > MortgageType")
        or "unknown"
    )
    result["loan_amount"] = _safe_float(
        _find_text(root, "TERMS_OF_LOAN > BaseLoanAmount")
        or _find_text(root, "TERMS_OF_LOAN > NoteAmount")
        or _find_text(root, "TERMS_OF_MORTGAGE > NoteAmount")
    )
    result["note_rate"] = _safe_float(
        _find_text(root, "TERMS_OF_LOAN > NoteRatePercent")
        or _find_text(root, "TERMS_OF_MORTGAGE > NoteRatePercent")
    )
    result["lien_priority"] = (
        _find_text(root, "TERMS_OF_LOAN > LienPriorityType")
        or _find_text(root, "TERMS_OF_MORTGAGE > LienPriorityType")
        or "unknown"
    )

    # ---- Amortization ----
    result["amortization_type"] = (
        _find_text(root, "AMORTIZATION_RULE > AmortizationType")
        or _find_text(root, "AMORTIZATION_RULE > LoanAmortizationType")
        or "unknown"
    )
    result["loan_term_months"] = _safe_float(
        _find_text(root, "AMORTIZATION_RULE > LoanAmortizationPeriodCount")
    )

    # ---- Loan Detail ----
    result["interest_only"] = _bool_val(
        _find_text(root, "LOAN_DETAIL > InterestOnlyIndicator")
    )
    result["prepay_penalty"] = _bool_val(
        _find_text(root, "LOAN_DETAIL > PrepaymentPenaltyIndicator")
    )
    result["balloon"] = _bool_val(
        _find_text(root, "LOAN_DETAIL > BalloonIndicator")
    )

    # ---- Property ----
    prop_nodes = _find_all(root, "SUBJECT_PROPERTY") or _find_all(root, "PROPERTY")
    if prop_nodes:
        prop = prop_nodes[0]
        result["occupancy"] = (
            _elem_text(prop, "PropertyUsageType") or "unknown"
        )
        result["property_state"] = (
            _elem_text(prop, "StateCode") or "unknown"
        )
        result["property_county"] = (
            _elem_text(prop, "CountyName") or "unknown"
        )
        result["property_city"] = (
            _elem_text(prop, "CityName") or "unknown"
        )
        result["property_zip"] = (
            _elem_text(prop, "PostalCode") or "unknown"
        )
        result["property_address"] = (
            _elem_text(prop, "AddressLineText") or "unknown"
        )
        result["year_built"] = _safe_float(
            _elem_text(prop, "PropertyStructureBuiltYear")
        )
        result["units"] = _safe_float(
            _elem_text(prop, "FinancedUnitCount")
        )
        pud = _bool_val(_elem_text(prop, "PUDIndicator"))
        in_project = _bool_val(_elem_text(prop, "PropertyInProjectIndicator"))
        mixed_use = _bool_val(_elem_text(prop, "PropertyMixedUsageIndicator"))
        attachment = _elem_text(prop, "AttachmentType")
        units_int = int(result["units"]) if result["units"] is not None else None
        result["property_type"] = _derive_property_type(
            units_int, pud, in_project, mixed_use, attachment
        )

    # ---- Valuations ----
    result["appraised_value"] = _safe_float(
        _find_text(root, "PROPERTY_VALUATIONS > PROPERTY_VALUATION_DETAIL > PropertyValuationAmount")
    )

    # ---- Purchase price ----
    result["purchase_price"] = _safe_float(
        _find_text(root, "SALES_CONTRACTS > SALES_CONTRACT_DETAIL > SalesContractAmount")
        or _find_text(root, "URLA_DETAIL > PurchasePriceAmount")
    )

    # ---- LTV / CLTV ----
    if result["loan_amount"] and result["appraised_value"] and result["appraised_value"] > 0:
        result["ltv"] = round(result["loan_amount"] / result["appraised_value"] * 100, 3)
    ltv_elem = _find_text(root, "LTV > LTVRatioPercent")
    if ltv_elem:
        result["ltv"] = _safe_float(ltv_elem)
    cltv_elem = _find_text(root, "COMBINED_LTV > CombinedLTVRatioPercent")
    result["cltv"] = _safe_float(cltv_elem)

    # ---- FICO (FNM only) ----
    fico_nodes = _find_all(root, "CREDIT_SCORE_DETAIL")
    if fico_nodes:
        result["fico"] = _safe_float(_elem_text(fico_nodes[0], "CreditScoreValue"))

    # ---- Borrowers ----
    borrower_count_raw = _find_text(root, "LOAN_DETAIL > BorrowerCount")
    if borrower_count_raw:
        result["borrower_count"] = int(_safe_float(borrower_count_raw) or 0)

    party_nodes = _find_all(root, "PARTY")
    borrower_parties = [
        p for p in party_nodes
        if (_elem_text(p, "PartyRoleType") or "").lower() == "borrower"
    ]
    if not result["borrower_count"] and borrower_parties:
        result["borrower_count"] = len(borrower_parties)

    names = []
    ssns = []
    dobs = []
    self_employed_flags = []
    for party in borrower_parties:
        first = _elem_text(party, "FirstName") or ""
        last = _elem_text(party, "LastName") or ""
        middle = _elem_text(party, "MiddleName") or ""
        suffix = _elem_text(party, "SuffixName") or ""
        full_name = " ".join(filter(None, [first, middle, last, suffix]))
        if full_name:
            names.append(full_name)
        ssn = _elem_text(party, "TaxpayerIdentifierValue")
        if ssn:
            ssns.append(ssn)
        dob = _elem_text(party, "BorrowerBirthDate")
        if dob:
            dobs.append(dob)
        se = _elem_text(party, "EmploymentBorrowerSelfEmployedIndicator")
        if se:
            self_employed_flags.append(_bool_val(se))

    result["borrower_names"] = names
    result["borrower_ssns"] = ssns
    result["borrower_dobs"] = dobs
    if self_employed_flags:
        result["self_employed"] = any(self_employed_flags)

    # ---- Citizenship / Military ----
    result["citizenship"] = (
        _find_text(root, "DECLARATION_DETAIL > CitizenshipResidencyType") or "unknown"
    )
    military_raw = _find_text(root, "BORROWER_DETAIL > SelfDeclaredMilitaryServiceIndicator")
    result["military"] = _bool_val(military_raw)

    # ---- Declarations ----
    decl_tags = [
        "BankruptcyIndicator",
        "PriorPropertyForeclosureCompletedIndicator",
        "PriorPropertyShortSaleCompletedIndicator",
        "PriorPropertyDeedInLieuConveyedIndicator",
        "HomeownerPastThreeYearsType",
        "LiabilityCoMakerEndorserIndicator",
        "OutstandingJudgmentsIndicator",
        "PartyToLawsuitIndicator",
        "PresentlyDelinquentIndicator",
        "PropertyForeclosureOrJudgmentIndicator",
    ]
    declarations: dict[str, bool | None] = {}
    for tag in decl_tags:
        val = _find_text(root, f"DECLARATION_DETAIL > {tag}")
        if val is not None:
            declarations[tag] = _bool_val(val)
    result["declarations"] = declarations

    # ---- Liabilities ----
    liabilities = []
    for liab in _find_all(root, "LIABILITY"):
        liabilities.append({
            "type": _elem_text(liab, "LiabilityType"),
            "monthly_payment": _safe_float(_elem_text(liab, "LiabilityMonthlyPaymentAmount")),
            "unpaid_balance": _safe_float(_elem_text(liab, "LiabilityUnpaidBalanceAmount")),
            "payoff_status": _elem_text(liab, "LiabilityPayoffStatusIndicator"),
            "holder": _elem_text(liab, "FullName"),
        })
    result["liabilities"] = liabilities

    # ---- Owned Properties (iLAD) ----
    owned = []
    for op in _find_all(root, "OWNED_PROPERTY"):
        owned.append({
            "address": _elem_text(op, "AddressLineText"),
            "value": _safe_float(_elem_text(op, "PropertyCurrentValueAmount")),
            "lien_upb": _safe_float(_elem_text(op, "LiabilityUnpaidBalanceAmount")),
            "rental_income": _safe_float(_elem_text(op, "RentalEstimatedMonthlyRentAmount")),
            "disposition": _elem_text(op, "PropertyDispositionType"),
            "is_subject": _bool_val(_elem_text(op, "SubjectIndicator")),
        })
    result["owned_properties"] = owned

    return result
