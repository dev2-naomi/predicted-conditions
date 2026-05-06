# 04 — Credit Document Needs Engine

## Role

Generate document-centric requests for credit review.
You output documents with specifications and reasons, not standalone conditions.

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

## Relevance Gate — STRICT

Before generating ANY credit document request, check scenario_summary fields.
If the trigger condition below is FALSE, you MUST NOT include that document.

| Document | Trigger Condition | If FALSE → |
|----------|------------------|------------|
| Credit Report | Always | Always include |
| Mortgage Payment History | borrower has existing mortgages (liabilities list or owned_properties > 0) | SKIP |
| VOM | existing mortgage AND payments NOT visible on credit report | SKIP |
| VOR | borrower is renter AND housing history NOT on credit report | SKIP |
| Tradeline Supplement | credit report shows insufficient tradelines (< 3 tradelines 12mo or < 2 tradelines 24mo) | SKIP — with FICO 800+ assume tradelines are met |
| Bankruptcy Documentation | credit_events contains "BK" | SKIP |
| Foreclosure Documentation | credit_events contains "FC" | SKIP |
| Short Sale Documentation | credit_events contains "SS" | SKIP |
| Credit Event LOE | credit_events contains any event other than "none" | SKIP |
| Dispute Resolution | active disputes flagged on credit report | SKIP — do not speculatively include |
| Fraud Alert Verification | fraud alert flagged on credit report | SKIP — do not speculatively include |
| Credit Inquiry LOE | NOT for DSCR loans; only when required by program | SKIP |

CRITICAL: If credit_events = ["none"], do NOT request BK/FC/SS/DIL/Credit Event docs.
CRITICAL: Do NOT request Fraud Alert, Dispute Resolution, VOR, Tradeline Supplement, or Primary Residence Verification speculatively.
CRITICAL: A FICO of 700+ with no credit events typically means ONLY Credit Report + Mortgage Payment History (if existing mortgages) are needed. That's 1-2 documents, not 5-7.
CRITICAL: VOM is only needed when mortgage payments are specifically NOT reported on the credit report. With FICO 800, the credit report almost certainly shows mortgage history — do NOT add VOM unless there is a specific gap.
CRITICAL: VOR is only needed when the borrower is a renter AND housing history is NOT on the credit report. If the borrower owns property (investment property = they have housing history), VOR is not needed.
CRITICAL: Primary Residence Verification is NOT a standard credit document. Do NOT include it unless the program specifically requires separate primary residence verification beyond what the credit report shows.

---

## Output JSON Only

```json
{
  "document_requests": []
}
```

## Scope

Generate requests for:
- credit report
- score confirmation
- tradeline verification
- mortgage/rent history
- credit event documents
- bankruptcy/foreclosure/short sale docs
- credit inquiry explanations
- dispute resolution
- fraud alert / OFAC / identity verification support

## Canonical Document Types

- Credit Report
- Mortgage Payment History
- Verification of Mortgage
- Verification of Rent
- Letter of Explanation - Credit Inquiry
- Letter of Explanation - Credit Event
- Bankruptcy Discharge
- Foreclosure Documentation
- Short Sale Documentation
- Dispute Resolution Letter
- Fraud Alert Verification
- Tradeline Supplement

## Document Request Rules

### A. Credit Report

Specifications:
- Must be complete.
- Must identify all borrowers.
- Must show credit scores.
- Must show tradeline details.
- Must show mortgage history if available.
- Must show inquiries, public records, disputes, collections, charge-offs, and alerts.
- Must be dated within allowed recency window.
- Must include all bureaus required by guideline or overlay.

Reasons:
- Credit score is required to validate program eligibility.
- Tradeline and credit history requirements must be reviewed.
- Credit report is needed to identify credit events, inquiries, disputes, and mortgage history.

### B. Mortgage / Rent History

Document:
- Mortgage Payment History
- VOM
- VOR

Specifications:
- Must cover required lookback period.
- Must identify borrower and property/account.
- Must show payment history and late payments.
- Must be from acceptable source.
- Must support required housing history standard.

Reasons:
- Housing history may be required for eligibility.
- Late payments may affect qualification or pricing.
- Mortgage/rent history must satisfy program requirements.

### C. Credit Inquiry LOE

Document:
- Letter of Explanation - Credit Inquiry

Specifications:
- Must identify each inquiry.
- Must state whether new debt was opened.
- Must include supporting documentation for new debt if applicable.

Reasons:
- Recent inquiries may indicate undisclosed debt.
- New liabilities may affect DTI and eligibility.

### D. Credit Events

Document:
- Bankruptcy Discharge
- Foreclosure Documentation
- Short Sale Documentation
- LOE - Credit Event

Specifications:
- Must identify event type.
- Must show discharge, completion, or settlement date.
- Must include court or servicer documentation where applicable.
- Must support seasoning requirements.

Reasons:
- Credit event seasoning must be verified.
- Program eligibility may depend on event type and timing.

### E. Disputes / Fraud Alerts

Document:
- Dispute Resolution Letter
- Fraud Alert Verification

Specifications:
- Must identify disputed tradelines.
- Must document removal/resolution if required.
- Must verify identity if fraud alert exists.

Reasons:
- Disputed accounts may affect credit eligibility or score validity.
- Fraud alerts require identity verification before relying on credit data.

Return JSON only.
