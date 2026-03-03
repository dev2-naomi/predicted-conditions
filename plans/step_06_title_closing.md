# 06 — Title / Closing / Payoffs Conditions Engine (Focused)

## Role
Generate predictive underwriting conditions related to TITLE, ESCROW, PAYOFFS, CLOSING docs, and settlement items.
Use only title/closing docs, overlays, and the relevant NQMF guideline sections.

Do NOT generate:
- property insurance / hazard insurance / flood insurance conditions (those belong to Property STEP_05)
- compliance/OFAC conditions (those belong to Compliance STEP_07)
- entity vesting restriction conditions (those belong to Compliance STEP_07)
- conditions for things that are "not applicable" to this transaction type
  (e.g., do NOT generate "payoff statements not applicable for purchase" — just omit it)
- speculative conditions without evidence

## Inputs
- scenario_summary
- documents_subset: docs_by_facet.title_closing
- overlays_subset: overlays_by_facet.title_closing
- NQMF Guidelines sections: guideline_section_refs.title_closing
  (e.g., "PROPERTY INSURANCE", "TITLE INSURANCE", "TEXAS HOME EQUITY LOANS" if applicable)

## Output JSON ONLY
{ "conditions": [ ... ] }

## Condition Families
- TITLE_COMMITMENT_REQUIRED
- PRELIM_TITLE_REVIEW (liens, vesting, legal description)
- PAYOFF_STATEMENTS_REQUIRED (only for refinance transactions)
- HOA_CONDO_DOCS_REQUIRED
- ESCROW_INSTRUCTIONS_AND_CONTACTS
- CLOSING_DISCLOSURE_REVIEW
- SEASONING_OR_CHAIN_OF_TITLE (if program requires)
- TITLE_INSURANCE_COVERAGE

Deterministic checks:
- If refi -> payoff(s) + mortgage statements + subordinate liens
- If purchase -> do NOT generate payoff conditions
- Ensure vesting/borrower name matches (if mismatch -> CrossCutting or include here but reference CrossCutting dependency)
- If condo/HOA -> request questionnaire, master policy, budget per NQMF "CONDOMINIUMS - GENERAL" and insurance sections
- Per NQMF "TITLE INSURANCE" section: verify title commitment requirements, coverage amount, vesting, gap coverage

Return JSON only.
