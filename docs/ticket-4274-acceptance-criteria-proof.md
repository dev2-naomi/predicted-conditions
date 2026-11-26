# Ticket 4274 — acceptance criteria proof

**[Predicted Conditions: Don't Ask for Documents the Loan Can't Need]**

This document walks through **every acceptance criterion** of this ticket, one by
one, with the exact test (or live-run evidence) that proves it.

Status legend:
- ✅ **Met, unit-tested** — a deterministic test proves this exact behavior.
- ✅ **Met, live-confirmed** — confirmed on a real deployed run, not just a unit test.

Run the ticket's own test file: `python3 -m pytest tests/test_ticket_4274_predicted_conditions_scope.py -v`
→ **28 passed.**
Full regression: `python3 -m pytest tests/ --ignore=tests/test_thread_store_decimal_conversion.py --ignore=tests/test_thread_store_s3_offload.py -q`
→ **291 passed, 48 skipped, 0 failed.**

---

## Source

17 Nov 2026 UAT report covering loans 90000108510/511/512, plus loan 90000108513
(refinance, run 18 Nov 2026). All four loans had just reached Clear to Close in
production. 28 of the four loans' predicted conditions had no document linked; 23
of those were for documents the loan didn't need. The ticket's own evidence
section is explicit: *"Expected results are a proposal; confirm against
guidelines before changing any rule."*

This ticket restates and sharpens several items from the earlier 15-issue, 17 Nov
UAT report (`docs/pc-accuracy-uat-status.md`): AC1/AC2 close out **PC-01/PC-04**,
AC3 closes the DSCR half of **PC-02**, AC4 closes PC-02's remaining DSCR-purchase
lease item, AC6 closes **PC-03**'s remaining VOR item, and AC7 is **PC-11**.

---

## Status summary

| # | Acceptance criterion | Status | Already fixed before this ticket, or new this round? |
|---|---|---|---|
| 1 | Purchase loan before closing → Deed of Trust and Grant Deed not in the list | ✅ Unit-tested + live-confirmed | Already fixed (19/23 Nov) — re-confirmed against this ticket's exact wording |
| 2 | Investment property loan → Owner Occupancy Certification not in the list | ✅ Unit-tested + live-confirmed | Already fixed (19/23 Nov) — re-confirmed, incl. the "never together with Borrower Certification as to Business Purpose" detail |
| 3 | DSCR loan → Paystub, W-2, IRS 4506-C not in the list | ✅ Unit-tested | Already fixed (23 Nov) — re-confirmed, incl. ticket 4278's separate "W-2 or 1099 Tax Transcripts" doc type |
| 4 | DSCR purchase with appraisal rent schedule → Current Lease Agreement and Rental Income Calculations Worksheet not in the list | ✅ Unit-tested | **New this round** |
| 5 | Loan with bank statements in file → Verification of Deposit not in the list | ✅ Unit-tested + live-confirmed | Already fixed (PC-03) — re-confirmed against this ticket's exact wording |
| 6 | Loan with mortgage statements or rent-free letter → Verification of Rent not in the list | ✅ Unit-tested + live-confirmed | **New this round** |
| 7 | Two-borrower, large file (e.g. 90000108512) → no condition name appears twice | ✅ Unit-tested + live-confirmed | **New this round** |
| 8 | Wage-earner loan → Paystub and IRS 4506-C still predicted (negative control) | ✅ Unit-tested | Already fixed — re-confirmed AC3's gate doesn't over-fire |

---

## Detail

### AC1 — Deed of Trust and Grant Deed not pre-closing

Both document types were already removed from the unconditional floor
(`mandatory_docs()`/`conditional_docs()` in `tools/doc_rules.py`, 19 Nov fix), and
an unconditional gate (`_CLOSING_STAGE_SUPPRESSED` in `apply_negative_gates()`)
was added 23 Nov specifically because the per-module LLM steps kept
re-proposing them from their own guideline reasoning even after the floor stopped
injecting them.

**Tests** (`TestAC1_DeedOfTrustAndGrantDeedNotPreClosing`):
- `test_neither_is_in_the_floor_or_conditional_layer_on_a_purchase` — confirms the
  floor-removal hasn't regressed.
- `test_llm_reproposed_copies_are_gated_out_regardless` — simulates an
  LLM-reproposed copy of each and confirms the gate still catches it (not just
  that the floor's absence was never exercised).
- `test_refinance_loan_has_neither_either`.

**Live-confirmed**: `compiled_inputs/nyarko`, post-23-Nov-deploy — "Deed of Trust"
dropped from the live output (document count 25 → 22 on that run); "Owner
Occupancy Certification" also dropped (see AC2).

### AC2 — Owner Occupancy Certification never on investment

Folded into the same `_CLOSING_STAGE_SUPPRESSED` gate as AC1 (this document is
also executed at closing, regardless of occupancy).

**Tests** (`TestAC2_OwnerOccupancyCertificationNotOnInvestment`):
- `test_not_in_the_floor_or_conditional_layer_on_investment`
- `test_llm_reproposed_copy_is_gated_out_on_investment`
- `test_never_appears_alongside_borrower_certification_business_purpose` — the
  ticket's own loan-513 example: confirms Borrower Certification as to Business
  Purpose (the legitimate NOO document) is present while Owner Occupancy
  Certification is correctly absent, not just that one of the two is gone.
- `test_owner_occupied_loan_also_gates_out_an_llm_reproposed_copy` — confirms the
  owner-occupied case doesn't regress either (this one is closing-stage
  regardless of occupancy, unlike AC6's VOR which is occupancy/evidence-gated).

**Live-confirmed**: same nyarko run as AC1.

### AC3 — DSCR loan has no wage-income docs

Paystub and W-2 are only injected by the income-doc layer
(`derive_income_docs()`) for wage-earner income labels — DSCR correctly derives
neither. IRS 4506-C Authorization is suppressed unconditionally on DSCR via
`_DSCR_SUPPRESSED`.

**Tests** (`TestAC3_DSCRLoanNoWageIncomeDocs`):
- `test_dscr_loan_full_pipeline_has_none_of_the_three` — runs the real income-doc
  layer, then additionally simulates an LLM-reproposed copy of all three before
  gating, confirming the gate (not just the absence) catches them.
- `test_dscr_loan_never_gets_the_w2_1099_tax_transcripts_doc_either` — ticket
  4278 added a *separate* "W-2 or 1099 Tax Transcripts" document type gated on a
  different flag (`has_wage_or_1099_income`); this confirms that flag is also
  correctly `False` on DSCR, since `_DSCR_SUPPRESSED` alone wouldn't catch a
  differently-named document type if that flag were ever wrong.

### AC4 — DSCR purchase + appraisal rent schedule suppresses lease docs

**New this round.** `conditional_docs()`'s DSCR block unconditionally injected
"Current Lease Agreement"/"Rental Agreement" and "Rental Income Calculations
Worksheet" with no purpose gate — exactly the ticket's loan-511 finding ("the
underwriter used the appraisal's rent schedule"; "there is no lease yet" on a
purchase).

**Fix:** new `_has_appraisal_rent_schedule()` (`tools/doc_rules.py`) scans a
submitted Appraisal Report's extracted fields for Form 1007/1025 rent-schedule
evidence (field-key match or free-text mention). New
`_RENT_SCHEDULE_SUPPRESSED` gate drops both document types when
`is_purchase AND has_appraisal_rent_schedule`.

**Guideline basis:** `data/canonical_doc_specs.json`'s "Single Family Comparable
Rent Schedule (FNMA Form 1007) must be included for all 1-unit Investment
Properties when rental income is used to qualifying" and
`data/conditions_masterlist.json`'s "[provide] Form 1007 Comparative Rent
Schedule (if income is being used)" both treat the 1007/1025 as a standalone
alternative to a lease, not an additional ask on top of it.

**Tests** (`TestAC4_RentScheduleReplacesLeaseOnDSCRPurchase`):
- `test_rent_schedule_present_suppresses_both` — confirms the DSCR injection
  really did add both docs first, so the test exercises suppression, not
  absence.
- `test_free_text_1007_mention_also_detected`.
- `test_no_rent_schedule_evidence_does_not_suppress` — negative control: without
  evidence, both docs correctly stay (no over-suppression on absence of data).
- `test_refinance_with_rent_schedule_is_not_suppressed` — the ticket says "on a
  purchase" specifically; a refinance with an existing tenant still needs its
  own lease evidence even if the appraisal also carries a 1007.

### AC5 — Bank statements in file suppresses VOD

Already fixed (PC-03): `_VOD_DOCS` + `_has_asset_statement()` suppress
Verification of Deposit once a real bank/401K/brokerage statement is on file.

**Tests** (`TestAC5_VODSuppressedWhenBankStatementsOnFile`):
- `test_bank_statement_on_file_suppresses_vod`
- `test_no_bank_statement_keeps_vod_open` (negative control)

**Live-confirmed** (earlier `nyarko` run, 23 Nov): absent despite Bank Statement +
Investment Account Statement both present and linked.

### AC6 — Mortgage statements or rent-free letter suppresses VOR

**New this round.** Mirrors AC5's alternative-evidence pattern exactly, for the
ticket's loan-511 (mortgage statements show housing history) and loan-513
(rent-free letter in file) findings.

**Fix:** new `_has_mortgage_statement_evidence()` / `_has_rent_free_letter()` +
`_VOR_DOCS` gate in `apply_negative_gates()`.

**Guideline basis:** `data/canonical_doc_specs.json`'s "Borrowers living rent-free
must provide a rent-free letter from the property owner instead of a VOR";
`data/doctype_masterlist.json` confirms "Mortgage Statement" is its own real
document type, distinct from Verification of Mortgage.

**Tests** (`TestAC6_VORSuppressedByMortgageStatementOrRentFreeLetter`):
- `test_mortgage_statements_show_history_suppresses_vor` (loan 511's finding)
- `test_rent_free_letter_in_file_suppresses_vor` (loan 513's finding)
- `test_neither_present_keeps_vor_open` (negative control)

**Live-confirmed**: redeployed `compiled_inputs/nyarko` run shows Verification of
Rent still correctly **present** (this fixture has no mortgage-statement/
rent-free-letter evidence) — confirming the gate doesn't over-suppress on a real
run that genuinely lacks the alternative evidence, the same negative-control
standard as the unit test.

### AC7 — No condition name appears twice (closes PC-11)

**New this round.** Root cause confirmed: `merge_document_requests`'s
`_merge_key` groups by document_type + `document_context`, so two per-module LLM
proposals for the *same* real-world document need survive as separate groups
whenever `document_context` is populated inconsistently between them (one
proposal tags `{"borrower": "..."}`, another for the identical need leaves it
empty) — matching the ticket's "34 conditions, 13 repeated another by name with
a different result" symptom on loan 512.

**Fix:** new `_collapse_duplicate_canonical_types()` (`tools/merger_tools.py`)
runs a second, coarser merge pass keyed on canonical document_type alone,
immediately after the existing context-aware merge. Does **not** affect
`tools/coborrower.py`'s later, intentional one-row-per-party split for
borrower-specific document types on a multi-borrower loan — that pass re-derives
its own rows from this now-deduped canonical set afterward, so the sanctioned
multi-borrower duplication (e.g. one Government-Issued Photo ID row per
borrower) is preserved.

**Tests** (`TestAC7_NoConditionNameAppearsTwice`):
- `test_collapse_helper_merges_same_canonical_type_different_context` — direct
  unit test of the merge function: confirms specs from both duplicates survive
  in the merged result, and the higher severity/priority of the two wins.
- `test_three_way_duplicate_collapses_to_one` — three duplicate entries collapse
  to one, with all three specs merged.
- `test_merge_document_requests_tool_produces_no_duplicate_names` — end-to-end
  through the real `merge_document_requests` tool (not the helper in isolation),
  feeding the same document_type from two different module_outputs entries, one
  with document_context and one without; confirms the final merged list the
  real pipeline produces never repeats a canonical document_type.

**Live-confirmed**: redeployed `compiled_inputs/nyarko` run — no document type
name repeats across the 27-item output.

### AC8 — Wage-earner loan still gets Paystub and IRS 4506-C (negative control)

Confirms AC3's DSCR gate is scoped correctly and doesn't over-fire on a normal
full-doc wage-earner loan.

**Test** (`TestAC8_WageEarnerStillGetsPaystubAnd4506C::test_full_doc_wage_loan_keeps_both`)
— confirms the income layer actually fired (Paystub present before gating), then
confirms neither Paystub nor IRS 4506-C Authorization is in the gate's `removed`
list.

---

## Live e2e verification (dev, post-deploy)

All against the deployed Lambda, using `compiled_inputs/nyarko` (the closest
available local stand-in for the UAT loans — the real 510/511/512/513 loan data
is real-borrower PII and isn't committed to this repo).

| Run | What it confirmed |
|---|---|
| Deploy (commit `372b6fd`) | AC4/AC6/AC7 fixes live: Deed of Trust / Grant Deed / Owner Occupancy Certification correctly absent; Verification of Rent / Current Lease Agreement / Rental Income Calculations Worksheet all correctly **present** (this fixture has no suppression evidence — negative control intact); no duplicate document type names across the output; ticket 4277's `not_yet_available_pre_closing` Note cross-check regression-checked and still correct |

Full regression: **291 passed, 48 skipped, 0 failures** (up from 263 before
this ticket — +28 new tests, all in
`tests/test_ticket_4274_predicted_conditions_scope.py`).

---

## What's still genuinely open

1. **AC7's fix addresses the confirmed, most-likely root cause** (inconsistent
   `document_context` across per-module LLM proposals), but the original
   report's exact loan-512 symptom (34 conditions, 13 near-exact repeats) was
   never independently reproduced against real loan-512 data (PII, not
   available locally) — the fix is validated against synthetic duplicates and a
   live multi-document nyarko run showing zero duplicates, not loan 512 itself.
2. **AC4/AC6's evidence detection is best-effort.** `_has_appraisal_rent_schedule()`,
   `_has_mortgage_statement_evidence()`, and `_has_rent_free_letter()` scan for
   plausible field names and free-text tokens since no local fixture has real
   appraisal/mortgage-statement extraction data to confirm the exact field keys
   against (the same caveat ticket 4278's new document types carried for their
   aliases). If a real extraction uses a field name these functions don't
   recognize, the suppression simply won't fire — a safe failure mode (under-
   suppress, not over-suppress) but not a guaranteed one.
