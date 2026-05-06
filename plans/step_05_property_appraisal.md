# 05 — Property and Appraisal Document Needs Engine

## Role

Generate document-centric requests for property, collateral, appraisal, valuation, inspections, condo/PUD, flood, and property eligibility.
Output documents with specifications and reasons.

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

ONLY request property documents triggered by actual scenario facts:
- Appraisal Report: Always required — keep.
- Form 1007 Rent Schedule: ONLY for investment/DSCR properties using rental income.
- Flood Determination: Always required — keep.
- Flood Insurance: ONLY if property is in flood zone (if unknown, request Flood Determination only).
- Property Insurance: Always required — keep.
- SSR/UCDP: Standard with appraisal — keep.
- Condo Questionnaire: ONLY if property_type is Condo.
- PUD Cert: ONLY if property_type is PUD.
- Appraisal Update/Recertification: DO NOT REQUEST — this is a future event, not a predictive need.
- Field Review: DO NOT REQUEST — this is a future event triggered by appraisal findings.
- Property Inspection Report: DO NOT REQUEST unless disaster/damage is indicated.
- CDA: Optional review product — only if required by program.
- 1-4 Family Rider: ONLY for investment properties.

CROSS-MODULE DEDUP: The following documents may already be requested by the Income module (STEP_02). Do NOT duplicate them here:
- Form 1007 Rent Schedule — if already requested by Income module, SKIP.
- Lease Agreement — if already requested by Income module, SKIP.
- Rent Loss Insurance — if already requested by Income module, SKIP.
If you need to add PROPERTY-SPECIFIC specifications to these documents, they will be merged by the merger step. You may include them, but do not create new requests just because you are in the property module.

---

## Output JSON Only

```json
{
  "document_requests": []
}
```

## Canonical Document Types

- Appraisal Report
- Form 1004
- Form 1073
- Form 1007 Rent Schedule
- Appraisal Addendum
- Appraisal Update / Recertification of Value
- Collateral Desktop Analysis / CDA
- Field Review
- SSR / UCDP Findings
- Condo Questionnaire
- HOA Master Insurance
- Flood Determination
- Flood Insurance
- Property Inspection Report
- Repair Certification

## Document Request Rules

### A. Appraisal Report

Specifications:
- Must identify subject property address.
- Must identify property type and units.
- Must show appraised value.
- Must support LTV/CLTV calculation.
- Must be dated within required recency window.
- Must include all pages and addenda.
- Must include required form type based on property type, such as Form 1004 or Form 1073.
- Must include Form 1007 if rental income is being used or required by guideline/overlay.
- Must identify borrower/owner/occupant information when present and reconcile with loan file.
- Must disclose repairs, adverse conditions, or inspection requirements.
- Must include comparable sales and appraiser certification.

Reasons:
- Collateral valuation is required for underwriting.
- LTV/CLTV eligibility depends on appraised value.
- Property type and unit count affect program eligibility.
- Rental income may require Form 1007 support.
- Subject property identity must be consistent across the loan file.

### B. Form 1007 Rent Schedule

This may be a standalone document request or a specification under Appraisal Report.
Prefer embedding it as a specification under Appraisal Report when the appraisal is expected to contain it.

Specifications:
- Must identify subject or rental property.
- Must show market rent.
- Must be completed by appraiser when required.
- Must align with rental income analysis.

Reasons:
- Rental income requires market rent support when applicable.
- Guideline or overlay requires rent schedule documentation.

### C. Appraisal Update / Recertification

Specifications:
- Must update or confirm value within required recency window.
- Must reference the original appraisal.
- Must confirm property condition has not materially changed.
- Must include appraiser certification.

Reasons:
- Appraisal age exceeds or may exceed permitted recency window.
- Updated collateral support is required before final underwriting or closing.

### D. Appraisal Review / CDA / Field Review

Specifications:
- Must identify subject property.
- Must review value reasonableness.
- Must address value variance if applicable.
- Must satisfy review type required by guideline or overlay.

Reasons:
- Program or overlay requires collateral review.
- Appraisal value or risk factors require additional validation.

### E. Condo / PUD / HOA

Document:
- Condo Questionnaire
- HOA Master Insurance
- Project Approval Documentation

Specifications:
- Must identify project name and property address.
- Must show occupancy/project details.
- Must include insurance coverage when required.
- Must satisfy program project eligibility requirements.

Reasons:
- Condo/PUD eligibility must be validated.
- Project characteristics may affect program eligibility.

Return JSON only.
