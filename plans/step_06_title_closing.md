# 06 — Title, Closing, Escrow, and Payoff Document Needs Engine

## Role

Generate document-centric requests for title, vesting, liens, payoffs, escrow, closing, and settlement review.

---

## Inputs

```json
{
  "scenario_summary": {},
  "documents_subset": [],
  "overlays_subset": [],
  "doctype_masterlist": [],
  "kg_nodes_subset": []
}
```

---

## Relevance Gate

## Relevance Gate — STRICT

Title/Closing generates ONLY documents an underwriter must review before approving the loan.

**Always include:**
- Title Commitment — underwriter must review title exceptions and lien position
- Vesting Deed — confirms property ownership and vesting

**Include if triggered:**
- Payoff Statement — ONLY for refinance transactions
- Mortgage Statement — ONLY for refinance (to verify existing loan)
- HOA Documentation — ONLY if property is in an HOA (condo, PUD, or HOA indicated)
- Survey — ONLY if title exception specifically requires it

**DO NOT REQUEST — these are closing-stage documents, not underwriting inputs:**
- Closing Disclosure — does NOT exist at underwriting time; generated at closing
- Settlement Statement — generated at closing; duplicative with Closing Disclosure
- Escrow Instructions — routine closing workflow item
- Wire Transfer Instructions — routine closing workflow item
- Tax Certificate — routine closing item

**CROSS-MODULE DEDUP:**
- Hazard Insurance Declaration Page — defer to Property module (STEP_05)
- Rent Loss Insurance — defer to Income module (STEP_02) for DSCR loans
- Flood Determination — defer to Property module (STEP_05)

Target for this module: 2-3 documents max for a clean purchase loan.

---

## Output JSON Only

```json
{
  "document_requests": []
}
```

## Canonical Document Types

- Title Commitment
- Preliminary Title Report
- Vesting Deed
- Legal Description
- Payoff Statement
- Mortgage Statement
- Subordinate Lien Documentation
- Escrow Instructions
- Closing Disclosure
- Settlement Statement
- HOA Demand
- Tax Certificate
- Insurance Binder
- Hazard Insurance Declaration Page
- Flood Insurance Declaration Page

## Document Request Rules

### A. Title Commitment / Preliminary Title

Specifications:
- Must identify borrower/owner/vested parties.
- Must identify subject property address and legal description.
- Must disclose liens, judgments, easements, exceptions, and title requirements.
- Must show title insurer and effective date.
- Must be dated within acceptable recency window.
- Must reconcile vesting with borrower and transaction structure.

Reasons:
- Title must confirm ownership, lien position, and insurable interest.
- Vesting and subject property must align with loan file.
- Liens and exceptions may affect closing conditions.

### B. Payoff Statement

Specifications:
- Must identify creditor/servicer.
- Must identify account number.
- Must show payoff amount.
- Must show good-through date.
- Must identify subject property or secured lien when applicable.
- Must include per diem if applicable.

Reasons:
- Existing liens must be paid or subordinated for refinance transactions.
- Payoff amount is required to calculate funds to close and lien position.

### C. Mortgage Statement

Specifications:
- Must identify borrower, servicer, account number, property, payment, and unpaid balance.
- Must be recent.
- Must support payoff, housing history, or REO liability review.

Reasons:
- Existing mortgage obligations must be verified.
- Mortgage statement may support lien, payoff, and payment history review.

### D. Closing Disclosure / Settlement Statement

Specifications:
- Must show final or estimated transaction terms.
- Must identify cash to close.
- Must reconcile loan amount, payoff, fees, credits, and seller/borrower contributions.
- Must align with title and escrow documents.

Reasons:
- Closing figures must reconcile with verified assets and approved loan terms.
- Cash to close must be supported by acceptable funds.

### E. Hazard / Flood Insurance

Specifications:
- Must identify insured property.
- Must identify borrower or acceptable insured party.
- Must show coverage amount, deductible, premium, and effective dates.
- Must show mortgagee clause if required.
- Must include flood insurance if flood determination requires it.

Reasons:
- Property insurance must satisfy collateral protection requirements.
- Flood coverage is required when property is in a flood zone.

Return JSON only.
