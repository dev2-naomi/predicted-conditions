"""
normalize.py — Shared normalization for document request dicts.

Ensures consistent casing, valid enum values, required field presence,
and maps LLM-generated document names to canonical masterlist names.
"""

from __future__ import annotations

import json
from pathlib import Path

_VALID_PRIORITIES = {"P0", "P1", "P2", "P3"}
_VALID_SEVERITIES = {"HARD-STOP", "SOFT-STOP"}
_VALID_CATEGORIES = {
    "Program Eligibility", "Income", "Assets", "Credit",
    "Property", "Appraisal", "Title", "Compliance", "Closing", "Other",
    "Cross-Cutting",
}
_VALID_STATUSES = {
    "needed", "partially_satisfied", "satisfied_but_review_required", "unknown",
}

_PRIORITY_ALIASES: dict[str, str] = {
    "p0": "P0", "p1": "P1", "p2": "P2", "p3": "P3",
    "critical": "P0", "high": "P1", "medium": "P2", "low": "P3",
    "conditional": "P2",
}

_SEVERITY_ALIASES: dict[str, str] = {
    "hard-stop": "HARD-STOP", "hard_stop": "HARD-STOP", "hardstop": "HARD-STOP",
    "soft-stop": "SOFT-STOP", "soft_stop": "SOFT-STOP", "softstop": "SOFT-STOP",
}

_CATEGORY_ALIASES: dict[str, str] = {
    "cross_cutting": "Cross-Cutting", "crosscutting": "Cross-Cutting",
    "cross-cutting": "Cross-Cutting", "program eligibility": "Program Eligibility",
    "program_eligibility": "Program Eligibility",
    "income": "Income", "assets": "Assets", "credit": "Credit",
    "property": "Property", "appraisal": "Appraisal",
    "property_appraisal": "Property", "property/appraisal": "Property",
    "title": "Title", "title_closing": "Title", "title/closing": "Title",
    "closing": "Closing", "compliance": "Compliance", "other": "Other",
}

# ---------------------------------------------------------------------------
# Document-type name mapping: LLM-generated name -> canonical masterlist name
# ---------------------------------------------------------------------------
# Keys are lowercased for case-insensitive lookup.
# This covers every variant we've seen the LLM produce across test runs.
_DOCTYPE_ALIASES: dict[str, str] = {
    # Identity — maps to primary type; acceptable_types added separately
    "government-issued photo id": "Government-Issued Photo ID",
    "government issued photo id": "Government-Issued Photo ID",
    "photo id": "Government-Issued Photo ID",
    "state id": "Government-Issued Photo ID",
    "driver's license": "Drivers License",
    "drivers license": "Drivers License",
    "non-driver id": "Non-Driver ID",

    # Authorization / 4506
    "irs form 4506-c": "IRS 4506-C Authorization",
    "irs form 4506c": "IRS 4506-C Authorization",
    "4506-c": "IRS 4506-C Authorization",
    "4506c": "IRS 4506-C Authorization",
    "borrower authorization form (4506-c)": "IRS 4506-C Authorization",
    "4506-t": "4506-T",
    "borrower authorization": "Borrowers Authorization",
    "borrowers authorization": "Borrowers Authorization",
    "credit authorization": "Borrowers Authorization",
    "authorization to release information": "Authorization to Release Information",

    # Occupancy
    "occupancy certification": "Owner Occupancy Certification",
    "occupancy certification - investment property": "Owner Occupancy Certification",
    "non-owner occupancy certification / investment property affidavit": "Owner Occupancy Certification",
    "occupancy certification / declaration": "Owner Occupancy Certification",
    "occupancy declaration": "Owner Occupancy Certification",
    "affidavit of occupancy": "Affidavit of Occupancy",
    "annual certification of occupancy": "Annual Certification of Occupancy",
    "primary residence verification": "Owner Occupancy Certification",

    # Purchase contract
    "purchase contract": "Purchase Contract",
    "purchase contract (fully executed)": "Purchase Contract",
    "fully executed purchase contract": "Purchase Contract",

    # Compliance / business purpose
    "business purpose affidavit": "Borrower Certification as to Business Purpose",
    "business purpose certification": "Borrower Certification as to Business Purpose",
    "borrower certification as to business purpose": "Borrower Certification as to Business Purpose",
    "dscr compliance agreement": "DSCR Documentation",
    "compliance agreement - dscr loans": "DSCR Documentation",
    "dscr documentation": "DSCR Documentation",
    "dscr calculator": "DSCR Calculator",
    "borrower certification": "Borrower Certifications and Disclosure",
    "borrower certifications and disclosure": "Borrower Certifications and Disclosure",
    "patriot act / cip form": "Borrower Certifications and Disclosure",

    # Income - 1099
    "1099-nec": "Form 1099-NEC",
    "1099 nec": "Form 1099-NEC",
    "form 1099-nec": "Form 1099-NEC",
    "1099-misc": "Form 1099-MISC",
    "form 1099-misc": "Form 1099-MISC",
    "consolidated 1099": "Consolidated 1099",
    "1099": "Form 1099-NEC",

    # Income - tax returns
    "tax return - 1040": "Form 1040",
    "tax return (form 1040)": "Form 1040",
    "tax return 1040": "Form 1040",
    "form 1040": "Form 1040",
    "personal tax return": "Form 1040",
    "schedule c": "Form 1040",
    "form 1040a": "Form 1040A",
    "1120 corporate tax return": "1120 Corporate Tax Return",

    # Income - P&L / bank statement
    "year-to-date profit and loss statement": "Profit and Loss",
    "profit and loss statement": "Profit and Loss",
    "p&l statement": "Profit and Loss",
    "ytd p&l": "Profit and Loss",
    "profit and loss": "Profit and Loss",
    "bank statement income analysis": "Non QM Bank Statement Analysis Worksheet",
    "non qm bank statement analysis worksheet": "Non QM Bank Statement Analysis Worksheet",

    # Income - employment
    "written verification of employment": "Verification of Employment",
    "verification of employment": "Verification of Employment",
    "verbal verification of employment": "Verbal Verification of Employment",
    "vvoe": "Verbal Verification of Employment",
    "voe": "Verification of Employment",
    "paystub": "Paystub",
    "paystubs": "Paystub",
    "pay stub": "Paystub",
    "w-2": "W2",
    "w2": "W2",
    "military les paystub": "Military LES Paystub",

    # Income - rental
    "form 1007 rent schedule": "Rental Income Calculations Worksheet",
    "form 1007": "Rental Income Calculations Worksheet",
    "1007 rent schedule": "Rental Income Calculations Worksheet",
    "rent schedule": "Rental Income Calculations Worksheet",
    "rental income calculations worksheet": "Rental Income Calculations Worksheet",
    "lease agreement": "Rental Agreement",
    "rental agreement": "Rental Agreement",
    "lease": "Rental Agreement",

    # Income - LOE
    "letter of explanation - declining income": "Income LOE",
    "letter of explanation - income trend (if applicable)": "Income LOE",
    "letter of explanation - income": "Income LOE",
    "income loe": "Income LOE",
    "loe on employment gap": "LOE on Employment Gap",

    # Income - other
    "business license or cpa letter": "Verification of Employment",
    "business verification letter": "Verification of Employment",
    "cpa verification letter": "Verification of Employment",
    "ssa 1099": "SSA 1099",
    "award letter": "Award Letter",
    "employment contract": "Employment Contract",

    # Assets
    "personal bank statements": "Bank Statement",
    "bank statements": "Bank Statement",
    "bank statement": "Bank Statement",
    "additional bank statements - all accounts": "Bank Statement",
    "business bank statements": "Bank Statement",
    "bank statements - rental income receipt": "Bank Statement",
    "earnest money deposit verification": "Verification of Deposit",
    "emd verification": "Verification of Deposit",
    "earnest money deposit": "Verification of Deposit",
    "verification of deposit": "Verification of Deposit",
    "reserve verification documentation": "Bank Statement",
    "reserve verification": "Bank Statement",
    "reserve calculation worksheet": "Bank Statement",
    "investment account statement": "Investment Account Statement",
    "401k": "401K",
    "401k statement": "401K",
    "wire transfer": "Wire Transfer",
    "wire transfer receipt": "Wire Transfer",
    "gift letter": "Gift",
    "gift": "Gift",
    "gift funds": "Gift",
    "assets loe": "Assets LOE",
    "loe source of large deposits": "LOE Source of Large Deposits",
    "letter of explanation - large deposit": "LOE Source of Large Deposits",
    "large deposit loe": "LOE Source of Large Deposits",

    # Credit
    "credit report": "Credit Report",
    "mortgage payment history": "Verification of Mortgage",
    "mortgage payment history (vom)": "Verification of Mortgage",
    "vom": "Verification of Mortgage",
    "verification of mortgage": "Verification of Mortgage",
    "verification of rent": "Verification of Rent",
    "vor": "Verification of Rent",
    "mortgage statement": "Verification of Mortgage",
    "bankruptcy documents": "Bankruptcy Documents",
    "credit payoffs": "Credit Payoffs",
    "derogatory credit loe": "Derogatory Credit LOE",
    "loe credit docs": "LOE Credit Docs",

    # Property / Appraisal
    "appraisal report": "Appraisal Report",
    "appraisal": "Appraisal Report",
    "appraisal update": "Appraisal Update",
    "appraisal review": "Appraisal Review",
    "appraisal desk review": "Appraisal Desk Review",
    "ssr / ucdp findings": "UCDP SSR",
    "ssr/ucdp findings": "UCDP SSR",
    "ucdp ssr": "UCDP SSR",
    "ssr": "UCDP SSR",
    "flood determination": "Flood Hazard Determination",
    "flood hazard determination": "Flood Hazard Determination",
    "flood certification": "Flood Certification",
    "property insurance": "Hazard Insurance",
    "hazard insurance": "Hazard Insurance",
    "hazard insurance declaration page": "Hazard Insurance",
    "homeowners insurance": "Hazard Insurance",
    "rent loss insurance": "Hazard Insurance",
    "rent loss insurance evidence": "Hazard Insurance",
    "1-4 family rider": "Deed of Trust",
    "property inspection": "Property Inspection",
    "commercial bpo / field review": "Appraisal Report",
    "form 1025 small residential income property appraisal report": "Appraisal Report",
    "condo questionnaire": "Condo PUD Questionnaire",
    "condo pud questionnaire": "Condo PUD Questionnaire",
    "pud questionnaire": "Condo PUD Questionnaire",
    "hoa master insurance": "Hazard Insurance",
    "property loe": "Property LOE",
    "loe collateral": "LOE Collateral",

    # Title / Closing
    "title commitment": "Title Commitment",
    "title insurance": "Title Insurance",
    "vesting deed": "Grant Deed",
    "grant deed": "Grant Deed",
    "warranty deed": "Warranty Deed",
    "quit claim deed": "Quit Claim Deed",
    "deed": "Deed",
    "deed of trust": "Deed of Trust",
    "payoff statement": "Payoff Statement",
    "request for payoff": "Request for Payoff",
    "closing disclosure": "Closing Disclosure",
    "settlement statement": "Settlement Statement",
    "alta settlement statement": "ALTA Settlement Statement",
    "hoa demand": "LOE for HOA Dues",
    "hoa documentation": "LOE for HOA Dues",
    "chain of title": "Chain of Title",
    "assignments of deeds": "Assignments of Deeds",
    "tax cert": "Tax Cert",
    "tax certificate": "Tax Cert",

    # Settlement / source of funds (delayed financing)
    "settlement statement - original purchase": "Settlement Statement",
    "source of funds - original purchase": "Bank Statement",

    # Entity / Trust / Legal
    "trust documents": "Trust Documents",
    "power of attorney": "Power of Attorney",
    "divorce decree": "Divorce Decree",

    # ITIN
    "itin": "ITIN",
    "itin documentation": "ITIN",
    "application for itin w7": "Application for ITIN W7",

    # Misc LOE
    "loe addresses on credit report": "LOE Addresses on Credit Report",
    "loe cash out": "LOE Cash Out",
    "loe name variation": "LOE Name Variation",
    "loe relationship of parties": "LOE Relationship of Parties",
    "loe budget": "LOE Budget",
    "loan info loe": "Loan Info LOE",
    "overdraft loe": "Overdraft LOE",

    # Schedule of RE
    "schedule of real estate owned": "Schedule of Real Estate Owned",

    # Other
    "flood insurance disclosure": "Flood Insurance Disclosure",
    "amortization schedule": "Amortization Schedule",
    "balance sheet": "Balance Sheet",
    "rsu vesting schedule": "RSU Vesting Schedule",
}

# Documents that map to multiple acceptable masterlist types
_MULTI_TYPE_DOCS: dict[str, list[str]] = {
    "Government-Issued Photo ID": [
        "Drivers License",
        "Passport",
        "Non-Driver ID",
        "Permanent Resident Card",
        "Social Security ID",
        "Travel VISA",
    ],
}

# Build the masterlist name set at import time for validation
_MASTERLIST_NAMES: set[str] = set()


def _load_masterlist_names() -> None:
    global _MASTERLIST_NAMES
    if _MASTERLIST_NAMES:
        return
    ml_path = Path(__file__).parent.parent.parent / "data" / "doctype_masterlist.json"
    if ml_path.exists():
        with open(ml_path, encoding="utf-8") as f:
            data = json.load(f)
        _MASTERLIST_NAMES = {d["document_type"] for d in data if d.get("nqm_relevant")}


def normalize_document_type(name: str) -> str:
    """Map an LLM-generated document name to the canonical masterlist name."""
    if not name:
        return "Unknown"

    _load_masterlist_names()

    if name in _MASTERLIST_NAMES:
        return name

    alias = _DOCTYPE_ALIASES.get(name.lower().strip())
    if alias:
        return alias

    return name


def normalize_priority(val: str | None) -> str:
    if not val:
        return "P2"
    key = val.strip().lower()
    if val in _VALID_PRIORITIES:
        return val
    return _PRIORITY_ALIASES.get(key, "P2")


def normalize_severity(val: str | None) -> str:
    if not val:
        return "SOFT-STOP"
    if val in _VALID_SEVERITIES:
        return val
    return _SEVERITY_ALIASES.get(val.strip().lower(), "SOFT-STOP")


def normalize_category(val: str | None) -> str:
    if not val:
        return "Other"
    if val in _VALID_CATEGORIES:
        return val
    return _CATEGORY_ALIASES.get(val.strip().lower(), "Other")


def normalize_document_request(dr: dict, default_category: str = "Other") -> dict:
    """Normalize a document request dict in-place and return it.

    Fixes:
    - document_type: maps to canonical masterlist name
    - priority: maps aliases like "critical" -> "P0", "high" -> "P1"
    - severity: normalizes casing
    - document_category: normalizes casing, uses default_category if missing
    """
    raw_name = dr.get("document_type") or dr.get("document_name") or dr.get("title") or "Unknown"
    canonical = normalize_document_type(raw_name)
    dr["document_type"] = canonical

    if canonical in _MULTI_TYPE_DOCS:
        dr["acceptable_types"] = _MULTI_TYPE_DOCS[canonical]

    dr["priority"] = normalize_priority(dr.get("priority"))
    dr["severity"] = normalize_severity(dr.get("severity"))

    raw_cat = dr.get("document_category") or dr.get("category")
    normalized_cat = normalize_category(raw_cat)
    if normalized_cat == "Other" and default_category != "Other":
        dr["document_category"] = normalize_category(default_category)
    else:
        dr["document_category"] = normalized_cat

    if "status" not in dr:
        dr["status"] = "needed"

    if not isinstance(dr.get("specifications"), list):
        specs = dr.get("specifications")
        if isinstance(specs, str):
            dr["specifications"] = [{"specification": specs}]
        elif specs is None:
            dr["specifications"] = []

    if not isinstance(dr.get("reasons_needed"), list):
        reasons = dr.get("reasons_needed")
        if isinstance(reasons, str):
            dr["reasons_needed"] = [{"reason": reasons}]
        elif reasons is None:
            dr["reasons_needed"] = []

    return dr


def normalize_all(document_requests: list[dict], default_category: str = "Other") -> list[dict]:
    """Normalize a list of document request dicts."""
    for dr in document_requests:
        normalize_document_request(dr, default_category)
    return document_requests
