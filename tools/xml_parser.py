from __future__ import annotations

"""
MISMO XML / FNM Loan File Parser

Parses iLAD 2.0 and FNM 3.0 format MISMO 3.x residential XML files
into a normalized Python dict for consumption by downstream tools.

Handles both single-line and multi-line XML files.
"""

import os
import re
import xml.etree.ElementTree as ET
from typing import Any, Optional


# ═══════════════════════════════════════════════════════════════════════════
# Namespace helpers — MISMO XMLs are heavily namespaced
# ═══════════════════════════════════════════════════════════════════════════

_MISMO_NS = "http://www.mismo.org/residential/2009/schemas"
_ULAD_NS = "http://www.datamodelextension.org/Schema/ULAD"
_DU_NS = "http://www.datamodelextension.org/Schema/DU"
_LPA_NS = "http://www.datamodelextension.org/Schema/LPA"
_ILAD_NS = "http://www.datamodelextension.org/Schema/ILAD"
_XLINK_NS = "http://www.w3.org/1999/xlink"

_NS = {
    "m": _MISMO_NS,
    "ULAD": _ULAD_NS,
    "DU": _DU_NS,
    "LPA": _LPA_NS,
    "ilad": _ILAD_NS,
    "xlink": _XLINK_NS,
}


def _tag(ns_prefix: str, local: str) -> str:
    """Build a Clark-notation tag for ElementTree."""
    ns_map = {
        "m": _MISMO_NS,
        "ULAD": _ULAD_NS,
        "DU": _DU_NS,
        "LPA": _LPA_NS,
        "ilad": _ILAD_NS,
        "xlink": _XLINK_NS,
    }
    return f"{{{ns_map[ns_prefix]}}}{local}"


def _find(element: ET.Element, path: str) -> Optional[ET.Element]:
    """XPath-like find using namespace map."""
    return element.find(path, _NS)


def _findall(element: ET.Element, path: str) -> list[ET.Element]:
    return element.findall(path, _NS)


def _text(element: ET.Element, path: str, default: str = "unknown") -> str:
    """Extract text from a sub-element, returning default if not found."""
    el = _find(element, path)
    if el is not None and el.text:
        return el.text.strip()
    return default


def _bool(element: ET.Element, path: str) -> Optional[bool]:
    """Extract a boolean indicator."""
    val = _text(element, path, default="")
    if val.lower() == "true":
        return True
    if val.lower() == "false":
        return False
    return None


def _float(element: ET.Element, path: str) -> Optional[float]:
    val = _text(element, path, default="")
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _int(element: ET.Element, path: str) -> Optional[int]:
    val = _text(element, path, default="")
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


# ═══════════════════════════════════════════════════════════════════════════
# Main parser
# ═══════════════════════════════════════════════════════════════════════════


def detect_format(root: ET.Element) -> str:
    """Detect whether the file is iLAD 2.0 or FNM 3.0."""
    about = root.find(f".//{_tag('m', 'AboutVersionIdentifier')}")
    if about is not None and about.text:
        txt = about.text.strip().lower()
        if "ilad" in txt:
            return "iLAD"
    # If URLA_DETAIL or BorrowerCount is present, likely FNM
    if root.find(f".//{_tag('m', 'URLA_DETAIL')}") is not None:
        return "FNM"
    return "iLAD"  # default


def parse_mismo_xml(xml_path: str) -> dict[str, Any]:
    """
    Parse a MISMO XML file (iLAD 2.0 or FNM 3.0) and return a normalized dict.

    The output dict mirrors the scenario_summary schema from Module 00.
    Handles both iLAD 2.0 (TERMS_OF_LOAN, SUBJECT_PROPERTY) and FNM 3.0
    (TERMS_OF_MORTGAGE, COLLATERAL/PROPERTIES/PROPERTY) element names.
    """
    tree = ET.parse(xml_path)
    root = tree.getroot()
    fmt = detect_format(root)

    # Locate the DEAL node
    deal = root.find(f".//{_tag('m', 'DEAL')}")
    if deal is None:
        raise ValueError(f"No DEAL element found in {xml_path}")

    result: dict[str, Any] = {
        "source_file": os.path.basename(xml_path),
        "format": fmt,
    }

    # ── Loan Identifiers ─────────────────────────────────────────────
    loan = deal.find(f".//{_tag('m', 'LOAN')}")
    if loan is not None:
        result["loan_id"] = _text(loan, f".//{_tag('m', 'LoanIdentifier')}")

        # Terms — iLAD uses TERMS_OF_LOAN, FNM 3.0 uses TERMS_OF_MORTGAGE
        terms = loan.find(f".//{_tag('m', 'TERMS_OF_LOAN')}")
        if terms is None:
            terms = loan.find(f".//{_tag('m', 'TERMS_OF_MORTGAGE')}")
        if terms is not None:
            result["purpose"] = _text(terms, _tag("m", "LoanPurposeType"))
            result["mortgage_type"] = _text(terms, _tag("m", "MortgageType"))
            result["loan_amount"] = (
                _float(terms, _tag("m", "BaseLoanAmount"))
                or _float(terms, _tag("m", "NoteAmount"))
            )
            result["note_rate"] = _float(terms, _tag("m", "NoteRatePercent"))
            result["note_amount"] = _float(terms, _tag("m", "NoteAmount"))
            result["lien_priority"] = _text(terms, _tag("m", "LienPriorityType"))

        # Amortization
        amort = loan.find(f".//{_tag('m', 'AMORTIZATION_RULE')}")
        if amort is not None:
            result["amortization_type"] = _text(amort, _tag("m", "AmortizationType"))
            # FNM may use LoanAmortizationType instead of AmortizationType
            if result.get("amortization_type") == "unknown":
                result["amortization_type"] = _text(amort, _tag("m", "LoanAmortizationType"))
            result["loan_term_months"] = _int(amort, _tag("m", "LoanAmortizationPeriodCount"))

        # Loan Detail
        ld = loan.find(f".//{_tag('m', 'LOAN_DETAIL')}")
        if ld is not None:
            result["interest_only"] = _bool(ld, _tag("m", "InterestOnlyIndicator"))
            result["prepay_penalty"] = _bool(ld, _tag("m", "PrepaymentPenaltyIndicator"))
            result["balloon"] = _bool(ld, _tag("m", "BalloonIndicator"))

        # LTV element (FNM 3.0)
        ltv_el = loan.find(f".//{_tag('m', 'LTV')}")
        if ltv_el is not None:
            result["xml_ltv"] = _float(ltv_el, _tag("m", "LTVRatioPercent"))
            result["xml_base_ltv"] = _float(ltv_el, _tag("m", "BaseLTVRatioPercent"))

        # CLTV element (FNM 3.0)
        cltv_el = deal.find(f".//{_tag('m', 'COMBINED_LTV')}")
        if cltv_el is not None:
            result["xml_cltv"] = _float(cltv_el, _tag("m", "CombinedLTVRatioPercent"))

        # Qualification (FNM 3.0) — monthly income, liabilities, housing
        qual = loan.find(f".//{_tag('m', 'QUALIFICATION')}")
        if qual is not None:
            result["total_monthly_income"] = _float(qual, _tag("m", "TotalMonthlyIncomeAmount"))
            result["total_monthly_liabilities"] = _float(qual, _tag("m", "TotalLiabilitiesMonthlyPaymentAmount"))
            result["proposed_housing_expense"] = _float(qual, _tag("m", "TotalMonthlyProposedHousingExpenseAmount"))
            result["reserves_months"] = _int(qual, _tag("m", "BorrowerReservesMonthlyPaymentCount"))

        # Refinance
        refi = loan.find(f".//{_tag('m', 'REFINANCE')}")
        if refi is not None:
            result["cash_out_amount"] = _float(refi, _tag("m", "RefinanceCashOutAmount"))

    # ── Subject Property ──────────────────────────────────────────────
    # iLAD 2.0 uses SUBJECT_PROPERTY, FNM 3.0 uses COLLATERAL/PROPERTIES/PROPERTY
    sp = deal.find(f".//{_tag('m', 'SUBJECT_PROPERTY')}")
    if sp is None:
        # FNM 3.0: first PROPERTY under COLLATERALS
        sp = deal.find(f".//{_tag('m', 'COLLATERAL')}//{_tag('m', 'PROPERTY')}")
    if sp is None:
        # Last resort: first PROPERTY anywhere under DEAL
        sp = deal.find(f".//{_tag('m', 'PROPERTY')}")

    if sp is not None:
        addr = sp.find(f".//{_tag('m', 'ADDRESS')}")
        if addr is not None:
            result["property_state"] = _text(addr, _tag("m", "StateCode"))
            result["property_county"] = _text(addr, _tag("m", "CountyName"))
            result["property_city"] = _text(addr, _tag("m", "CityName"))
            result["property_zip"] = _text(addr, _tag("m", "PostalCode"))
            result["property_address"] = _text(addr, _tag("m", "AddressLineText"))

        pd = sp.find(f".//{_tag('m', 'PROPERTY_DETAIL')}")
        if pd is not None:
            result["units"] = _int(pd, _tag("m", "FinancedUnitCount"))
            result["occupancy"] = _text(pd, _tag("m", "PropertyUsageType"))
            result["year_built"] = _int(pd, _tag("m", "PropertyStructureBuiltYear"))
            result["pud_indicator"] = _bool(pd, _tag("m", "PUDIndicator"))
            result["in_project"] = _bool(pd, _tag("m", "PropertyInProjectIndicator"))
            result["mixed_use"] = _bool(pd, _tag("m", "PropertyMixedUsageIndicator"))
            result["attachment_type"] = _text(pd, _tag("m", "AttachmentType"))
            result["estate_type"] = _text(pd, _tag("m", "PropertyEstateType"))

            # Derive property type
            result["property_type"] = _derive_property_type(
                units=result.get("units"),
                pud=result.get("pud_indicator"),
                in_project=result.get("in_project"),
                mixed_use=result.get("mixed_use"),
                attachment_type=result.get("attachment_type"),
            )

        # Valuation
        pv = sp.find(f".//{_tag('m', 'PROPERTY_VALUATION_DETAIL')}")
        if pv is not None:
            result["appraised_value"] = _float(pv, _tag("m", "PropertyValuationAmount"))

        # Flood determination
        flood = sp.find(f".//{_tag('m', 'FLOOD_DETERMINATION_DETAIL')}")
        if flood is not None:
            result["special_flood_hazard"] = _bool(flood, _tag("m", "SpecialFloodHazardAreaIndicator"))
            result["flood_insurance_required"] = _bool(flood, _tag("m", "PropertyFloodInsuranceIndicator"))

    # ── Sales Contract (purchase price) ───────────────────────────────
    sc = deal.find(f".//{_tag('m', 'SALES_CONTRACT_DETAIL')}")
    if sc is not None:
        result["purchase_price"] = _float(sc, _tag("m", "SalesContractAmount"))
    else:
        # FNM alternate
        urla = deal.find(f".//{_tag('m', 'URLA_DETAIL')}")
        if urla is not None:
            result["purchase_price"] = _float(urla, _tag("m", "PurchasePriceAmount"))

    # ── Borrowers ─────────────────────────────────────────────────────
    borrowers = []
    parties = _findall(deal, f".//{_tag('m', 'PARTY')}")
    for party in parties:
        roles = _findall(party, f".//{_tag('m', 'ROLE')}")
        for role in roles:
            role_type = _text(role, f".//{_tag('m', 'PartyRoleType')}")
            if role_type != "Borrower":
                continue

            borrower_info: dict[str, Any] = {"role_type": role_type}

            # Name
            name_el = party.find(f".//{_tag('m', 'NAME')}")
            if name_el is not None:
                first = _text(name_el, _tag("m", "FirstName"))
                last = _text(name_el, _tag("m", "LastName"))
                middle = _text(name_el, _tag("m", "MiddleName"))
                suffix = _text(name_el, _tag("m", "SuffixName"))
                borrower_info["first_name"] = first
                borrower_info["last_name"] = last
                borrower_info["middle_name"] = middle if middle != "unknown" else None
                borrower_info["suffix"] = suffix if suffix != "unknown" else None
                borrower_info["full_name"] = _text(name_el, _tag("m", "FullName"))

            # SSN
            ssn_el = party.find(f".//{_tag('m', 'TaxpayerIdentifierValue')}")
            if ssn_el is not None and ssn_el.text:
                borrower_info["ssn"] = ssn_el.text.strip()
            ssn_type = party.find(f".//{_tag('m', 'TaxpayerIdentifierType')}")
            if ssn_type is not None and ssn_type.text:
                borrower_info["ssn_type"] = ssn_type.text.strip()

            # Borrower Detail
            bd = role.find(f".//{_tag('m', 'BORROWER_DETAIL')}")
            if bd is not None:
                borrower_info["dob"] = _text(bd, _tag("m", "BorrowerBirthDate"))
                borrower_info["military"] = _bool(bd, _tag("m", "SelfDeclaredMilitaryServiceIndicator"))
                borrower_info["marital_status"] = _text(bd, _tag("m", "MaritalStatusType"))

            # Employment
            employer = role.find(f".//{_tag('m', 'EMPLOYER')}")
            if employer is not None:
                emp = employer.find(f".//{_tag('m', 'EMPLOYMENT')}")
                if emp is not None:
                    borrower_info["self_employed"] = _bool(emp, _tag("m", "EmploymentBorrowerSelfEmployedIndicator"))
                    borrower_info["employment_status"] = _text(emp, _tag("m", "EmploymentStatusType"))
                    borrower_info["position"] = _text(emp, _tag("m", "EmploymentPositionDescription"))
                    borrower_info["time_in_line_months"] = _int(emp, _tag("m", "EmploymentTimeInLineOfWorkMonthsCount"))

                legal = employer.find(f".//{_tag('m', 'LEGAL_ENTITY_DETAIL')}")
                if legal is not None:
                    borrower_info["employer_name"] = _text(legal, _tag("m", "FullName"))

            # Declarations
            decl = role.find(f".//{_tag('m', 'DECLARATION_DETAIL')}")
            if decl is not None:
                borrower_info["declarations"] = {
                    "bankruptcy": _bool(decl, _tag("m", "BankruptcyIndicator")),
                    "foreclosure": _bool(decl, _tag("m", "PriorPropertyForeclosureCompletedIndicator")),
                    "short_sale": _bool(decl, _tag("m", "PriorPropertyShortSaleCompletedIndicator")),
                    "deed_in_lieu": _bool(decl, _tag("m", "PriorPropertyDeedInLieuConveyedIndicator")),
                    "outstanding_judgments": _bool(decl, _tag("m", "OutstandingJudgmentsIndicator")),
                    "party_to_lawsuit": _bool(decl, _tag("m", "PartyToLawsuitIndicator")),
                    "presently_delinquent": _bool(decl, _tag("m", "PresentlyDelinquentIndicator")),
                    "undisclosed_borrowed_funds": _bool(decl, _tag("m", "UndisclosedBorrowedFundsIndicator")),
                    "undisclosed_comaker": _bool(decl, _tag("m", "UndisclosedComakerOfNoteIndicator")),
                    "undisclosed_credit_app": _bool(decl, _tag("m", "UndisclosedCreditApplicationIndicator")),
                    "undisclosed_mortgage_app": _bool(decl, _tag("m", "UndisclosedMortgageApplicationIndicator")),
                    "intent_to_occupy": _text(decl, _tag("m", "IntentToOccupyType")),
                    "homeowner_past_3_years": _text(decl, _tag("m", "HomeownerPastThreeYearsType")),
                }
                # Citizenship (FNM)
                cit = _text(decl, _tag("m", "CitizenshipResidencyType"))
                if cit != "unknown":
                    borrower_info["citizenship"] = cit

            # Residence
            res = role.find(f".//{_tag('m', 'RESIDENCE')}")
            if res is not None:
                res_det = res.find(f".//{_tag('m', 'RESIDENCE_DETAIL')}")
                if res_det is not None:
                    borrower_info["residency_basis"] = _text(res_det, _tag("m", "BorrowerResidencyBasisType"))
                    borrower_info["residency_duration_months"] = _int(res_det, _tag("m", "BorrowerResidencyDurationMonthsCount"))

            borrowers.append(borrower_info)

    result["borrowers"] = borrowers
    result["borrower_count"] = len(borrowers)

    # ── Credit Scores ─────────────────────────────────────────────────
    # Collect all credit scores across all borrowers
    credit_scores: list[dict[str, Any]] = []
    for cs in _findall(deal, f".//{_tag('m', 'CREDIT_SCORE_DETAIL')}"):
        score_val = _int(cs, _tag("m", "CreditScoreValue"))
        source = _text(cs, _tag("m", "CreditRepositorySourceType"))
        model = _text(cs, _tag("m", "CreditScoreModelType"))
        if score_val and score_val > 0:
            credit_scores.append({
                "score": score_val,
                "source": source if source != "unknown" else None,
                "model": model if model != "unknown" else None,
            })
    result["credit_scores"] = credit_scores
    # Keep backward-compat fico field — use min qualifying score
    if credit_scores:
        result["fico"] = min(cs["score"] for cs in credit_scores)
    else:
        # Loan-level credit score
        loan_level_cs = deal.find(f".//{_tag('m', 'LOAN_LEVEL_CREDIT_DETAIL')}")
        if loan_level_cs is not None:
            lcs = _int(loan_level_cs, _tag("m", "LoanLevelCreditScoreValue"))
            if lcs and lcs > 0:
                result["fico"] = lcs
            else:
                result["fico"] = None
        else:
            result["fico"] = None

    # ── Liabilities ───────────────────────────────────────────────────
    liabilities = []
    for liab in _findall(deal, f".//{_tag('m', 'LIABILITY')}"):
        ld = liab.find(f".//{_tag('m', 'LIABILITY_DETAIL')}")
        if ld is None:
            continue
        holder_name = _text(liab, f".//{_tag('m', 'FullName')}")
        liabilities.append({
            "type": _text(ld, _tag("m", "LiabilityType")),
            "monthly_payment": _float(ld, _tag("m", "LiabilityMonthlyPaymentAmount")),
            "unpaid_balance": _float(ld, _tag("m", "LiabilityUnpaidBalanceAmount")),
            "payoff": _bool(ld, _tag("m", "LiabilityPayoffStatusIndicator")),
            "holder": holder_name if holder_name != "unknown" else None,
            "account_id": _text(ld, _tag("m", "LiabilityAccountIdentifier")),
        })
    result["liabilities"] = liabilities

    # ── Owned Properties (iLAD) ───────────────────────────────────────
    owned_properties = []
    for asset in _findall(deal, f".//{_tag('m', 'ASSET')}"):
        op = asset.find(f".//{_tag('m', 'OWNED_PROPERTY')}")
        if op is None:
            continue
        opd = op.find(f".//{_tag('m', 'OWNED_PROPERTY_DETAIL')}")
        prop = op.find(f".//{_tag('m', 'PROPERTY')}")
        item: dict[str, Any] = {}
        if opd is not None:
            item["lien_upb"] = _float(opd, _tag("m", "OwnedPropertyLienUPBAmount"))
            item["maintenance_expense"] = _float(opd, _tag("m", "OwnedPropertyMaintenanceExpenseAmount"))
            item["rental_income_net"] = _float(opd, _tag("m", "OwnedPropertyRentalIncomeNetAmount"))
            item["is_subject"] = _bool(opd, _tag("m", "OwnedPropertySubjectIndicator"))
        if prop is not None:
            addr = prop.find(f".//{_tag('m', 'ADDRESS')}")
            if addr is not None:
                item["city"] = _text(addr, _tag("m", "CityName"))
                item["state"] = _text(addr, _tag("m", "StateCode"))
                item["zip"] = _text(addr, _tag("m", "PostalCode"))
            pdet = prop.find(f".//{_tag('m', 'PROPERTY_DETAIL')}")
            if pdet is not None:
                item["estimated_value"] = _float(pdet, _tag("m", "PropertyEstimatedValueAmount"))
        owned_properties.append(item)
    result["owned_properties"] = owned_properties

    # ── Housing Expenses ──────────────────────────────────────────────
    housing_expenses = {"present": [], "proposed": []}
    for he in _findall(deal, f".//{_tag('m', 'HOUSING_EXPENSE')}"):
        timing = _text(he, _tag("m", "HousingExpenseTimingType"))
        expense_type = _text(he, _tag("m", "HousingExpenseType"))
        amount = _float(he, _tag("m", "HousingExpensePaymentAmount"))
        entry = {"type": expense_type, "amount": amount}
        if timing == "Present":
            housing_expenses["present"].append(entry)
        elif timing == "Proposed":
            housing_expenses["proposed"].append(entry)
    result["housing_expenses"] = housing_expenses

    # ── Calculated fields ─────────────────────────────────────────────
    loan_amt = result.get("loan_amount")
    appr_val = result.get("appraised_value")
    purch_price = result.get("purchase_price")

    # Use XML-provided LTV if available, otherwise calculate
    if result.get("xml_ltv") is not None:
        result["ltv"] = result["xml_ltv"]
    elif loan_amt and appr_val and appr_val > 0:
        result["ltv"] = round(loan_amt / appr_val * 100, 2)
        # For purchase, LTV uses lesser of appraised_value and purchase_price
        if result.get("purpose", "").lower() in ("purchase",) and purch_price:
            lesser = min(appr_val, purch_price)
            if lesser > 0:
                result["ltv"] = round(loan_amt / lesser * 100, 2)
    else:
        result["ltv"] = None

    # CLTV — use XML value if present, otherwise calculate
    if result.get("xml_cltv") is not None:
        result["cltv"] = result["xml_cltv"]
    else:
        total_liens = loan_amt or 0
        for liab in result.get("liabilities", []):
            if liab.get("type") == "MortgageLoan" and not liab.get("payoff"):
                total_liens += liab.get("unpaid_balance", 0) or 0
        if appr_val and appr_val > 0:
            result["cltv"] = round(total_liens / appr_val * 100, 2)
        else:
            result["cltv"] = None

    # DTI — from Qualification element or calculated
    total_income = result.get("total_monthly_income")
    total_liabs = result.get("total_monthly_liabilities")
    if total_income and total_income > 0 and total_liabs is not None:
        result["dti"] = round(total_liabs / total_income * 100, 2)
    else:
        result["dti"] = None

    return result


# ═══════════════════════════════════════════════════════════════════════════
# Property Type Derivation
# ═══════════════════════════════════════════════════════════════════════════


def _derive_property_type(
    units: Optional[int],
    pud: Optional[bool],
    in_project: Optional[bool],
    mixed_use: Optional[bool],
    attachment_type: Optional[str] = None,
) -> str:
    """
    Derive property type from XML flags per Module 00 spec.
    """
    if mixed_use is True:
        return "Mixed-Use"
    if units is not None:
        if units == 1:
            if pud is True:
                return "PUD"
            if in_project is True and pud is not True:
                return "Condo"
            # Use AttachmentType to differentiate Condo vs SFR
            if attachment_type and attachment_type.lower() in ("attached", "semidetached"):
                return "Condo/Townhouse"
            return "SFR"
        if 2 <= units <= 4:
            return "2-4 Unit"
        if 5 <= units <= 8:
            return "Multi 5-8"
    return "Unknown"


# ═══════════════════════════════════════════════════════════════════════════
# FNM File Parser (Fannie Mae Export Text)
# ═══════════════════════════════════════════════════════════════════════════


def parse_fnm_file(fnm_path: str) -> dict[str, Any]:
    """
    Parse a .fnm (Fannie Mae 3.2 flat file) format.
    FNM files are pipe-delimited text. This is a basic parser for key fields.
    For full parsing, the file should be converted to MISMO XML first.
    """
    with open(fnm_path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()

    # If it starts with XML declaration, it's actually XML
    if content.strip().startswith("<?xml") or content.strip().startswith("<MESSAGE"):
        return parse_mismo_xml(fnm_path)

    # Otherwise treat as pipe-delimited FNM
    result: dict[str, Any] = {
        "source_file": os.path.basename(fnm_path),
        "format": "FNM_flat",
        "raw_content_preview": content[:2000],
    }
    # Basic FNM parsing — fields are positional per record type
    # This is a simplified parser; production would need full FNM spec
    lines = content.strip().split("\n")
    for line in lines:
        parts = line.split("|")
        if not parts:
            continue
        record_type = parts[0].strip() if parts else ""
        # Add more record type parsers as needed
        if record_type == "00" and len(parts) > 5:
            result["loan_amount"] = _safe_float(parts[2]) if len(parts) > 2 else None
            result["note_rate"] = _safe_float(parts[3]) if len(parts) > 3 else None

    return result


def _safe_float(val: str) -> Optional[float]:
    try:
        return float(val.strip())
    except (ValueError, TypeError):
        return None


# ═══════════════════════════════════════════════════════════════════════════
# Unified entry point
# ═══════════════════════════════════════════════════════════════════════════


def parse_loan_file(filepath: str) -> dict[str, Any]:
    """
    Auto-detect file format and parse accordingly.
    Supports: .xml (MISMO iLAD/FNM XML), .fnm (Fannie Mae flat file)
    """
    ext = os.path.splitext(filepath)[1].lower()
    if ext == ".fnm":
        return parse_fnm_file(filepath)
    else:
        return parse_mismo_xml(filepath)
