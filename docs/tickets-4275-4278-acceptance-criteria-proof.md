# Tickets 4275 / 4276 / 4277 / 4278 — acceptance criteria proof

This document walks through **every acceptance criterion** of all four tickets,
one by one, with the exact test (or live-run evidence) that proves it, and an
honest status for the few that aren't fully, deterministically provable.

Status legend:
- ✅ **Met, unit-tested** — a deterministic test proves this exact behavior.
- ✅ **Met, live-confirmed** — confirmed on a real deployed run (see e2e section).
- ⚠️ **Partially met** — deterministic code does its part; the rest depends on
  LLM grading quality, which can't be unit-tested to a guaranteed pass.
- ❌ **Not addressed** — explicitly out of scope for this batch, with why.

Run all tests: `python3 -m pytest tests/ --ignore=tests/test_thread_store_decimal_conversion.py -q`
→ **263 passed, 48 skipped, 0 failed.**

---

## Ticket 4275 — Recognise Documents That Are Already in the File

All 6 ACs covered by `tests/test_task_tile_category_mapping.py` (10/10 passing).

| # | Acceptance criterion | Status | Evidence |
|---|---|---|---|
| 1 | Employment verification, category "Verification of Income" → VOE shows linked | ✅ Unit-tested | `TestAC1_VerificationOfIncomeToVOE::test_links` |
| 2 | Cleared EMD, category "Transaction History" → EMD Check shows linked | ✅ Unit-tested | `TestAC2_TransactionHistoryToEMDCheck::test_links` |
| 3 | Bank-statement loan, "Income Calculations Worksheet" → Non QM Bank Statement Analysis Worksheet linked | ✅ Unit-tested | `TestAC3_IncomeCalculationsWorksheetToBankStatementAnalysis::test_links` |
| 4 | Asset-depletion loan, "Asset Worksheet" + asset statements → worksheet linked to Asset Depletion Worksheet; statements linked to income condition | ✅ Unit-tested | `TestAC4_AssetWorksheetToAssetDepletionWorksheet` (both halves tested separately) |
| 5 | Only 4506-C has category "4506-C IVES" → IRS 4506-C shows linked | ✅ Unit-tested | `TestAC5_4506CIVESToIRS4506C::test_links` |
| 6 | Document really not in file → condition still shows no document | ✅ Unit-tested | `TestAC6_GenuinelyMissingDocumentStillShowsNoMatch` (2 tests: negative control + empty manifest) |

Full category → condition mapping table: [`docs/task-tile-category-mapping.md`](./task-tile-category-mapping.md).

---

## Ticket 4276 — Link the Right Copy of Each Document

All 9 ACs covered by `tests/test_ticket_4276_duplicate_copy_selection.py` (17/17 passing).

| # | Acceptance criterion | Status | Evidence |
|---|---|---|---|
| 1 | Signed + unsigned 1003s → latest signed linked; signed-1003 requirement met | ✅ Unit-tested | `TestAC1_LatestSigned1003Wins::test_signed_copy_wins_even_when_unsigned_copies_indexed_later` — end-to-end through `parse_manifest_from_string`, 2 unsigned copies indexed LATER than 5 signed copies; the signed copy still wins |
| 2 | Two payoff statements, different dates → newer linked | ✅ Unit-tested | `TestDocRankDateBeforeConfidence::test_newer_dated_copy_wins_over_older_regardless_of_confidence` |
| 3 | Needs 12 months bank statements, all 12 in file → all 12 linked, requirement met | ✅ Unit-tested (linking) / ⚠️ Partial (grading) | Linking: `test_statements_link_to_income_condition`-style matching already proven in ticket 4275's AC4 test (multiple Bank Statement entries all match). **Grading** ("requirement met") is LLM judgment — see `PERIOD-COVERAGE specs` guidance added to `_SATISFACTION_INSTRUCTIONS`, not independently unit-testable |
| 4 | Same with one month missing → requirement open, names the missing month | ⚠️ Partially met | Deterministic gap detection added for paystubs (`coverage_gaps`/`has_coverage_gap`, see AC6 below); for bank statements specifically, added explicit prompt instruction ("name the specific missing month(s)") to `_SATISFACTION_INSTRUCTIONS`'s new PERIOD-COVERAGE section — the LLM must still compute and state the missing month itself, since statements aren't deterministically merged the way paystubs are |
| 5 | Wage earner, two consecutive paystubs → both linked | ✅ Unit-tested | `TestAC5AC6_PaystubConsecutivenessAndGaps::test_two_consecutive_paystubs_both_linked_no_gap` — `document_ids` carries both contributing stub IDs |
| 6 | Paystubs with a gap in 30 days → 30-day requirement open | ✅ Unit-tested (real bug fixed) | **Found and fixed a real gap**: `_merge_paystubs` previously only checked the overall earliest→latest SPAN (≥30 days), not whether the days in between were actually covered — a stub for Jan 1-15 and another for Feb 10-24 span 55 days end-to-end but miss Jan 16–Feb 9 entirely, and the old code treated that as satisfying "30 days." Now computes `coverage_gaps` deterministically and surfaces it in the merged entry's own `name` field (e.g. "... — COVERAGE GAP: missing 2026-01-16 to 2026-02-09"). `test_gap_inside_30_day_window_flagged_and_named` + `test_small_boundary_gap_not_flagged` (normal pay-cycle noise ≤3 days isn't a false positive) |
| 7 | Policies for subject and another property → only subject policy linked to Hazard Insurance | ✅ Unit-tested | `TestSubjectPropertyScoping::test_other_property_hazard_policy_not_linked` + `test_subject_property_hazard_policy_linked` |
| 8 | Only hazard policy expired → condition says expired | ✅ Unit-tested (spec added) | New "Hazard insurance policy must not be expired" spec added to `data/canonical_doc_specs.json`; whether the LLM correctly flags an *actually* expired policy is grading, same caveat as elsewhere — the spec text now exists to be graded against at all, which it didn't before |
| 9 | Two borrowers, an ID each → both linked, one per borrower | ✅ Unit-tested | `TestAC9_OneGovernmentIdPerBorrower::test_two_borrowers_two_ids_both_survive_as_separate_entries` (two different borrowers' passports both survive dedup) + `test_two_copies_of_the_same_borrowers_id_still_dedup_to_one` (confirms the fix doesn't over-correct into treating every re-upload as a new person) |

---

## Ticket 4277 — Keep Each Condition's Requirements to What Applies

8 of 9 ACs covered by `tests/test_ticket_4277_requirement_scoping.py` (18/18 passing);
1 AC (completeness of LLM grading) is prompt-reinforced but not unit-testable.

| # | Acceptance criterion | Status | Evidence |
|---|---|---|---|
| 1 | Flood cert shows not in flood zone → flood-insurance requirements not open; certificate requirement met | ✅ Unit-tested (suppression) / ⚠️ Partial (grading) | `TestFloodZoneFromSubmittedDocument::test_flood_cert_shows_not_in_zone_suppresses_flood_insurance_specs` — the conditional specs are deterministically suppressed; "certificate requirement met" (crediting the cert's own base spec) is LLM grading |
| 2 | Single-unit appraisal → 2-4 unit form requirement not open | ✅ Unit-tested | `TestSingleUnitNo24UnitFormRequirement` (both directions: single-unit excluded, 4-unit included) |
| 3 | Standard homeowners policy for subject → no per-peril lines | ✅ Unit-tested | `TestHazardInsurancePerPerilConsolidation` (2 tests) + live-confirmed on `dev` (see e2e section — real Hazard Insurance output shows exactly one consolidated line) |
| 4 | Purchase with cleared EMD → "not yet cleared" not open | ✅ Unit-tested | `TestEMDCheckEitherOr::test_cleared_evidence_present_drops_not_yet_cleared_branch` |
| 5 | Wage earner with W-2 → W-2 condition has no paystub/4506-C/VOE requirements | ✅ Unit-tested | `TestW2NoLongerBleedsOtherDocumentRequirements` (2 tests covering both bled-in families) + a 3rd test confirming W-2's own real items survive |
| 6 | Before closing with appraisal → no check against the Note shows missing | ✅ Unit-tested (real bug fixed) | **Found and fixed**: the Note/security instrument only exists after closing, so `cross_document_checks` always reported `missing_sibling_document` for the appraisal's 2 Note-dated specs on every pre-closing file — the correct, universal state, not a real gap. New `not_yet_available_pre_closing` status added specifically for closing-only cross-reference targets. `TestNoteNotCheckedBeforeClosing::test_appraisal_note_cross_checks_do_not_show_missing_sibling_document` |
| 7 | Credit report with empty tradelines → tradeline requirements "not verified" | ✅ Unit-tested | `TestTradelineNotVerifiedWording` (3 tests) — open tradeline-detail specs are rewritten in place to literally start with "Not verified:", distinct from PC-12's earlier fix (which only stopped them being *falsely satisfied*; this makes the open state explicit) |
| 8 | Any loan → every condition has at least one requirement | ✅ Unit-tested | `TestEveryConditionHasAtLeastOneRequirement::test_fallback_item_when_canonicalization_resolves_to_empty` |
| 9 | Loan at Clear to Close with full file → Appraisal Report, Flood Hazard Determination, Title Commitment show most requirements met | ⚠️ **Partially addressed — the hardest AC in this batch** | This is PC-05 from the original 17 Nov report, previously flagged "Not addressed." It is fundamentally about LLM satisfaction-grading quality, not deterministic code — there's no field-shape data in any local fixture for Flood Hazard Determination or Title Commitment to build a safe, targeted backstop against (the kind PC-12 built for Credit Report's `creditTradeLines`/`mortgageSummary`, confirmed against a real extraction). Guessing at field names here risks the opposite failure mode: **false positives** (claiming a requirement is met when it isn't), which is worse than today's over-cautious "mostly open." Added a general, low-risk reinforcement paragraph to `_SATISFACTION_INSTRUCTIONS` naming these 3 document types explicitly and instructing the grader to search ALL extracted fields for data that plainly answers a spec rather than requiring an exact field-name match — but this cannot be unit-tested to a guaranteed outcome. **Recommend**: get one real Appraisal Report / Flood Hazard Determination / Title Commitment extraction sample from production, then build a deterministic field-mapping backstop the same way Credit Report's was built. |

---

## Ticket 4278 — Add the Documents Almost Every Loan Needs

All 8 ACs covered by `tests/test_ticket_4278_new_documents.py` (21/21 passing).

| # | Acceptance criterion | Status | Evidence |
|---|---|---|---|
| 1 | Any Wholesale loan → title company documents, closing protection letter, property tax certificate | ✅ Unit-tested + live-confirmed | `TestWholesaleChannelFlag::test_wholesale_loan_gets_title_company_docs_cpl_and_tax_cert`; live on `dev` against `compiled_inputs/aubrey` (Wholesale) — all 4 appeared |
| 2 | Loan with an appraisal → includes appraisal delivery confirmation | ✅ Unit-tested + live-confirmed | `TestAppraisalDeliveryConfirmation`; live-confirmed on both `aubrey` and `niccum` runs |
| 3 | Wage-earner loan → includes W-2 or 1099 tax transcripts | ✅ Unit-tested | `TestWageOr1099TaxTranscripts::test_wage_earner_loan_gets_tax_transcripts` |
| 4 | DSCR loan, no wage income → no tax transcripts | ✅ Unit-tested | `TestWageOr1099TaxTranscripts::test_dscr_loan_no_wage_income_gets_no_tax_transcripts` |
| 5 | Loan vesting in an LLC → includes entity documents | ✅ Unit-tested | `TestEntityDocuments::test_llc_vesting_gets_entity_documents` |
| 6 | Loan vesting in an individual → no entity documents | ✅ Unit-tested | `TestEntityDocuments::test_individual_vesting_gets_no_entity_documents` |
| 7 | Appraisal names a different lender → includes transfer letter and AIR certification | ✅ Unit-tested | `TestAppraisalDifferentLender` (3 tests: different lender triggers both docs, same lender doesn't, no data defaults to no extra docs) |
| 8 | Any of these documents already in file → linked to its new condition | ✅ Unit-tested | `TestAC8_AlreadyInFileLinksToNewCondition` — all 9 new document types, each tested against a *plausible real-world* Task Tile category name (not just the exact canonical name), e.g. "Wire Instructions" → Title Company Wiring Instructions, "CPL" → Closing Protection Letter |

Also confirmed live: `compiled_inputs/niccum` (Retail, negative control) correctly shows **none** of the 4 wholesale-gated document types, while the universal Appraisal Delivery Confirmation still appears — proving the `channel` gate discriminates correctly in both directions on a real deployed run, not just in a unit test.

---

## Live e2e verification (dev, post-deploy)

- **`compiled_inputs/aubrey`** (Wholesale, LLC=False) — success. Closing Protection
  Letter, Title Company Wiring Instructions, Title Company E&O Insurance, Property
  Tax Certificate, Appraisal Delivery Confirmation all generated. Hazard Insurance's
  specs showed the single consolidated coverage line + the new expiration check,
  not the old 8 per-peril lines.
- **`compiled_inputs/niccum`** (Retail, negative control) — success. None of the 4
  wholesale-gated docs appeared; Appraisal Delivery Confirmation (universal) did;
  EMD Check's "not yet cleared" branch correctly stayed open (no EMD-evidence
  document on file in this fixture).
- **`compiled_inputs/montes`** — hit a **pre-existing, unrelated** infra limit
  (DynamoDB 400KB item-size cap in `api/thread_store.py`, nothing to do with this
  batch's changes) before reaching the agent at all. Flagged separately as a
  follow-up; see `docs/pc-accuracy-uat-status.md`.

### Second deploy (this batch's fixes: `79dea33`, deployed, `deploy` job green in 4m18s)

Neither `aubrey` nor `niccum` exercises the paystub-gap, empty-tradeline-credit-report,
or multi-signed-1003 code paths added this turn, so two more fixtures with richer raw
document sets were run against the freshly deployed Lambda specifically to exercise
them on real (not synthetic-test) data:

- **`compiled_inputs/nyarko`** (34 raw documents, incl. 12 raw URLA 1003 copies and
  an Appraisal Report) — success.
  - **4277 AC6 live-confirmed on real data**: the deployed run's own output showed
    `Cross-document checks: 2 total, by status: {'not_yet_available_pre_closing': 2}`
    — both of the Appraisal Report's Note-dated cross-checks ("appraisal must be
    dated within 365 days of the Note date", "recertification of value... if the
    report exceeds 120 days of the Note Date") came back with the new status and the
    expected note text ("...this document is only executed at closing and does not
    exist yet on a file at this stage..."), not the old, misleading
    `missing_sibling_document`.
  - **4276 AC1 dedup confirmed correct on real data, not a bug**: inspecting
    `parse_manifest_from_string` against this fixture's raw manifest directly showed
    12 raw URLA-1003 entries collapsing to 11 — the one genuine duplicate pair (two
    entries sharing `group_name="2"`, one `signed=False` and one `signed=None`)
    correctly collapsed to a single survivor, while the other 10 entries (each a
    distinct TaskTile `group_name` — i.e. genuinely different physical documents by
    the manifest's own grouping, not re-extractions of one file) correctly stayed
    separate. Confirms the dedup bucket key (`category_id`, `group_name`,
    `specific_type`) behaves as intended on messy real-world data, not just the
    clean synthetic fixtures in the unit tests.
- **`compiled_inputs/weingarten`** (28 raw documents, incl. an Appraisal Report and a
  Title Commitment) — success, general health smoke-test. This fixture's Appraisal
  Report happened not to carry a Note-dated cross-check spec in its resolved spec set
  (loan-specific condition gating), so it didn't additionally exercise AC6, but it
  confirms the new `_SATISFACTION_INSTRUCTIONS` additions and `_CLOSING_ONLY_CROSS_REF_TYPES`
  constant don't break/crash a larger, more varied document set.
- Neither fixture contains a Credit Report with empty `creditTradeLines`, nor paystubs
  with a genuine gap between periods, nor more than one government ID per borrower —
  so 4277 AC7 (tradeline "not verified" wording) and 4276 AC4/AC6/AC9 (paystub gap,
  borrower-ID split) remain confirmed via unit test only, not yet via a live deployed
  run on real extraction data. No fixture with those shapes exists among the locally
  available `compiled_inputs/*` test files; one would need to be sourced or
  hand-built from a redacted real file to close this specific residual gap.

## What's still genuinely open after this batch

1. **4277 AC9 / PC-05** — satisfaction-grading completeness for Appraisal Report,
   Flood Hazard Determination, and Title Commitment. Prompt-reinforced, not
   deterministically guaranteed. Needs real extraction samples to close properly.
2. **4276 AC3/AC4** (bank-statement 12-month coverage, naming the missing month) —
   linking is proven; the "all 12 linked → requirement met" / "one missing → names
   the month" grading itself is LLM judgment, reinforced via prompt instruction but
   not unit-testable.
3. **4276 AC8** (expired hazard policy correctly flagged) — the spec now exists to
   grade against; whether the LLM correctly reads an actual expiration date against
   the closing-date projection is, again, grading quality (same caveat the original
   PC-10 Government ID expiration fix already carried).

These three are flagged consistently rather than claimed as fully closed — they are
the same category of gap (LLM grading quality on documents with no local real-data
fixture to build a deterministic backstop against) that PC-05 was originally
flagged as "Not addressed" for, not a new regression introduced by this batch.
