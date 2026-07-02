"""
doc_rules.py — Deterministic document-set rules for the main pipeline.

Ported and adapted from agent_fast.py's deterministic layers, but keyed to the
MAIN pipeline's scenario_summary schema (occupancy="OO"/"NOO", purpose,
program, property.property_type, income_profile.income_doc_label, etc.).

Design principle: **LLM proposes, deterministic rules dispose.**
The per-step LLM generation is untouched (LLM decision-making retained). At the
STEP_08 merge choke point we reconcile the LLM output against these rules:

  1. MANDATORY floor    — docs that must always be present (guarantees baseline)
  2. CONDITIONAL docs   — docs triggered by unambiguous scenario facts
  3. INCOME docs        — derived deterministically from the eligibility engine's
                          resolved income entries (fixes W2/Paystub/P&L drift)
  4. NEGATIVE gates     — remove docs that are clearly wrong for the scenario
                          (e.g. income docs on a DSCR loan, purchase docs on refi)

Everything the LLM added that isn't gated out is KEPT, so it can still catch
edge-case documents the rules don't know about.
"""

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# Scenario flag derivation
# ---------------------------------------------------------------------------

def derive_flags(ss: dict) -> dict:
    """Normalize the scenario_summary into a flat set of boolean flags."""
    occ = str(ss.get("occupancy") or "").strip().lower()
    purpose = str(ss.get("purpose") or "").strip().lower()
    program = str(ss.get("program") or "").strip().lower()
    dscr_label = str(ss.get("dscr_label") or "").strip().lower()
    prop = ss.get("property") or {}
    prop_type = str(prop.get("property_type") or "").strip().lower()
    reo = ss.get("reo_summary") or {}
    try:
        total_props = int(reo.get("total_properties_owned") or 0)
    except (TypeError, ValueError):
        total_props = 0

    assets = ss.get("asset_profile") or {}

    is_noo = occ in (
        "noo", "investment", "non-owner occupied", "non owner occupied",
        "investor", "investment property",
    )
    is_dscr = "dscr" in program or "dscr" in dscr_label

    return {
        "is_noo": is_noo,
        "is_owner_occupied": not is_noo,
        "is_purchase": "purchase" in purpose,
        "is_refinance": "refi" in purpose,
        "is_dscr": is_dscr,
        "is_condo": "condo" in prop_type,
        "has_reo": total_props > 0,
        "has_large_deposits": bool(assets.get("has_large_deposit_flags")),
        "has_gift": bool(assets.get("has_gift_indicators")),
    }


# ---------------------------------------------------------------------------
# Income-document derivation (from eligibility resolved income entries)
# ---------------------------------------------------------------------------

def _income_docs_for_entry(resolved_doc: str, borrower_type: str) -> list[dict]:
    """Map one resolved income entry to the document(s) it requires."""
    rd = str(resolved_doc or "").lower()
    bt = str(borrower_type or "").lower()
    is_self_employed = "self" in bt

    docs: list[dict] = []

    # DSCR is handled by the DSCR conditional block, not income docs.
    if "dscr" in rd:
        return docs

    if "bank statement" in rd or "bank stmt" in rd:
        docs.append(_doc(
            "Bank Statement", "Income", "P1", "HARD-STOP",
            ["12 or 24 months of consecutive bank statements", "All pages included",
             "Account holder name matches borrower"],
            ["Bank statement income qualification requires the underlying statements"],
        ))
        docs.append(_doc(
            "Non QM Bank Statement Analysis Worksheet", "Income", "P1", "HARD-STOP",
            ["Income calculation methodology documented", "Deposit totals and adjustments",
             "Expense factor applied"],
            ["Bank statement programs require a documented income calculation worksheet"],
        ))
        return docs

    if "p&l" in rd or "pnl" in rd or "profit" in rd:
        docs.append(_doc(
            "Profit and Loss", "Income", "P1", "HARD-STOP",
            ["Covers the required 12 or 24 month period", "Signed by borrower or CPA",
             "Shows gross revenue, expenses, and net income"],
            ["P&L income qualification requires a profit and loss statement"],
        ))
        if "cpa" in rd:
            docs.append(_doc(
                "CPA Prepared P&L Letter", "Income", "P2", "SOFT-STOP",
                ["Prepared and signed by a licensed CPA", "References the subject business",
                 "Confirms the P&L period"],
                ["CPA-prepared P&L programs require CPA attestation"],
            ))
        return docs

    if "1099" in rd:
        docs.append(_doc(
            "1099", "Income", "P1", "HARD-STOP",
            ["Most recent 1 or 2 years of 1099 forms", "Matches income source on application"],
            ["1099 income qualification requires the 1099 forms"],
        ))
        return docs

    if "asset" in rd:
        docs.append(_doc(
            "Asset Depletion Worksheet", "Income", "P1", "HARD-STOP",
            ["Qualifying asset balances documented", "Depletion calculation methodology"],
            ["Asset-based qualification requires an asset depletion calculation"],
        ))
        return docs

    # Full documentation
    if "full doc" in rd or "full documentation" in rd:
        if is_self_employed:
            docs.append(_doc(
                "Form 1040", "Income", "P1", "HARD-STOP",
                ["Most recent 2 years personal federal tax returns", "All pages and schedules",
                 "Signed or IRS transcript accepted"],
                ["Full documentation self-employed borrowers require personal tax returns"],
            ))
            docs.append(_doc(
                "Profit and Loss", "Income", "P2", "SOFT-STOP",
                ["Year-to-date profit and loss statement", "Signed by borrower or CPA"],
                ["Self-employed income requires a current P&L"],
            ))
        else:
            docs.append(_doc(
                "W2", "Income", "P1", "HARD-STOP",
                ["Most recent 2 years W-2 forms", "Matches employer on application"],
                ["Full documentation wage earners require W-2 forms"],
            ))
            docs.append(_doc(
                "Paystub", "Income", "P1", "HARD-STOP",
                ["Most recent 30 days of paystubs", "Shows YTD earnings",
                 "Employer and employee name"],
                ["Full documentation wage earners require recent paystubs"],
            ))
            docs.append(_doc(
                "Verification of Employment", "Income", "P2", "SOFT-STOP",
                ["Employer name and contact", "Position and start date", "Current employment status"],
                ["Employment verification required for wage earners"],
            ))
    return docs


def derive_income_docs(ss: dict) -> list[dict]:
    """Derive required income documents from the eligibility resolved entries.

    Falls back to income_profile.income_doc_label when the resolved-entries
    list is unavailable.
    """
    elig = ss.get("_eligibility_data") or {}
    app = elig.get("application_data") or {}
    entries = app.get("_all_resolved_income_entries") or []

    docs: list[dict] = []
    seen: set[str] = set()

    def _add(doc_list: list[dict]) -> None:
        for d in doc_list:
            key = d["document_type"].strip().lower()
            if key not in seen:
                seen.add(key)
                docs.append(d)

    if entries:
        for e in entries:
            _add(_income_docs_for_entry(
                e.get("resolved_doc", ""), e.get("borrower_type", ""),
            ))
    else:
        # Fallback: single income_doc_label
        label = ss.get("income_profile", {}).get("income_doc_label", "")
        bt = ss.get("borrower_type", "")
        _add(_income_docs_for_entry(label, bt))

    return docs


# ---------------------------------------------------------------------------
# Document builders
# ---------------------------------------------------------------------------

def _doc(
    document_type: str,
    category: str,
    priority: str,
    severity: str,
    specifications: list[str],
    reasons_needed: list[str],
) -> dict:
    return {
        "document_type": document_type,
        "document_category": category,
        "priority": priority,
        "severity": severity,
        "specifications": list(specifications),
        "reasons_needed": list(reasons_needed),
    }


# ---------------------------------------------------------------------------
# Layer 1: Mandatory floor (all loans)
# ---------------------------------------------------------------------------

def mandatory_docs() -> list[dict]:
    return [
        _doc("Government-Issued Photo ID", "Cross-Cutting", "P1", "HARD-STOP",
             ["Valid, unexpired government-issued photo identification for all borrowers",
              "Name matches the loan application"],
             ["Identity verification required for all borrowers"]),
        _doc("IRS 4506-C Authorization", "Cross-Cutting", "P1", "HARD-STOP",
             ["Signed by all borrowers", "Correct tax years and SSN"],
             ["IRS tax transcript authorization required for income/identity verification"]),
        _doc("Credit Report", "Credit", "P0", "HARD-STOP",
             ["Tri-merge credit report from all three bureaus",
              "Credit scores from each reporting bureau", "All borrowers included"],
             ["Credit report required for all loans to verify creditworthiness"]),
        _doc("Bank Statement", "Assets", "P1", "HARD-STOP",
             ["Most recent 2 months", "All pages included",
              "Account holder name and ending balances"],
             ["Asset verification required for funds to close and reserves"]),
        _doc("Verification of Deposit", "Assets", "P2", "SOFT-STOP",
             ["Current and 2-month average balance", "Account holder name"],
             ["Verify sufficient liquid assets for down payment and reserves"]),
        _doc("Appraisal Report", "Property", "P1", "HARD-STOP",
             ["Completed appraisal form (URAR or applicable)", "Subject and comparable photos",
              "Market value opinion and condition assessment"],
             ["Property valuation required for all mortgage lending"]),
        _doc("Flood Hazard Determination", "Property", "P1", "HARD-STOP",
             ["FEMA flood zone determination", "Community and panel number",
              "Property address verification"],
             ["Flood zone determination required for all properties"]),
        _doc("Hazard Insurance", "Property", "P1", "HARD-STOP",
             ["Coverage amount meets or exceeds loan amount / guideline minimum",
              "Named insured matches borrower/entity", "Effective date covers closing"],
             ["Property insurance required to protect the collateral"]),
        _doc("UCDP SSR", "Property", "P2", "SOFT-STOP",
             ["Submission Summary Report from the UCDP portal", "Document ID and submission date"],
             ["Standard quality-control requirement accompanying the appraisal"]),
        _doc("Title Commitment", "Title", "P1", "HARD-STOP",
             ["Schedule A — ownership and property description",
              "Schedule B — exceptions and requirements", "All recorded liens and encumbrances"],
             ["Title examination required to ensure clear and marketable title"]),
        _doc("Deed of Trust", "Title", "P1", "HARD-STOP",
             ["Legal description matches title", "Borrower/grantor name matches application",
              "Lien position identified"],
             ["Security instrument required for the mortgage collateral"]),
        _doc("Owner Occupancy Certification", "Compliance", "P1", "SOFT-STOP",
             ["Borrower certifies intended occupancy", "Signed and dated by all borrowers"],
             ["Occupancy certification required to confirm loan purpose"]),
    ]


# ---------------------------------------------------------------------------
# Layer 2: Conditional docs (unambiguous scenario triggers)
# ---------------------------------------------------------------------------

def conditional_docs(flags: dict) -> list[dict]:
    docs: list[dict] = []

    if flags["is_purchase"]:
        docs.append(_doc("Purchase Contract", "Cross-Cutting", "P1", "HARD-STOP",
             ["Fully executed by all buyers and sellers",
              "Purchase price, property address, closing date",
              "All addenda, amendments, and counter-offers"],
             ["Purchase transactions require a fully executed purchase contract"]))
        docs.append(_doc("Grant Deed", "Title", "P1", "SOFT-STOP",
             ["Current ownership/vesting confirmed", "Legal description matches title",
              "Recording information"],
             ["Evidence of ownership transfer required for purchase transactions"]))

    if flags["is_refinance"]:
        docs.append(_doc("Payoff Statement", "Title", "P1", "SOFT-STOP",
             ["Current payoff amount for existing mortgage", "Per-diem interest and good-through date",
              "Loan account number and lender contact"],
             ["Payoff statement required to determine funds to satisfy existing liens"]))

    if flags["is_noo"]:
        docs.append(_doc("Borrower Certification as to Business Purpose", "Compliance", "P1", "HARD-STOP",
             ["Signed by all borrowers", "Identifies the subject property address",
              "Declares the property will not be owner-occupied"],
             ["Investment/NOO loans require a business-purpose certification"]))
        docs.append(_doc("Rental Agreement", "Income", "P1", "SOFT-STOP",
             ["Current executed lease agreement", "Monthly rent amount",
              "Lease term and tenant information"],
             ["Lease required to document rental income for investment properties"]))
        docs.append(_doc("Verification of Rent", "Income", "P2", "SOFT-STOP",
             ["Tenant verification and monthly rent confirmed", "Payment history if available"],
             ["Rental income verification required for investment properties"]))

    if flags["is_noo"] and flags["has_reo"]:
        docs.append(_doc("Verification of Mortgage", "Credit", "P1", "SOFT-STOP",
             ["12-month payment history for all mortgages", "Current balance and payment amount"],
             ["Mortgage payment verification required for borrowers with existing real estate"]))

    if flags["is_dscr"]:
        docs.append(_doc("Rental Income Calculations Worksheet", "Income", "P1", "HARD-STOP",
             ["DSCR ratio calculation", "Monthly rental income vs PITIA",
              "Property cash-flow analysis"],
             ["DSCR qualification requires a documented rental income calculation"]))
        docs.append(_doc("Rental Agreement", "Income", "P1", "SOFT-STOP",
             ["Current executed lease or market rent schedule (Form 1007/1025)",
              "Monthly rent amount"],
             ["DSCR income is documented via lease or market rent schedule"]))

    if flags["is_condo"]:
        docs.append(_doc("Condo PUD Questionnaire", "Property", "P1", "SOFT-STOP",
             ["HOA budget and financials", "Owner-occupancy ratio", "Litigation and insurance status",
              "Delinquent-dues percentage"],
             ["Condominium project review required for condo property types"]))

    return docs


# ---------------------------------------------------------------------------
# Layer 3: Negative gates (suppress clearly-wrong docs)
# ---------------------------------------------------------------------------

_DSCR_SUPPRESSED = {
    "w2", "w-2", "paystub", "pay stub", "verification of employment",
    "verbal verification of employment", "voe", "form 1040", "form 1040a",
    "form 1040ez", "1040", "profit and loss", "1120 corporate tax return",
    "1065", "tax return", "personal tax return", "business tax return",
    "state tax return", "employment contract", "cpa prepared p&l letter",
    "award letter", "1099",
}

_PURCHASE_SUPPRESSED = {
    "payoff statement", "request for payoff", "payoff demand",
}

_REFINANCE_SUPPRESSED = {
    "purchase contract", "grant deed", "emd check", "earnest money deposit",
}

# Investment/DSCR-only docs that must not appear on an owner-occupied,
# non-DSCR loan (prevents the classification wobble seen in variance testing).
_INVESTMENT_ONLY_SUPPRESSED = {
    "borrower certification as to business purpose", "business purpose affidavit",
    "rental agreement", "lease agreement", "verification of rent", "vor",
    "rental income calculations worksheet", "dscr documentation", "dscr",
    "market rent schedule", "rent loss insurance",
}

# Data-driven docs that only apply when the corresponding asset flag is set.
_LARGE_DEPOSIT_DOCS = {
    "loe source of large deposits", "source of large deposits",
    "large deposit explanation", "loe large deposits",
    "letter of explanation for large deposits",
}
_GIFT_DOCS = {
    "gift", "gift letter", "gift funds", "gift letter and donor documentation",
    "gift funds documentation",
}
# Verification of existing mortgage payment history — only relevant when the
# borrower has an existing mortgage (owns other property, or is refinancing).
_VOM_DOCS = {"verification of mortgage", "vom", "mortgage payment history"}


def apply_negative_gates(docs: list[dict], flags: dict) -> tuple[list[dict], list[str]]:
    """Remove documents that shouldn't exist for this scenario.

    Returns (filtered_docs, removed_type_names).
    """
    removed: list[str] = []
    out: list[dict] = []
    for dr in docs:
        dt = (dr.get("document_type") or "").strip().lower()
        drop = False

        if flags["is_dscr"] and dt in _DSCR_SUPPRESSED:
            drop = True
        elif flags["is_purchase"] and dt in _PURCHASE_SUPPRESSED:
            drop = True
        elif flags["is_refinance"] and dt in _REFINANCE_SUPPRESSED:
            drop = True
        elif (flags["is_owner_occupied"] and not flags["is_dscr"]
              and dt in _INVESTMENT_ONLY_SUPPRESSED):
            drop = True
        elif dt in _LARGE_DEPOSIT_DOCS and not flags["has_large_deposits"]:
            drop = True
        elif dt in _GIFT_DOCS and not flags["has_gift"]:
            drop = True
        elif (dt in _VOM_DOCS and not flags["has_reo"]
              and not flags["is_refinance"]):
            drop = True

        if drop:
            removed.append(dr.get("document_type") or dt)
        else:
            out.append(dr)
    return out, removed


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def apply_deterministic_rules(
    merged: list[dict],
    scenario_summary: dict,
    canonical_fn=None,
) -> tuple[list[dict], dict]:
    """Apply the deterministic layers on top of the LLM-merged document list.

    Args:
        merged: LLM-produced (already merged) document requests.
        scenario_summary: the main-pipeline scenario_summary dict.
        canonical_fn: optional callable(name)->canonical name, used to dedup by
            canonical type when injecting the floor.

    Returns (final_docs, stats).
    """
    flags = derive_flags(scenario_summary)

    def canon(name: str) -> str:
        n = (name or "").strip().lower()
        return canonical_fn(n) if canonical_fn else n

    # Start from the LLM output, apply negative gates first so we don't keep
    # clearly-wrong LLM docs.
    docs, removed = apply_negative_gates(list(merged), flags)

    existing = {canon(dr.get("document_type") or "") for dr in docs}

    injected: list[str] = []

    def _inject(candidates: list[dict]) -> None:
        for cand in candidates:
            # candidate may itself be gated out
            gated, _ = apply_negative_gates([cand], flags)
            if not gated:
                continue
            key = canon(cand.get("document_type") or "")
            if key and key not in existing:
                docs.append(dict(cand, source_module="deterministic"))
                existing.add(key)
                injected.append(cand.get("document_type") or key)

    _inject(mandatory_docs())
    _inject(conditional_docs(flags))
    _inject(derive_income_docs(scenario_summary))

    stats = {
        "flags": flags,
        "removed": removed,
        "injected": injected,
    }
    return docs, stats
