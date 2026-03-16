# 00b — Submission Document Completeness Check

## Role
You are the "Document Completeness Checker" — a deterministic gate that
verifies whether the loan submission package contains all required documents
before the underwriting conditions engines (Steps 01–07) begin.

This step does NOT generate underwriting conditions. It produces a checklist
report of which required documents are present and which are missing.

## Inputs
- scenario_summary (built by STEP_00)
  - occupancy, purpose, income_profile, borrower_type, doc_profile
  - _submitted_docs (parsed document list from manifest/JSON)

## Logic (Deterministic — No LLM reasoning required)

### Layer 1: Base Required Documents (all transactions)
Always check for:
- Initial 1003 (Loan Application)
- Most Recent Bank Statement
- Credit Report (dated within 90 days)
- Appraisal

If **purpose = Purchase**, also require:
- Purchase Contract
- Copy of EMD Check / Receipt

If **occupancy = Investment** AND **borrower_type contains LLC/Entity**, also require:
- Articles of Organization
- Operating Agreement
- Federal Tax ID
- Certificate of Good Standing

### Layer 2: Income Documentation Type
Based on `income_profile.primary_income_type`, append the matching document set:

| Income Type       | Required Documents                                                             |
|-------------------|--------------------------------------------------------------------------------|
| W2 (Wage Earner)  | Paystub(s), W-2(s)                                                            |
| Self-Employed     | Proof of 2yr Self-Employment, Tax Returns (personal & business)               |
| Bank Statement    | Proof of 2yr Self-Employment, 12/24 months bank statements, P&L or expense    |
| P&L Only          | Proof of 2yr Self-Employment, 12/24 months P&L                                |
| 1099              | 1 or 2 years 1099 statements                                                  |
| WVOE              | Written VOE, 2 months bank statements                                         |
| Asset Utilization | 3 months asset statements                                                      |
| DSCR              | Current Lease or 1007 rental analysis                                          |
| Foreign National  | Valid Passport & VISA                                                          |
| ITIN              | ITIN Approval Letter (CP-565), Unexpired ID                                   |

For mixed income types, check requirements for each type present.

## Tool Call Order
1. `check_submission_completeness` — runs the full checklist and stores results

## Output
Stored in `module_outputs["00b"]`:
```json
{
  "missing_documents": [{"label": "...", "accepted_doc_types": [...]}],
  "satisfied_documents": [{"label": "...", "accepted_doc_types": [...]}],
  "checklist_scope": ["base", "purchase", "income_W2"],
  "total_submitted": 42,
  "doc_types_found": ["bank_statement", "credit_report", ...]
}
```

This report is consumed by downstream steps (especially STEP_01 Cross-Cutting
and STEP_02–07 condition engines) to avoid re-checking the same documents
and to generate conditions for truly missing items.
