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

    # Bank-statement qualification: only true when EVERY income entry is a
    # bank-statement type (protects multi-borrower loans with a full-doc or
    # wage co-borrower, where traditional income docs are still required).
    income_labels = _income_labels(ss)
    is_bank_statement = bool(income_labels) and all(
        ("bank stmt" in lbl or "bank statement" in lbl) for lbl in income_labels
    )
    # Distinct from is_bank_statement (which requires EVERY entry to be bank
    # statement): this is True if ANY borrower is bank-statement qualified,
    # used only to NEGATIVELY gate bank-statement-only docs (Bank Statement,
    # Non QM Bank Statement Analysis Worksheet) off of loans where NO
    # borrower is bank-statement at all — e.g. observed on a live rerun
    # where the LLM hallucinated a "Non QM Bank Statement Analysis
    # Worksheet" for a Full Doc + CPA-prepared-P&L borrower pair on one
    # rerun but not another. has_any_income_data distinguishes "we know
    # income types and none are bank-statement" (safe to suppress) from
    # "we have no income label data at all" (e.g. DSCR loans return early
    # without labels — don't suppress based on absence of data).
    has_any_income_data = bool(income_labels)
    has_bank_statement_income = any(
        ("bank stmt" in lbl or "bank statement" in lbl) for lbl in income_labels
    )

    is_multi_unit = any(tok in prop_type for tok in ("2-4", "2 unit", "3 unit", "4 unit",
                                                       "duplex", "triplex", "fourplex", "multi"))
    is_5_8_unit = any(tok in prop_type for tok in ("5-8", "5 unit", "6 unit", "7 unit", "8 unit",
                                                     "multi 5", "multi-5"))

    # Normalized income-documentation-type bucket, used to resolve guideline
    # condition strings like "documentation_type=full_doc" (see
    # _resolve_condition below). Falls back to "unknown" (unresolvable) when
    # income entries don't clearly indicate a single bucket, e.g. mixed
    # multi-borrower income types.
    income_doc_type = "unknown"
    if is_dscr:
        income_doc_type = "dscr"
    elif is_bank_statement:
        income_doc_type = "bank_statement"
    elif income_labels:
        if all("1099" in lbl for lbl in income_labels):
            income_doc_type = "1099"
        elif all("asset" in lbl for lbl in income_labels):
            income_doc_type = "asset_utilization"
        elif all(("p&l" in lbl or "pnl" in lbl or "profit" in lbl) for lbl in income_labels):
            income_doc_type = "p_and_l"
        elif all(("full doc" in lbl or "full documentation" in lbl or "wage" in lbl or "w2" in lbl)
                 for lbl in income_labels):
            income_doc_type = "full_doc"

    # Added for the w2/paystub/VOE/profit-and-loss/grant-deed/VOD guideline
    # coverage extension: these condition vocabularies reference
    # borrower_type (self-employed/ITIN/foreign national) and a P&L program
    # sub-variant (Alt Doc P&L-only vs Alt Doc P&L+2mo-bank-statements vs
    # S/E Full Doc supplement) that the flags above didn't previously track.
    elig = ss.get("_eligibility_data") or {}
    app = elig.get("application_data") or {}
    borrower_type_raw = str(ss.get("borrower_type") or "").strip().lower()
    entries = app.get("_all_resolved_income_entries") or []
    is_self_employed_borrower = "self" in borrower_type_raw or any(
        "self" in str(e.get("borrower_type") or "").lower() for e in entries
    )
    citizenship_raw = str(app.get("citizenship") or app.get("Citizenship") or "").strip().lower()
    is_itin = "itin" in citizenship_raw or any("itin" in lbl for lbl in income_labels)
    is_foreign_national = "foreign" in citizenship_raw and "non-foreign" not in citizenship_raw

    # LLC/entity-vesting flag — drives canonical_doc_specs.json's
    # "entity_type=LLC" conditions on the "operating or partnership
    # agreement" entry (Operating Agreement must list owners/ownership %,
    # identify managing members, be signed, etc.). Previously unresolvable
    # (no "entity_type" case existed in _resolve_atomic at all), which
    # silently wiped that doc type's specifications to an empty list on
    # every single run regardless of whether the borrower was actually an
    # LLC — confirmed via a real production sample
    # (consistency_check/omalley_1_10x.json's O'Malley LLC loan had
    # "specifications": [] despite LLCOrLegalEntity=true). Sourced from
    # the eligibility engine's own application_data field (confirmed real
    # key/shape via that same sample: "LLCOrLegalEntity": true,
    # "llc_or_legal_entity": "Yes"). "Partnership" has no equivalent signal
    # available yet, so entity_type=Partnership stays unresolvable
    # (excluded) rather than guessed.
    llc_flag_raw = app.get("LLCOrLegalEntity")
    if llc_flag_raw is None:
        llc_flag_raw = app.get("llc_or_legal_entity")
    if isinstance(llc_flag_raw, bool):
        is_llc = llc_flag_raw
    else:
        is_llc = str(llc_flag_raw or "").strip().lower() in ("true", "yes", "1")

    # "any" (not "all") variant of the full_doc bucket check — needed so W2/
    # Paystub/VOE guideline canonicalization still applies on a mixed-income
    # multi-borrower loan (e.g. one wage-earning co-borrower + one
    # bank-statement-qualified co-borrower), where income_doc_type falls
    # back to "unknown" because not EVERY entry is full_doc.
    has_full_doc_income = any(
        ("full doc" in lbl or "full documentation" in lbl or "wage" in lbl or "w2" in lbl)
        for lbl in income_labels
    )

    income_doc_subtype = None
    if income_doc_type == "p_and_l":
        combo = any(
            ("w/ bank statement" in lbl or "with bank statement" in lbl or "bank statement" in lbl)
            for lbl in income_labels
        )
        income_doc_subtype = "p_and_l_bank_stmt_combo" if combo else "p_and_l_standalone"

    return {
        "is_noo": is_noo,
        "is_owner_occupied": not is_noo,
        "is_purchase": "purchase" in purpose,
        "is_refinance": "refi" in purpose,
        "is_dscr": is_dscr,
        "is_condo": "condo" in prop_type,
        "is_multi_unit": is_multi_unit,
        "is_5_8_unit": is_5_8_unit,
        "is_bank_statement": is_bank_statement,
        "has_any_income_data": has_any_income_data,
        "has_bank_statement_income": has_bank_statement_income,
        "income_doc_type": income_doc_type,
        "income_doc_subtype": income_doc_subtype,
        "has_full_doc_income": has_full_doc_income,
        "is_self_employed_borrower": is_self_employed_borrower,
        "is_itin": is_itin,
        "is_foreign_national": is_foreign_national,
        "is_llc": is_llc,
        "has_reo": total_props > 0,
        "has_large_deposits": bool(assets.get("has_large_deposit_flags")),
        "has_gift": bool(assets.get("has_gift_indicators")),
        "has_loan_application": _has_loan_application(ss),
    }


# Tokens identifying the borrower's loan application (URLA/1003) among
# submitted documents. Kept intentionally narrower than merger_tools.py's
# full _DOCTYPE_ALIASES table (which also handles addendum-vs-primary
# precedence, multi-borrower-1003 merging, etc.) -- this only needs a
# presence check, and importing from merger_tools here would create a
# circular import (merger_tools already imports doc_rules at call time).
_LOAN_APPLICATION_TOKENS = ("1003", "urla", "loan application", "loan_application")


def _has_loan_application(ss: dict) -> bool:
    """True when the borrower's loan application (URLA/1003) was itself
    submitted as a document in this manifest.

    Gates specs that can only be checked against data that lives on the
    loan application itself -- e.g. the Credit Report's "verification of
    all credit references provided on the loan application" spec cross-
    checks the report's tradeline lender names against the liabilities the
    borrower declared on their 1003. Without the 1003 in the file at all,
    there is no reference list to check against, so leaving that spec
    "unsatisfied"/open is misleading -- it reads as a real documentation
    gap when it's actually "not checkable, the loan application wasn't
    submitted." Observed live on Mark Kashana's file: no 1003/URLA/loan
    application document anywhere in the manifest, yet this spec still
    showed up as a permanently-open Credit Report requirement.
    """
    for sdoc in ss.get("_submitted_docs") or []:
        name = str(sdoc.get("name") or "").strip().lower()
        dtype = str(sdoc.get("doc_type") or "").strip().lower()
        if any(tok in name or tok in dtype for tok in _LOAN_APPLICATION_TOKENS):
            return True
    return False


def _income_labels(ss: dict) -> list[str]:
    """Return lowercased income-doc labels, one per resolved income entry
    (falling back to the single income_doc_label when entries are absent)."""
    elig = ss.get("_eligibility_data") or {}
    app = elig.get("application_data") or {}
    entries = app.get("_all_resolved_income_entries") or []
    if entries:
        return [str(e.get("resolved_doc") or "").lower() for e in entries]
    label = str((ss.get("income_profile") or {}).get("income_doc_label") or "").lower()
    return [label] if label else []


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
                # This is the deterministic FLOOR/backstop for Form 1040 —
                # it only fires when the LLM income-requirements step
                # (STEP_02) didn't already produce its own Form 1040 entry
                # (see apply_deterministic_rules' existing-key dedup), so
                # it's the only thing guaranteeing this borrower's Form
                # 1040 requirement even shows up at all. Wording below is
                # matched 1:1 to the richer LLM-authored version (module 02)
                # seen in production, rather than a distinct shorthand, so
                # the floor and the LLM path never present differently
                # worded/differently-scoped requirements for the same
                # document depending purely on which one happened to fire.
                ["Must include complete personal tax returns for the most recent 2 tax years",
                 "Must include all schedules and attachments (Schedule E, Schedule K-1, etc.)",
                 "Must show borrower name and SSN matching loan application",
                 "Must be signed and dated or include electronic filing confirmation",
                 # Self-employed borrowers are frequently S-corp/partnership
                 # shareholders (25%+ ownership already qualifies as
                 # self-employed per NQMF guidelines), whose K-1 is a
                 # SEPARATE physical document from the 1040 itself. Kept as
                 # its own explicit spec (not folded into the "all schedules"
                 # line above) so it can't be silently dropped, and so it
                 # gives cross_check_satisfaction's K-1-companion-document
                 # lookup (merger_tools._find_k1_companion_fields) a "k-1"
                 # keyword to trigger on and verify against any K-1 actually
                 # submitted in the file.
                 "Must include Schedule K-1 showing S-Corporation distributions and ownership percentage"],
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
# Eligibility-engine required document categories
# ---------------------------------------------------------------------------
#
# The eligibility engine (parse_eligibility_output, tools/scenario_tools.py)
# evaluates the loan against each program's rule set and surfaces a
# "Minimum Required Documents" / "Income Documentation" requirement check
# per program, whose `expected` keys are the document CATEGORIES that
# program mandates (e.g. "Business License", "Ownership Interest
# Certification", "Title Invoice") — stored on
# scenario_summary._eligibility_data.required_doc_categories. Today this is
# surfaced ONLY as informational text in a ToolMessage; nothing guarantees
# these categories actually turn into document_requests — it's entirely up
# to the per-module LLM steps (01-07) to happen to generate a matching
# document this run. Since the eligibility engine treats these as
# pass/fail program-qualification checks, silently dropping one is a real
# underwriting-completeness risk, not just cosmetic wording drift.
#
# _ELIGIBILITY_CATEGORY_DOC_MAP maps each known category name to the
# document template it corresponds to. Categories that already have a
# dedicated deterministic source (Credit Report/URLA via mandatory_docs(),
# Bank Statement/Profit and Loss via derive_income_docs(), and — as of the
# submission-requirements-coverage audit below — Title Invoice/Borrower
# Authorization, now ALSO in mandatory_docs()) map to the SAME canonical
# document_type, so apply_deterministic_rules' existing-key dedup naturally
# no-ops instead of double-injecting. This map stays in place even for
# those two as a second, independent backstop: mandatory_docs() guarantees
# them unconditionally, this map additionally merges in the eligibility
# engine's own program-specific reasoning (via _merge_reasons in _inject)
# whenever the eligibility JSON actually flags the category. Categories
# with no OTHER deterministic source (Business License, Ownership Interest
# Certification, ITIN, LoanNex/Prequal) get a new floor doc here so they
# can no longer be silently omitted by LLM run-to-run variance.
# ---------------------------------------------------------------------------

def _eligibility_category_doc_map() -> dict[str, dict]:
    return {
        "credit report": _doc(
            "Credit Report", "Credit", "P0", "HARD-STOP",
            ["Tri-merge credit report from all three bureaus",
             "Credit scores from each reporting bureau", "All borrowers included"],
            ["Credit report required for all loans to verify creditworthiness"],
        ),
        "urla 1003": _doc(
            "Loan Application (1003)", "Cross-Cutting", "P1", "HARD-STOP",
            ["Final signed and dated URLA (Form 1003) for all borrowers",
             "All sections complete and consistent with the loan terms and program"],
            ["Final signed loan application required for all loans"],
        ),
        # Alias variants observed in the wild for the same two categories —
        # mapped to the SAME canonical document_type as their base entry so
        # they dedup against mandatory_docs()'s floor instead of creating a
        # redundant second doc (confirmed via multi-loan audit: niccum's
        # eligibility engine used "Initial Loan Application (1003)" and
        # "LoanNex Product and Pricing Results or Completed Submission
        # Form" instead of the shorter names, producing exact-string-match
        # duplicates before this was added).
        "initial loan application (1003)": _doc(
            "Loan Application (1003)", "Cross-Cutting", "P1", "HARD-STOP",
            ["Final signed and dated URLA (Form 1003) for all borrowers",
             "All sections complete and consistent with the loan terms and program"],
            ["Final signed loan application required for all loans"],
        ),
        "borrower authorization (when nqmf is pulling credit)": _doc(
            "Borrower Authorization", "Cross-Cutting", "P1", "HARD-STOP",
            ["Signed authorization for the lender to verify credit, employment, "
             "income, and asset information", "Signed and dated by all borrowers"],
            ["Program eligibility requires a general borrower authorization on file"],
        ),
        # Explicit entry so the display name matches the eligibility engine's
        # own normalized acronym ("ITIN") instead of falling through to the
        # generic `.title()` fallback, which mangles it to "Itin".
        "itin": _doc(
            "ITIN", "Cross-Cutting", "P1", "HARD-STOP",
            ["ITIN Card or Letter from the IRS (ITIN Approval Letter / CP-565)",
             "ITIN must be assigned to the borrower prior to application"],
            ["Program eligibility engine flagged ITIN documentation as a minimum "
             "required document category for this borrower/program"],
        ),
        "loannex product and pricing results or completed submission form": _doc(
            "Prequal Response Form", "Cross-Cutting", "P1", "HARD-STOP",
            ["LoanNex product and pricing results matching the loan program, rate, and price "
             "reflected in the loan file — or, if LoanNex results are unavailable, the "
             "completed Submission Form (Program, Loan Purpose, Loan Amount, Appraised Value, "
             "Purchase Price, Occupancy, Property Type, Product Type, Term, Interest Rate, "
             "Interest Only, 2/1 Buydown, and Prepayment Penalty selections), or a signed Rate "
             "Lock Confirmation / Interest Rate Lock In Agreement confirming the locked rate "
             "and terms"],
            ["Required for all transactions per NQMF submission requirements — confirms the "
             "priced product/rate/terms match the loan file"],
        ),
        "bank statement": _doc(
            "Bank Statement", "Income", "P1", "HARD-STOP",
            ["12 or 24 months of consecutive bank statements", "All pages included",
             "Account holder name matches borrower"],
            ["Program eligibility requires bank-statement income documentation"],
        ),
        "profit and loss": _doc(
            "Profit and Loss", "Income", "P1", "HARD-STOP",
            ["Covers the required 12 or 24 month period", "Signed by borrower or CPA",
             "Shows gross revenue, expenses, and net income"],
            ["Program eligibility requires a profit and loss statement"],
        ),
        "borrower authorization": _doc(
            "Borrower Authorization", "Cross-Cutting", "P1", "HARD-STOP",
            ["Signed authorization for the lender to verify credit, employment, "
             "income, and asset information", "Signed and dated by all borrowers"],
            ["Program eligibility requires a general borrower authorization on file"],
        ),
        # Distinct from "borrower authorization" above — this is evidence
        # that whoever is signing the loan/closing docs is actually
        # authorized to bind the entity (LLC/partnership), not a credit-
        # pull authorization. Per data/guidelines.md ("If all members are
        # not borrowers, evidence the borrower has authority to sign on
        # behalf of the entity... can be validated through the Operating
        # Agreement or Certificate of Authorization. If not available, a
        # Borrowing Certificate is required"), so Operating Agreement,
        # Certificate of Authorization, or a Borrowing Certificate can all
        # satisfy it — added as satisfaction aliases in
        # tools/merger_tools.py's _DOCTYPE_ALIASES rather than folded away
        # like "llc member list" was, since this one has its own real
        # canonical_doc_specs.json content and isn't purely redundant with
        # the Operating Agreement. Was previously falling through to the
        # generic eligibility placeholder (P2/SOFT-STOP, generic wording)
        # despite the eligibility engine flagging it as a key in the
        # "Minimum Required Documents" expected dict on every LLC loan —
        # given an explicit HARD-STOP entry here instead.
        "borrower authorization to sign": _doc(
            "Borrower Authorization to Sign", "Cross-Cutting", "P1", "HARD-STOP",
            ["Must identify the individual being authorized to sign loan "
             "and/or closing documents",
             "Must identify the borrower or entity on whose behalf the "
             "individual is authorized to sign",
             "Must be signed and dated by the borrower (or an authorized "
             "officer/manager of the entity) granting the authorization",
             "Must specify the scope of documents/transactions the "
             "authorization covers"],
            ["Program eligibility engine flagged evidence of authority to "
             "sign on behalf of the entity as required for this LLC/entity-"
             "owned loan"],
        ),
        "business license": _doc(
            "Business License", "Income", "P1", "HARD-STOP",
            ["Current, unexpired business license (or equivalent registration) "
             "for the borrower's self-employed business",
             "Business name matches the business name on the loan application"],
            ["Program eligibility requires proof of active business licensure "
             "for self-employed borrowers"],
        ),
        # Surfaced via _extract_required_doc_categories's "HasXxx" single-
        # field detection (tools/scenario_tools.py) — the eligibility
        # engine flags this as "HasCPALetter" (a boolean, not a
        # "Minimum Required Documents" dict key) for Foreign National Full
        # Doc Self-Employed borrowers. Per data/guidelines.md: "CPA Letter
        # with most recent 2 years income & YTD Earnings" (Foreign National
        # – Full Doc Self Employed) and data/submission_documents.md's
        # Foreign National row. Added so this gets real guideline-sourced
        # specs instead of the generic eligibility-category placeholder.
        "cpa letter": _doc(
            "CPA Letter", "Income", "P1", "HARD-STOP",
            ["Letter from the borrower's licensed CPA, on CPA letterhead, "
             "signed and dated",
             "States the borrower's self-employment income for the most "
             "recent 2 years and year-to-date earnings"],
            ["Foreign National Full Doc Self-Employed borrowers must document "
             "self-employment income via a CPA letter per NQMF guidelines"],
        ),
        "ownership interest certification": _doc(
            "Ownership Interest Certification", "Income", "P1", "HARD-STOP",
            ["Certifies borrower's percentage of ownership interest in the business",
             "Signed and dated by the borrower",
             "Ownership percentage consistent with tax returns/K-1"],
            ["Program eligibility requires certification of the borrower's "
             "ownership percentage in the qualifying business"],
        ),
        "title invoice": _doc(
            "Title Invoice", "Title", "P2", "SOFT-STOP",
            ["Itemized title/closing fees from the title company",
             "Matches fees disclosed on the Closing Disclosure/Loan Estimate"],
            ["Program eligibility requires the title company's itemized invoice"],
        ),
        # The eligibility engine flags "LLC Member List" as its own
        # category (confirmed via real omalley eligibility.json: a key in
        # the "Minimum Required Documents" expected-dict), but per request
        # its content — members/ownership %, managing member ID — is
        # already covered by the Operating Agreement and kept there
        # instead of as a second, redundant document (see
        # data/canonical_doc_specs.json's "operating or partnership
        # agreement" entry, and tools/merger_tools.py's
        # _DOCTYPE_ALIASES "operating or partnership agreement" entry for
        # the satisfaction-matching side). Mapped directly to that
        # document_type here (rather than left to fall through to the
        # generic placeholder + alias-based merge-time dedup) so this
        # collapses correctly even on the very first injection, before any
        # other module has drafted an Operating Agreement request yet.
        "llc member list": _doc(
            "Operating Or Partnership Agreement", "Income", "P1", "HARD-STOP",
            ["Operating Agreement must contain a list of owners along with "
             "titles and their respective ownership percentages"],
            ["Program eligibility engine flagged LLC member/ownership "
             "documentation as required for this LLC-owned loan — satisfied "
             "by the Operating Agreement rather than a separate document"],
        ),
    }


# ---------------------------------------------------------------------------
# Layer 1b: Submission Requirements checklist floor (required_documents_json)
# ---------------------------------------------------------------------------
#
# Maps the Submission Requirements checklist's own `category` field onto
# this pipeline's canonical document_type display names. NOTE: this is a
# SEPARATE vocabulary from Tasktile's manifest category_id/
# CATEGORY_ID_TO_DOC_TYPE (tools/shared/manifest_parser.py) — confirmed by
# cross-checking the one real payload seen so far (Sahay Vibhor
# Binayprasad, thread b152c710-7746-4b3b-bc57-1ae6977ca5c9): category_ids
# 349/117/502/237 do coincidentally match our Tasktile map, but 14/17/
# 2211/2174/2173/2064 do not appear in it at all, nor in any real
# submitted-document sample in this repo — so `category_ids` is NOT used
# as the primary join key here, only `category` (this checklist's own
# string enum). Unmapped categories still get a humanized fallback rather
# than being silently dropped (e.g. the newly-added "borrower_authorization
# _to_sign" / "llc_member_list" categories the frontend mentioned).
_REQUIRED_DOCS_CATEGORY_MAP: dict[str, str] = {
    "urla_1003": "Loan Application (1003)",
    "credit_report": "Credit Report",
    "bank_statement": "Bank Statement",
    "borrowers_authorization": "Borrower Authorization",
    "title_invoice": "Title Invoice",
    "non_qm_bank_statement_analysis_worksheet": "Non QM Bank Statement Analysis Worksheet",
    "business_license": "Business License",
    "profit_and_loss": "Profit and Loss",
    "ownership_interest_certification": "Ownership Interest Certification",
}


def _humanize_required_doc_category(category: str, label: str) -> str:
    """Fallback display name for a checklist category this table hasn't
    been taught yet, so nothing from the authoritative checklist silently
    vanishes just because its category string is unrecognized."""
    base = category or label
    return str(base).replace("_", " ").strip().title()


def required_documents_floor(ss: dict) -> list[dict]:
    """Return floor documents for every item on the loan's Submission
    Requirements checklist (required_documents_json — the same
    `minimum_required_documents` item the Submission Requirements tab
    renders). Every checklist item is guaranteed to be represented as a
    document request (via the bypass_negative_gates injection path), on
    top of — not instead of — whatever STEP_01-07's own guideline
    reasoning already produced.

    The checklist's own `status` ("completed"/"pending") is carried into
    reasons_needed as a supplementary signal, but the final
    satisfied-vs-outstanding determination is still left to this
    pipeline's own manifest-grounded satisfaction check at STEP_08 rather
    than trusted blindly — our own evidence can be more current than the
    checklist snapshot, and this keeps a single source of truth for
    status instead of two potentially-conflicting ones."""
    data = ss.get("_required_documents_data") or {}
    items = data.get("documents") or []
    program_name = data.get("program_name") or "this loan's program"

    docs: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        if not label:
            continue
        category = str(item.get("category") or "").strip().lower()
        status = item.get("status")
        doc_type = _REQUIRED_DOCS_CATEGORY_MAP.get(category) or \
            _humanize_required_doc_category(category, label)

        if status == "completed":
            status_note = (
                f'Submission Requirements checklist already marks this item as '
                f'completed (checklist label: "{label}") — verify against the '
                f"manifest rather than assuming satisfied."
            )
        else:
            status_note = (
                f'Submission Requirements checklist marks this item as pending/'
                f'outstanding (checklist label: "{label}").'
            )

        docs.append(_doc(
            doc_type, "Cross-Cutting", "P1", "HARD-STOP",
            [label],
            [f"Required by the loan's Submission Requirements checklist for "
             f"{program_name}.", status_note],
        ))
    return docs


def eligibility_required_docs(ss: dict) -> list[dict]:
    """Return floor documents for every category the eligibility engine
    flagged as a minimum-required-document for the qualifying program.
    Unmapped/unknown category names still get a generic floor doc (rather
    than being silently dropped) so nothing from the authoritative
    eligibility list can vanish just because this table hasn't been taught
    that category name yet."""
    elig = ss.get("_eligibility_data") or {}
    categories = elig.get("required_doc_categories") or []
    doc_map = _eligibility_category_doc_map()
    docs: list[dict] = []
    seen: set[str] = set()
    for cat in categories:
        key = str(cat or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        template = doc_map.get(key)
        if template is None:
            # Unknown category — still guarantee SOMETHING gets requested
            # rather than silently dropping an eligibility-flagged
            # requirement just because this table hasn't been taught it yet.
            template = _doc(
                str(cat).strip().title(), "Cross-Cutting", "P2", "SOFT-STOP",
                [f"Provide documentation satisfying the \"{cat}\" requirement "
                 "flagged by the eligibility engine for this program"],
                ["Program eligibility engine flagged this as a minimum "
                 "required document category"],
            )
        docs.append(template)
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
        _doc("Loan Application (1003)", "Cross-Cutting", "P1", "HARD-STOP",
             ["Final signed and dated URLA (Form 1003) for all borrowers",
              "All sections complete and consistent with the loan terms and program"],
             ["Final signed loan application required for all loans"]),
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
        _doc("Prequal Response Form", "Cross-Cutting", "P1", "HARD-STOP",
             ["LoanNex product and pricing results matching the loan program, rate, and price "
              "reflected in the loan file — or, if LoanNex results are unavailable, the "
              "completed Submission Form (Program, Loan Purpose, Loan Amount, Appraised Value, "
              "Purchase Price, Occupancy, Property Type, Product Type, Term, Interest Rate, "
              "Interest Only, 2/1 Buydown, and Prepayment Penalty selections), or a signed Rate "
              "Lock Confirmation / Interest Rate Lock In Agreement confirming the locked rate "
              "and terms"],
             ["Required for all transactions per NQMF submission requirements — confirms the "
              "priced product/rate/terms match the loan file"]),
        # Title Fee Sheet / "Smart Fee" (data/submission_documents.md, "All
        # Transactions" row) — promoted from eligibility-only
        # (_eligibility_category_doc_map's "title invoice" entry) to the
        # universal floor: this item is required on every NQMF submission,
        # not just when the eligibility engine happens to flag "title
        # invoice" as a missing-doc category for the qualifying program.
        # Audited (10x consistency run across bibby/montes/segoviano/pisa/
        # omalley/kashana/kelly_goldberg/otodo_augustine/pullings) — this
        # item was absent on every single loan that should have listed it,
        # confirming the eligibility-only path alone wasn't reliably
        # surfacing it.
        _doc("Title Invoice", "Title", "P1", "HARD-STOP",
             ["Itemized title/closing fees from the title company",
              "Matches fees disclosed on the Closing Disclosure/Loan Estimate"],
             ["Required for all transactions per NQMF submission requirements "
              "(Title Fee Sheet / Smart Fee)"]),
        # Borrower Authorization (data/submission_documents.md, "All
        # Transactions" row: "Borrower Certification Form — if NQMF is
        # pulling credit"). Previously relied solely on the eligibility
        # engine flagging a "borrower authorization" missing-doc category
        # (_eligibility_category_doc_map) AND was explicitly blocked from
        # organic LLM generation by plans/step_01_cross_cutting.md's old
        # "DO NOT include — the 4506-C covers it" instruction — the 4506-C
        # only authorizes IRS tax-transcript retrieval, it does NOT cover
        # general credit/employment/income/asset verification, so it was
        # never actually a substitute. Promoted to the universal floor so
        # this doesn't depend on either the plan wording or the eligibility
        # engine's per-run category output.
        _doc("Borrower Authorization", "Cross-Cutting", "P1", "HARD-STOP",
             ["Signed authorization for the lender to verify credit, employment, "
              "income, and asset information", "Signed and dated by all borrowers"],
             ["Required for all transactions per NQMF submission requirements "
              "when NQMF is pulling credit"]),
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
        # "Copy of EMD Check / Receipt" — data/submission_documents.md
        # marks this "as applicable for purchase" (i.e. purchase-only,
        # unlike Purchase Contract/Grant Deed which are universal for
        # purchases). Had NO deterministic source at all before this —
        # not in mandatory_docs(), not in conditional_docs(), not in
        # _eligibility_category_doc_map() — entirely dependent on the
        # per-module LLM happening to generate it, which audits showed it
        # consistently did not. See normalize.py's "emd check"/"copy of
        # emd check" aliases for how LLM-phrased variants map onto this
        # canonical type.
        docs.append(_doc("EMD Check", "Title", "P2", "SOFT-STOP",
             ["Copy of the earnest money deposit check or receipt",
              "Amount matches the EMD disclosed in the purchase contract"],
             ["Purchase transactions require evidence the earnest money "
              "deposit was paid, per NQMF submission requirements"]))

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

    # Injected whenever has_reo OR is_refinance — mirrors _VOM_DOCS' own
    # negative-gate keep-condition below (`not has_reo and not is_refinance`
    # drops it), so VOM's presence is no longer purely up to whichever way
    # the per-module LLM happened to lean this run. Previously this only
    # fired on is_noo AND has_reo, which left an owner-occupied cash-out
    # refi with 1+ REO property (e.g. Segoviano) with ZERO deterministic
    # backing for VOM — confirmed via 3x rerun testing to flip the doc-type
    # set 1-in-3 runs even though occupancy/REO/purpose were 100% stable
    # across those reruns (see _segoviano_consistency.py results).
    if flags["has_reo"] or flags["is_refinance"]:
        docs.append(_doc("Verification of Mortgage", "Credit", "P1", "SOFT-STOP",
             ["12-month payment history for all mortgages", "Current balance and payment amount"],
             ["Mortgage payment verification required for borrowers with existing real estate "
              "or an existing mortgage being refinanced"]))

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

# Bank-statement income loans qualify on deposits, not wage/full-doc income —
# suppress the traditional income docs an LLM may over-request.
_BANK_STATEMENT_SUPPRESSED = {
    "w2", "w-2", "paystub", "pay stub", "verification of employment",
    "verbal verification of employment", "voe", "form 1040", "form 1040a",
    "form 1040ez", "1040", "profit and loss", "cpa prepared p&l letter",
    "1120 corporate tax return", "1065", "tax return", "personal tax return",
    "business tax return", "state tax return", "employment contract",
    "award letter", "1099",
}

_PURCHASE_SUPPRESSED = {
    "payoff statement", "request for payoff", "payoff demand",
}

_REFINANCE_SUPPRESSED = {
    "purchase contract", "grant deed", "emd check", "earnest money deposit",
}

# Bank-statement-only docs that must not appear when NO borrower on the
# loan is actually bank-statement qualified — added after a live rerun
# audit (alaska: Full Doc + CPA-prepared-P&L borrower pair) showed the LLM
# hallucinating a "Non QM Bank Statement Analysis Worksheet" on one rerun
# but not another, with no existing gate to catch it (only the reverse
# gate existed: _BANK_STATEMENT_SUPPRESSED removes traditional income docs
# FROM bank-statement scenarios, but nothing removed bank-statement docs
# from non-bank-statement scenarios). Gated on has_any_income_data so we
# never suppress based on absence of data (e.g. DSCR loans, which return
# early from _income_labels without any entries).
_BANK_STATEMENT_ONLY_SUPPRESSED = {
    "bank statement", "non qm bank statement analysis worksheet",
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
        elif flags["is_bank_statement"] and dt in _BANK_STATEMENT_SUPPRESSED:
            drop = True
        elif flags["is_purchase"] and dt in _PURCHASE_SUPPRESSED:
            drop = True
        elif flags["is_refinance"] and dt in _REFINANCE_SUPPRESSED:
            drop = True
        elif (flags["is_owner_occupied"] and not flags["is_dscr"]
              and dt in _INVESTMENT_ONLY_SUPPRESSED):
            drop = True
        elif (dt in _BANK_STATEMENT_ONLY_SUPPRESSED and flags["has_any_income_data"]
              and not flags["has_bank_statement_income"]):
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
    # Keyed lookup (first match wins) so a duplicate eligibility-engine
    # candidate can MERGE its reasons_needed into the doc that already
    # exists under this canonical type, instead of being silently dropped
    # entirely — see the bypass_negative_gates branch below.
    existing_by_key: dict[str, dict] = {}
    for _dr in docs:
        _k = canon(_dr.get("document_type") or "")
        if _k and _k not in existing_by_key:
            existing_by_key[_k] = _dr

    injected: list[str] = []

    def _merge_reasons(target: dict, new_reasons: list) -> None:
        """Append any of new_reasons not already present (case-insensitive,
        whitespace-normalized) onto target["reasons_needed"] in place."""
        current = target.get("reasons_needed")
        if not isinstance(current, list):
            current = list(current) if current else []
            target["reasons_needed"] = current
        seen = {" ".join(str(r).strip().lower().split()) for r in current}
        if isinstance(new_reasons, list):
            candidates = new_reasons
        elif new_reasons:
            candidates = [new_reasons]
        else:
            candidates = []
        for r in candidates:
            norm = " ".join(str(r).strip().lower().split())
            if norm and norm not in seen:
                current.append(r)
                seen.add(norm)

    def _inject(candidates: list[dict], bypass_negative_gates: bool = False) -> None:
        for cand in candidates:
            # candidate may itself be gated out — UNLESS bypass_negative_gates
            # is set, which is used for eligibility_required_docs(): those
            # candidates are authoritative, program-specific requirements
            # straight from the eligibility engine's own pass/fail rule
            # evaluation, not generic LLM over-requests, so they shouldn't
            # be silently dropped by the broad heuristic negative gates
            # (e.g. _BANK_STATEMENT_SUPPRESSED normally drops "Profit and
            # Loss" for bank-statement-qualified borrowers as a reasonable
            # general heuristic — but if THIS program's eligibility
            # evaluation explicitly requires P&L anyway, that authoritative
            # signal should win). Confirmed via multi-loan audit
            # (_multi_input_audit.py): niccum/pisa are bank-statement
            # self-employed borrowers whose Profit and Loss requirement was
            # being silently eaten by this exact gate on every run.
            if not bypass_negative_gates:
                gated, _ = apply_negative_gates([cand], flags)
                if not gated:
                    continue
            key = canon(cand.get("document_type") or "")
            if key and key not in existing:
                docs.append(dict(cand, source_module="deterministic"))
                existing.add(key)
                existing_by_key[key] = docs[-1]
                injected.append(cand.get("document_type") or key)
            elif key and bypass_negative_gates:
                # A document under this canonical type was ALREADY drafted
                # (almost always the case for eligibility_required_docs()
                # candidates like Bank Statement / Profit and Loss / Business
                # License on any loan where that income type is actually in
                # play) — so the candidate itself is correctly not injected
                # as a duplicate. But eligibility_required_docs()'s whole
                # point is that these reasons come from the eligibility
                # engine's own authoritative per-program pass/fail
                # evaluation ("Program eligibility requires bank-statement
                # income documentation" for THIS specific qualifying
                # program), not generic guideline text — silently dropping
                # that reasoning just because a doc already existed under
                # this name loses the one signal that made this doc
                # authoritative rather than just LLM-guessed. Merge it into
                # the existing doc's reasons_needed instead (specifications
                # are left alone — those get wholesale-replaced by
                # apply_guideline_canonicalization()'s canonical library
                # right after this runs, so injecting eligibility-specific
                # spec text here would just be discarded anyway).
                target = existing_by_key.get(key)
                if target is not None:
                    _merge_reasons(target, cand.get("reasons_needed"))

    _inject(mandatory_docs())
    _inject(conditional_docs(flags))
    _inject(derive_income_docs(scenario_summary))
    _inject(eligibility_required_docs(scenario_summary), bypass_negative_gates=True)
    _inject(required_documents_floor(scenario_summary), bypass_negative_gates=True)

    stats = {
        "flags": flags,
        "removed": removed,
        "injected": injected,
    }
    return docs, stats


# ---------------------------------------------------------------------------
# Layer 4: Spec canonicalization (Credit Report core specs)
# ---------------------------------------------------------------------------
#
# The per-module LLM steps (01-07) regenerate a Credit Report document's
# `specifications` list from scratch every run, in fresh natural-language
# wording each time — even though the underlying set of *concepts* being
# requested (tri-merge, all-borrowers, scores, tradelines, mortgage
# history, inquiries/public-records/collections, recency, public-record
# search certification, credit-reference verification) is fixed by
# guideline requirements and doesn't actually vary by loan. This produces
# spec-count and spec-wording drift across reruns of the SAME loan file
# (observed via _segoviano_consistency.py: 9/9/11 specs across 3 identical
# reruns, with 3+ distinct phrasings of the same "must show tradelines"
# concept — see plans/... consistency investigation).
#
# mandatory_docs()'s Credit Report floor (Layer 1) can't fix this: it only
# INJECTS the doc when it's entirely missing, and Credit Report is
# virtually always already present from the LLM merge, so the floor's
# fixed specs never actually get used.
#
# canonicalize_credit_report_specs() instead rewrites any spec matching a
# known concept's keyword set into ONE fixed canonical wording (collapsing
# duplicates/splits across the concept), and injects the concept if the
# LLM omitted it entirely this run — while leaving any spec that doesn't
# match a known concept (e.g. a dynamic "credit report already on file,
# dated X" note) untouched, since that carries genuinely loan-specific
# information the canon list can't predict. This does NOT change
# satisfaction logic (run_satisfaction_pass / _llm_check_specs still decide
# satisfied-vs-remaining against the extracted fields) — it only
# stabilizes the *wording and presence* of what's being asked for, so the
# same underlying requirement reads identically every run.
# ---------------------------------------------------------------------------

_CREDIT_REPORT_SPEC_CONCEPTS: list[tuple[str, tuple[str, ...], str]] = [
    (
        "tri_merge",
        ("tri-merge", "tri merge", "three bureaus", "three major bureaus"),
        "Must be a tri-merge or Residential Mortgage Credit Report (RMCR) "
        "from all three bureaus (Experian, TransUnion, Equifax)",
    ),
    (
        "all_borrowers",
        ("all borrowers", "each borrower"),
        "Must include and identify all borrowers on the loan",
    ),
    (
        "credit_scores",
        ("credit score",),
        "Must show credit scores from all three bureaus (representative "
        "score is the middle of three, or the lower of two, per "
        "underwriting guidelines)",
    ),
    (
        "tradelines",
        ("tradeline", "trade line"),
        "Must show complete tradeline details, including payment history "
        "and account status, for all listed accounts",
    ),
    (
        "mortgage_history",
        ("mortgage history", "mortgage account"),
        "Must show mortgage history for the subject property and any "
        "other properties owned, if available",
    ),
    (
        "adverse_items",
        ("inquir", "public record", "collection", "charge-off", "charge off",
         "dispute", "alert"),
        "Must show credit inquiries within the most recent 90 days, plus "
        "any public records, collections, charge-offs, disputes, and "
        "fraud alerts",
    ),
    (
        "recency",
        ("recency", "dated within", "age of documentation"),
        "Must be dated within the allowed recency window per the Age of "
        "Documentation Policy",
    ),
    (
        "public_record_search_certification",
        ("public record search", "cities where borrower", "city where borrower"),
        "Must certify results of public record searches for each city "
        "where the borrower has resided in the last 2 years",
    ),
    (
        "credit_reference_verification",
        ("credit reference",),
        "Must include verification of all credit references provided on "
        "the loan application",
    ),
    (
        "ssn_identification",
        ("social security number", "social security"),
        "Must show a valid Social Security Number for each borrower, "
        "matching the loan application",
    ),
]


def _spec_item_text(item: Any) -> str:
    if isinstance(item, str):
        return item
    return str((item or {}).get("specification", "") or "")


def canonicalize_credit_report_specs(dr: dict) -> None:
    """Rewrite this Credit Report document request's `specifications` list
    in place: collapse any spec(s) matching a known concept's keywords
    into ONE fixed canonical wording, and inject the concept even if it
    was omitted entirely this run (Credit Report core requirements don't
    vary by loan). Specs that don't match any known concept are preserved
    unchanged (and kept after the canonical core) so genuinely dynamic/
    loan-specific notes aren't lost."""
    _canonicalize_specs_with_concepts(dr, _CREDIT_REPORT_SPEC_CONCEPTS, force_inject=True)


# ---------------------------------------------------------------------------
# Layer 4b: Spec canonicalization for the remaining doc types NOT covered by
# the guideline-sourced library (Layer 4c, below)
# ---------------------------------------------------------------------------
#
# This originally covered 15 doc types via REWRITE-ONLY hand-picked concept
# keywords inferred from a handful of observed LLM output samples. As of
# the guideline-sourced rebuild (Layer 4c below — see
# _guideline_spec_extraction.py and data/canonical_doc_specs.json), 12 of
# those 15 are now handled by the stronger, guideline-VERIFIED deterministic
# layer instead (appraisal report, flood hazard determination, hazard
# insurance, irs 4506-c authorization, title commitment, verification of
# mortgage, government id, borrower certification as to business purpose,
# rental agreement, rental income calculations worksheet, owner occupancy
# certification, personal bank statements).
#
# Previously held 3 doc types (UCDP SSR, Payoff Statement, Deed of Trust)
# that had no dedicated data/guidelines.md section (they're closing-
# mechanics/execution documents rather than underwriting-policy topics) and
# so ran in REWRITE-ONLY mode (rewrite what the LLM already generated;
# don't force-inject). That meant any concept the LLM's freeform draft
# didn't happen to mention that run silently never appeared, producing real
# run-to-run content drift (confirmed via 5x reruns: UCDP SSR item count/
# wording varied wildly, 2 to 6 completely different items).
#
# Fixed by migrating this same hand-curated concept content into
# data/canonical_doc_specs.json (all items condition=None/universal, tagged
# "note": "hand-curated: no dedicated guidelines.md section exists...") so
# apply_guideline_canonicalization() now wholesale-replaces these 3 types
# too, same as every other covered type. This dict is now empty; kept (with
# apply_other_doc_spec_canonicalization() below) as a no-op fallback for any
# future doc type discovered to need rewrite-only treatment.
# ---------------------------------------------------------------------------

_OTHER_DOC_SPEC_CONCEPTS: dict[str, list[tuple[str, tuple[str, ...], str]]] = {}


def _canonicalize_specs_with_concepts(
    dr: dict,
    concepts: list[tuple[str, tuple[str, ...], str]],
    force_inject: bool,
) -> None:
    """Shared implementation for both Credit Report (force_inject=True)
    and the 15 other doc types (force_inject=False, rewrite-only — see
    module comment above for why these two modes differ).

    Matches each concept against the ORIGINAL spec text independently
    (rather than removing items as each concept is processed) so that a
    single LLM sentence covering TWO concepts at once (e.g. "Must include
    all pages, addenda, and required photographs...", which mentions both
    "all pages" and "photographs") correctly contributes to BOTH concepts'
    canonical lines instead of only the first one to match in iteration
    order — confirmed via testing against real Appraisal Report specs
    where a sequential remove-as-you-go approach silently dropped one of
    two concepts merged into a single LLM sentence."""
    specs = dr.get("specifications")
    if not isinstance(specs, list):
        specs = []

    original = list(specs)
    lowered = [_spec_item_text(s).lower() for s in original]
    consumed: set[int] = set()
    canonical: list[str] = []

    for _key, keywords, canonical_text in concepts:
        matched_idxs = [i for i, low in enumerate(lowered) if any(kw in low for kw in keywords)]
        if matched_idxs:
            canonical.append(canonical_text)
            consumed.update(matched_idxs)
        elif force_inject:
            canonical.append(canonical_text)

    leftover = [s for i, s in enumerate(original) if i not in consumed]
    dr["specifications"] = canonical + leftover


def apply_credit_report_canonicalization(docs: list[dict], canonical_fn=None) -> int:
    """Apply canonicalize_credit_report_specs() to every Credit Report
    document request in `docs` (mutates in place). Returns the number of
    documents touched."""
    def canon(name: str) -> str:
        n = (name or "").strip().lower()
        return canonical_fn(n) if canonical_fn else n

    touched = 0
    for dr in docs:
        if canon(dr.get("document_type") or "") == "credit report":
            canonicalize_credit_report_specs(dr)
            touched += 1
    return touched


# ---------------------------------------------------------------------------
# Layer 4c: Guideline-sourced deterministic specifications
# ---------------------------------------------------------------------------
#
# Everything above (Layers 4/4b) infers canonical specs by reverse-
# engineering a handful of observed LLM output samples — necessarily
# incomplete, and risky to force-inject broadly since we can't always tell
# which parts of an inferred concept are truly universal vs. loan-specific.
#
# This layer instead goes to the actual source of truth: data/guidelines.md
# (the NQMF Underwriting Guidelines). data/canonical_doc_specs.json is
# built OFFLINE by _guideline_spec_extraction.py, which:
#   1. Pulls the verbatim guideline section(s) for each canonical doc type
#      (tools/guideline_reader.py).
#   2. Runs an extraction prompt against that text 10 times (temperature>0,
#      for attention-coverage robustness — the source text itself is fixed,
#      so this catches items an individual pass might have missed) asking
#      for every specific, individually-checkable requirement, each tagged
#      with a machine-readable "condition" (e.g. "occupancy=NOO",
#      "property_units=5-8", "program=DSCR") or null if universal.
#   3. Synthesizes the 10 runs into one deduplicated master list, verified
#      against the source text.
#
# At runtime, _resolve_condition() below evaluates each item's condition
# against this scenario's flags:
#   - condition is null (universal)              -> always include
#   - condition resolves definitively True/False  -> include/exclude
#   - condition can't be resolved from our flags  -> exclude (safe default
#     for "=" branch-specific content we can't verify applies) UNLESS it's
#     a "!=" exception clause, in which case we default to True (assume the
#     rare exception doesn't apply, keep the otherwise-universal item).
#
# Unlike Layers 4/4b, this REPLACES `specifications` wholesale for covered
# doc types (no keyword-matching against the LLM's freeform text at all) —
# since the content is now guideline-verified rather than sample-inferred,
# there's no need to preserve/merge whatever the LLM happened to generate
# this run. This is what gives these 14 doc types true 100% wording+content
# consistency across reruns of the same loan, and correct (not just
# consistent) branching across different loan types.
# ---------------------------------------------------------------------------

import json as _json
import os as _os
import re as _re

_GUIDELINE_SPECS_PATH = _os.path.join(
    _os.path.dirname(_os.path.dirname(__file__)), "data", "canonical_doc_specs.json"
)
_guideline_specs_cache: dict | None = None


def _load_guideline_specs() -> dict:
    global _guideline_specs_cache
    if _guideline_specs_cache is None:
        try:
            with open(_GUIDELINE_SPECS_PATH, "r", encoding="utf-8") as f:
                _guideline_specs_cache = _json.load(f)
        except (OSError, _json.JSONDecodeError):
            _guideline_specs_cache = {}
    return _guideline_specs_cache


# Condition keys resolvable from our scenario flags. Values are matched by
# substring against the (lowercased, space-normalized) raw condition value,
# since the guideline extraction's value spellings vary
# ("DSCR"/"dscr"/"non-DSCR"/"non_DSCR", "1-4"/"2-4", etc.).
def _resolve_atomic(key: str, raw_value: str) -> Any:
    """Return a callable(flags) -> Optional[bool] resolver for this key, or
    None if the key isn't one we can structurally resolve."""
    key = key.strip().lower()
    val = raw_value.strip().lower()

    if key == "program":
        if "non" in val and "dscr" in val:
            return lambda flags: not flags["is_dscr"]
        if "dscr" in val:
            return lambda flags: flags["is_dscr"]
        # P&L program sub-variants (added for the profit-and-loss guideline
        # coverage extension) — see income_doc_subtype in derive_flags.
        if "p&l" in val and "bank statement" in val:
            return lambda flags: flags.get("income_doc_subtype") == "p_and_l_bank_stmt_combo"
        if "p&l only" in val or "s/e full doc" in val:
            return lambda flags: (
                flags.get("income_doc_type") == "p_and_l"
                and flags.get("income_doc_subtype") != "p_and_l_bank_stmt_combo"
            ) or (flags.get("income_doc_type") == "full_doc" and flags.get("is_self_employed_borrower"))
        if "full documentation" in val or "full doc" in val:
            return lambda flags: flags.get("has_full_doc_income", False)
        return None

    if key == "borrower_type":
        if "foreign" in val:
            return lambda flags: flags.get("is_foreign_national")
        if "itin" in val:
            return lambda flags: flags.get("is_itin")
        if "self" in val:
            return lambda flags: flags.get("is_self_employed_borrower")
        return None

    if key == "income_type":
        if "self_employ" in val or "self-employ" in val:
            return lambda flags: flags.get("is_self_employed_borrower")
        return None

    # Drives "operating or partnership agreement"'s LLC-specific items
    # (see derive_flags' is_llc comment for the gap this closes).
    # "Partnership" has no distinct signal available yet, so it falls
    # through to the "unresolvable -> exclude" default below rather than
    # being guessed from the absence of an LLC flag.
    if key == "entity_type":
        if "llc" in val:
            return lambda flags: flags.get("is_llc", False)
        return None

    if key in ("occupancy", "occupancy_status"):
        if "primary" in val:
            return lambda flags: flags["is_owner_occupied"]
        if "invest" in val or "tenant" in val:
            return lambda flags: flags["is_noo"]
        return None

    if key == "loan_purpose":
        wants_purchase = "purchase" in val
        wants_refi = "refinance" in val or "refi" in val or "cash_out" in val or "cash-out" in val
        if wants_purchase and wants_refi:
            return lambda flags: flags["is_purchase"] or flags["is_refinance"]
        if wants_purchase:
            return lambda flags: flags["is_purchase"]
        if wants_refi:
            return lambda flags: flags["is_refinance"]
        return None

    if key == "loan_type":
        if "business_purpose" in val:
            return lambda flags: flags["is_noo"]
        return None

    if key in ("documentation_type", "doc_type"):
        if "full_doc" in val or "full_documentation" in val or "tax_returns_provided" in val:
            return lambda flags: flags.get("income_doc_type") == "full_doc"
        if "bank_statement" in val:
            return lambda flags: flags.get("income_doc_type") == "bank_statement"
        if "no_tax_returns" in val:
            return lambda flags: flags.get("income_doc_type") not in ("full_doc", "unknown")
        if val == "dscr" or "dscr" in val:
            return lambda flags: flags.get("income_doc_type") == "dscr"
        return None

    if key == "property_units":
        if "5-8" in raw_value or "5_8" in val:
            return lambda flags: flags["is_5_8_unit"]
        if "1-4" in raw_value or "2-4" in raw_value:
            return lambda flags: not flags["is_5_8_unit"]
        return None

    if key == "property_type":
        if "condo" in val:
            return lambda flags: flags["is_condo"]
        return None

    # Conditions gating on the DOCUMENT'S OWN primary use case (i.e. the
    # branch is essentially always true whenever this document type is
    # being requested at all, since the reason it's requested already
    # implies the condition) — default to True rather than excluding, since
    # excluding would drop near-universal content and the downstream
    # satisfaction pass will naturally no-op on any genuinely inapplicable
    # item.
    if key in ("flood_insurance_required",):
        return lambda flags: True
    if key == "tradeline_requirement":
        if "standard" in val:
            return lambda flags: True
        if "limited" in val:
            return lambda flags: False
        return None

    # Gates a spec on whether the borrower's loan application (URLA/1003)
    # was itself submitted in this manifest -- see _has_loan_application's
    # docstring for why this exists (Credit Report's "credit references on
    # loan application" spec has nothing to check against without it).
    if key in ("loan_application_submitted", "reference_document"):
        return lambda flags: flags.get("has_loan_application", False)

    return None


def _resolve_single_clause(clause: str) -> Any:
    """Parse one atomic 'key OP value' clause. Returns a callable
    (flags) -> Optional[bool], or a constant-None-returning callable if
    unresolvable."""
    m = _re.match(r"^\s*([A-Za-z_][A-Za-z0-9_ ]*?)\s*(!=|=)\s*(.+?)\s*$", clause)
    if not m:
        return lambda flags: None
    key, op, value = m.group(1), m.group(2), m.group(3)
    resolver = _resolve_atomic(key, value)
    if resolver is None:
        # Unresolvable key: for "!=" (exception) clauses, default to True
        # (assume the rare exception doesn't apply); for "=" (branch-
        # specific) clauses, default to None (can't verify, exclude).
        return (lambda flags: True) if op == "!=" else (lambda flags: None)

    if op == "!=":
        return lambda flags: (None if resolver(flags) is None else not resolver(flags))
    return resolver


def _split_top_level(s: str, sep: str) -> list[str]:
    return [p.strip() for p in s.split(sep) if p.strip()]


def _resolve_condition(condition: str | None, flags: dict) -> bool:
    """Return True if this item should be included for the given scenario
    flags, False otherwise. condition=None (universal) always resolves
    True. Unresolvable conditions resolve False (excluded) — see
    _resolve_single_clause for the "!=" exception-clause default-True
    carve-out."""
    if not condition:
        return True
    condition = condition.strip()
    # Numeric-threshold / file-state-dependent operators aren't loan-
    # structure facts we can safely resolve here (they depend on values
    # like an actual DSCR ratio, months of housing history, appraisal age,
    # etc. that live in extracted document data, not scenario flags) —
    # treat any clause set containing one as unresolvable as a whole.
    if any(op in condition for op in (">=", "<=", "<", ">")):
        return False

    and_clauses = _split_top_level(condition, " AND ")
    results: list[Any] = []
    for clause in and_clauses:
        inner = clause[1:-1] if (clause.startswith("(") and clause.endswith(")")) else clause
        # Handle OR-groups whether or not they're wrapped in parens — the
        # newer (w2/paystub/VOE/etc.) extraction batch emits bare
        # "a=1 OR b=2" without parens, unlike the original 14-doc-type batch.
        if " OR " in inner:
            or_clauses = _split_top_level(inner, " OR ")
            sub = [_resolve_single_clause(c)(flags) for c in or_clauses]
            if any(r is True for r in sub):
                results.append(True)
            elif all(r is False for r in sub):
                results.append(False)
            else:
                results.append(None)
        else:
            results.append(_resolve_single_clause(clause)(flags))

    if any(r is None for r in results):
        return False
    return all(results)


def guideline_canonical_specs(doc_type: str, flags: dict) -> list[str] | None:
    """Return the deterministic, guideline-sourced specifications list for
    this canonical doc_type given the current scenario's flags, or None if
    this doc type isn't covered by the guideline-sourced library (caller
    should leave `specifications` untouched in that case)."""
    lib = _load_guideline_specs()
    entry = lib.get(doc_type)
    if not entry:
        return None
    out: list[str] = []
    for item in entry.get("items", []):
        text = item.get("text")
        if not text:
            continue
        if _resolve_condition(item.get("condition"), flags):
            out.append(text)
    return out


def guideline_cross_reference_map(doc_type: str, flags: dict) -> dict[str, list[str]]:
    """Return {spec_text: [other canonical doc_types]} for every spec on
    this canonical doc_type (resolved against the current scenario's flags,
    same gating as guideline_canonical_specs) that carries a
    `cross_references` flag in data/canonical_doc_specs.json — i.e. specs
    that can only genuinely be verified by comparing THIS document's data
    against another document type's data (e.g. Balance Sheet's "must be
    consistent with the YTD Profit and Loss period" needs the actual P&L
    document's dates, not just the Balance Sheet's own extracted fields).

    Consumed by run_satisfaction_pass (tools/merger_tools.py) to (a) pull in
    the referenced sibling document(s)' extracted_fields as extra evidence
    before running the satisfaction check, and (b) populate each document
    request's `cross_document_checks` output field with a per-spec verdict
    (consistent / inconsistent / missing_sibling_document / needs_review)
    rather than silently leaving the LLM to guess with no actual access to
    the other document's data.

    Returns {} if this doc type isn't covered by the guideline library or
    has no flagged specs — always safe to call unconditionally."""
    lib = _load_guideline_specs()
    entry = lib.get(doc_type)
    if not entry:
        return {}
    out: dict[str, list[str]] = {}
    for item in entry.get("items", []):
        text = item.get("text")
        refs = item.get("cross_references")
        if not text or not refs:
            continue
        if _resolve_condition(item.get("condition"), flags):
            out[text] = list(refs)
    return out


def apply_guideline_canonicalization(
    docs: list[dict], scenario_summary: dict, canonical_fn=None,
) -> int:
    """Replace `specifications` on every document request whose canonical
    doc_type is covered by data/canonical_doc_specs.json with the
    deterministic, guideline-sourced list resolved against this scenario's
    flags (mutates in place). Returns the number of documents touched."""
    flags = derive_flags(scenario_summary)

    def canon(name: str) -> str:
        n = (name or "").strip().lower()
        return canonical_fn(n) if canonical_fn else n

    touched = 0
    for dr in docs:
        ct = canon(dr.get("document_type") or "")
        specs = guideline_canonical_specs(ct, flags)
        if specs is None:
            continue
        dr["specifications"] = specs
        # Internal-only field (never reaches final output — see
        # tools/shared/normalize.py's _CANONICAL_FIELDS projection, which
        # intentionally doesn't preserve underscore-prefixed keys) carrying
        # {spec_text: [other doc_types]} for any spec that needs
        # cross-document verification. Consumed by run_satisfaction_pass.
        cross_ref_map = guideline_cross_reference_map(ct, flags)
        if cross_ref_map:
            dr["_cross_reference_map"] = cross_ref_map
        touched += 1
    return touched


def apply_other_doc_spec_canonicalization(docs: list[dict], canonical_fn=None) -> int:
    """Apply the rewrite-only spec canonicalization (_OTHER_DOC_SPEC_CONCEPTS)
    to every document request in `docs` whose canonical document_type has an
    entry there (mutates in place). Returns the number of documents touched.
    _OTHER_DOC_SPEC_CONCEPTS is currently empty -- the last 3 types that used
    to live there (UCDP SSR, Payoff Statement, Deed of Trust) were migrated
    to the wholesale-replace guideline-sourced path (see module comment
    above and apply_guideline_canonicalization()). Kept as a no-op fallback
    for any future doc type needing rewrite-only treatment."""
    def canon(name: str) -> str:
        n = (name or "").strip().lower()
        return canonical_fn(n) if canonical_fn else n

    touched = 0
    for dr in docs:
        ct = canon(dr.get("document_type") or "")
        concepts = _OTHER_DOC_SPEC_CONCEPTS.get(ct)
        if concepts is None:
            continue
        _canonicalize_specs_with_concepts(dr, concepts, force_inject=False)
        touched += 1
    return touched
