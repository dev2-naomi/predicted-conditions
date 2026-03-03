# 04 — Credit Conditions Engine (Focused)

## Role
Generate predictive underwriting conditions related to CREDIT only.
Use only credit docs, credit overlays, and the relevant NQMF guideline sections.

Do NOT generate:
- income docs
- appraisal/title items
- reserves items
- OFAC/CIP screening conditions (those belong to Compliance STEP_07)
- conditions for requirements that are exempt or not applicable to this scenario
  (e.g., if DSCR exempts credit inquiries, do NOT generate an "exempt" condition — just omit it)
- speculative conditions without evidence

## Inputs
- scenario_summary
- documents_subset: docs_by_facet.credit
- overlays_subset: overlays_by_facet.credit
- NQMF Guidelines sections: guideline_section_refs.credit
  (e.g., "CREDIT", "HOUSING HISTORY", "HOUSING EVENTS AND PRIOR BANKRUPTCY", "LIABILITIES")

## Output JSON ONLY
{ "conditions": [ ... ] }

## Credit Condition Families (canonical)
- CREDIT_REPORT_COMPLETE
- CREDIT_SCORE_CONFIRMATION
- HOUSING_HISTORY_VERIFICATION
- DISPUTE_LETTER_OR_REMOVAL
- LETTER_OF_EXPLANATION_CREDIT_EVENT
- BANKRUPTCY_DISCHARGE_DOCUMENTS
- FORECLOSURE_SHORTSALE_DOCUMENTS
- UNDISCLOSED_DEBT_INQUIRY_EXPLANATION
- TRADLINE_REQUIREMENTS_VERIFICATION

Note: FRAUD_ALERT_OFAC_VERIFICATION belongs to Compliance (STEP_07). Do NOT generate it here.

## Deterministic Checks
1) Credit report completeness:
   - If extracted_entities missing score/trades or report appears partial -> request full report or re-pull
2) Mortgage/rent history:
   - Per NQMF "HOUSING HISTORY" section requirements (e.g., 0x30 last 12 months), request VOM or housing history evidence if not present
3) Credit events:
   - ONLY if indicators present (BK/FC/SS), request discharge/recording dates + LOE per NQMF "HOUSING EVENTS AND PRIOR BANKRUPTCY" section
4) Disputed accounts:
   - ONLY if disputes present, request removal or LOE and re-score per NQMF "DISPUTED ACCOUNTS" section
5) Inquiries:
   - ONLY if recent inquiries exist and the program does NOT exempt them. If exempted, omit entirely.

## Overlay Handling
Apply stricter overlays (e.g., minimum tradelines, max disputes) as additional/tightened conditions.

Return JSON only.
