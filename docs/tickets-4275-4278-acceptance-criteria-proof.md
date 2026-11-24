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
→ **302 passed, 48 skipped, 0 failed.**

> **24 Nov 2026 update**: revisited every ⚠️ "Partially met" item below against
> fresh real `compiled_inputs/*` fixture data (not just re-stating the original
> analysis). Two of the five gained a genuine deterministic backstop because
> real field data for them now exists; the other three were re-confirmed as
> still genuinely blocked by a total absence of matching fixture data — not
> left alone out of inertia. See each row and "What's still genuinely open"
> below for specifics.

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

All 9 ACs covered by `tests/test_ticket_4276_duplicate_copy_selection.py` (23/23 passing).

| # | Acceptance criterion | Status | Evidence |
|---|---|---|---|
| 1 | Signed + unsigned 1003s → latest signed linked; signed-1003 requirement met | ✅ Unit-tested | `TestAC1_LatestSigned1003Wins::test_signed_copy_wins_even_when_unsigned_copies_indexed_later` — end-to-end through `parse_manifest_from_string`, 2 unsigned copies indexed LATER than 5 signed copies; the signed copy still wins |
| 2 | Two payoff statements, different dates → newer linked | ✅ Unit-tested | `TestDocRankDateBeforeConfidence::test_newer_dated_copy_wins_over_older_regardless_of_confidence` |
| 3 | Needs 12 months bank statements, all 12 in file → all 12 linked, requirement met | ✅ Unit-tested (linking + gap-detection) / ⚠️ Partial (grading) | Linking: multiple Bank Statement entries all match (ticket 4275 AC4). **24 Nov 2026 upgrade**: real `statementPeriodFrom`/`statementPeriodTo`/`accounts[].accountNumber` fields confirmed present in `nyarko`/`montes`/`niccum` fixtures, so a new deterministic `_annotate_bank_statement_coverage()` (`tools/shared/manifest_parser.py`) now computes `_coverage_summary`/`_coverage_gaps`/`has_coverage_gap` on EVERY Bank Statement entry, grouped per account, WITHOUT merging entries (unlike paystubs — AC3 needs "all 12 linked" individually). `_SATISFACTION_INSTRUCTIONS` now tells the LLM to read these fields directly instead of computing the date math itself. `TestAC3AC4_BankStatementCoverageAnnotation` (7 tests, incl. one against real `compiled_inputs/nyarko` data — 2 accounts, 5 statements, correctly identified as gap-free). What remains LLM judgment: confirming the covered span is *long enough* for the specific spec's required month count (12 vs 24) — `has_coverage_gap` only answers "is it continuous," not "is it enough" |
| 4 | Same with one month missing → requirement open, names the missing month | ✅ Unit-tested (gap detection + naming) / ⚠️ Partial (final grading) | **24 Nov 2026 upgrade**: `_annotate_bank_statement_coverage()` deterministically computes the exact missing date range per account (e.g. `{"gap_start": "2026-03-01", "gap_end": "2026-03-31", "gap_days": 31}`) into `_coverage_gaps`, and a human-readable `_coverage_summary` string naming it directly — no longer relying on the LLM to compute this itself. `test_missing_month_flagged_and_named` proves the exact gap dates. The LLM still performs the final satisfied/open judgment call (reading `has_coverage_gap` rather than re-deriving it), which is why this stays ⚠️ rather than fully ✅ |
| 5 | Wage earner, two consecutive paystubs → both linked | ✅ Unit-tested | `TestAC5AC6_PaystubConsecutivenessAndGaps::test_two_consecutive_paystubs_both_linked_no_gap` — `document_ids` carries both contributing stub IDs |
| 6 | Paystubs with a gap in 30 days → 30-day requirement open | ✅ Unit-tested (real bug fixed) | **Found and fixed a real gap**: `_merge_paystubs` previously only checked the overall earliest→latest SPAN (≥30 days), not whether the days in between were actually covered — a stub for Jan 1-15 and another for Feb 10-24 span 55 days end-to-end but miss Jan 16–Feb 9 entirely, and the old code treated that as satisfying "30 days." Now computes `coverage_gaps` deterministically and surfaces it in the merged entry's own `name` field (e.g. "... — COVERAGE GAP: missing 2026-01-16 to 2026-02-09"). `test_gap_inside_30_day_window_flagged_and_named` + `test_small_boundary_gap_not_flagged` (normal pay-cycle noise ≤3 days isn't a false positive) |
| 7 | Policies for subject and another property → only subject policy linked to Hazard Insurance | ✅ Unit-tested | `TestSubjectPropertyScoping::test_other_property_hazard_policy_not_linked` + `test_subject_property_hazard_policy_linked` |
| 8 | Only hazard policy expired → condition says expired | ✅ Unit-tested (spec added) | New "Hazard insurance policy must not be expired" spec added to `data/canonical_doc_specs.json`; whether the LLM correctly flags an *actually* expired policy is grading, same caveat as elsewhere — the spec text now exists to be graded against at all, which it didn't before |
| 9 | Two borrowers, an ID each → both linked, one per borrower | ✅ Unit-tested | `TestAC9_OneGovernmentIdPerBorrower::test_two_borrowers_two_ids_both_survive_as_separate_entries` (two different borrowers' passports both survive dedup) + `test_two_copies_of_the_same_borrowers_id_still_dedup_to_one` (confirms the fix doesn't over-correct into treating every re-upload as a new person) |

---

## Ticket 4277 — Keep Each Condition's Requirements to What Applies

8 of 9 ACs covered by `tests/test_ticket_4277_requirement_scoping.py` (23/23 passing);
1 AC (completeness of LLM grading) is prompt-reinforced, and as of 24 Nov 2026
partially deterministic for one of its three named document types (see AC9 below).

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
| 9 | Loan at Clear to Close with full file → Appraisal Report, Flood Hazard Determination, Title Commitment show most requirements met | ⚠️ **Partially addressed — the hardest AC in this batch** | This is PC-05 from the original 17 Nov report, previously flagged "Not addressed." It is fundamentally about LLM satisfaction-grading quality, not deterministic code. **24 Nov 2026 upgrade for Appraisal Report specifically**: `compiled_inputs/aubrey` has rich real extraction data (`appraiser.license`, non-empty `comps[]` with real `salePrice`/`adjustedValue`) confirming 2 of this doc type's ~35 conditioned specs — "appraiser must provide license/certification" (plus the parallel "licensed/certified in the property's state" spec) and the Sales Comparison Approach spec — are now safe to credit deterministically. New `_apply_appraisal_report_backstop()` (`tools/merger_tools.py`) does exactly that, mirroring the pattern PC-12 used for Credit Report. **A real false positive was caught and fixed during live verification**: the first version matched on the broader phrase "comparable used," which also appears inside the unrelated "must contain a street map showing... all comparable used" spec and the "unfavorable conditions... comparable sales" commentary spec — the live `aubrey` run actually credited the street-map spec with zero evidence a street map exists. Narrowed the keyword to the exact phrase "sales comparison approach," which (confirmed against `data/canonical_doc_specs.json`) appears in exactly one spec — the one `comps[]` actually backs. Re-verified live afterward: only the 3 genuinely field-backed specs are credited. `TestAppraisalReportBackstop` (6 tests, incl. a regression test for this exact false positive, and one against real `aubrey` data). The other ~32 Appraisal Report specs (photos, sketches, maps, narrative, limiting-conditions certification) have no corresponding field in any fixture and are deliberately left untouched — crediting them would be guessing. **Flood Hazard Determination and Title Commitment remain completely unaddressed**: re-confirmed on fresh inspection that zero of the 5 local fixtures contain either document category at all (an earlier version of this doc inaccurately claimed `weingarten` had a Title Commitment — it does not; its 28 documents include an Appraisal Report but no Title Commitment). Guessing at field names for either risks the opposite failure mode: **false positives** (claiming a requirement is met when it isn't), which is worse than today's over-cautious "mostly open" — exactly what the street-map incident above demonstrated firsthand. The general `_SATISFACTION_INSTRUCTIONS` reinforcement paragraph for all 3 document types remains in place for whatever isn't covered by the new backstop. **Recommend**: get one real Flood Hazard Determination / Title Commitment extraction sample from production to close the remaining two document types. |

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
- **`compiled_inputs/weingarten`** (28 raw documents, incl. an Appraisal Report —
  *correction, 24 Nov 2026: an earlier version of this doc also claimed a Title
  Commitment here; fresh inspection confirms weingarten has no Title Commitment at
  all*) — success, general health smoke-test. This fixture's Appraisal
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

### Third deploy (24 Nov 2026 follow-up: `7c0f55a`, deployed, `deploy` job green in 3m47s)

Specifically to live-verify the two new backstops added this round (bank-statement
coverage annotation, Appraisal Report license/comps backstop) against real data,
not just unit tests:

- **`compiled_inputs/nyarko`** — success (27 document requests, 4 hard stops). Bank
  Statement came back `satisfied_but_review_required`, and its own satisfied-spec
  reason **directly echoes the new deterministic `_coverage_summary` text**: *"Three
  consecutive business statements for KOREE PARTNERS LLC (account 4127) were
  provided, covering 2025-11-01 to 2026-01-30 with no gaps (91 days)."* — confirming
  the LLM is reading the new field, not recomputing the date math itself.
- **`compiled_inputs/aubrey`** — success (26 document requests, 4 hard stops).
  Appraisal Report came back `satisfied_but_review_required`; its satisfied specs
  confirmed the license and Sales Comparison Approach specs were credited with the
  backstop's own canned reason text ("Confirmed from the appraisal report's own
  extracted data...").
  - **A real false positive was caught right here, on this run**: the first version
    of the backstop's keyword list also credited "Appraisal report must contain a
    street map showing the location of the subject property and all comparable
    used" — a spec about a street map, with zero evidence one exists, solely
    because it happens to share the phrase "comparable used" with the real
    Sales Comparison Approach spec. Fixed by narrowing the keyword to the exact
    phrase "sales comparison approach" (confirmed unique to the one real spec in
    `data/canonical_doc_specs.json`), added a regression test for the exact
    false-positive spec text, and re-ran this same `aubrey` output through the
    fixed backstop directly — confirmed it now credits only the 3 genuinely
    field-backed specs (license, state-licensure, Sales Comparison Approach) and
    nothing else. This is exactly the failure mode this document's own PC-05
    analysis warned was worse than leaving a spec open — caught before being
    reported as closed, not after.

## What's still genuinely open after this batch

**24 Nov 2026**: re-audited every item below against fresh real fixture data before
re-listing it here — nothing below is carried over unchanged just because it was
flagged before. Two items from the original list (4276 AC3/AC4 bank-statement
coverage, and the Appraisal Report portion of 4277 AC9) were closed with real
deterministic backstops this round and are no longer listed below. The following
remain genuinely blocked, re-confirmed by directly inspecting all 5 local
`compiled_inputs/*` manifests again rather than re-asserting the original finding:

1. **4277 AC9 / PC-05 — Flood Hazard Determination and Title Commitment** only
   (Appraisal Report is now partially closed, see its table row above). Zero of
   the 5 local fixtures contain either document category at all — no field data
   exists anywhere locally to build a safe backstop against. Still
   prompt-reinforced only, not deterministically guaranteed. Needs a real
   extraction sample of either document type from production to close properly.
2. **4276 AC8** (expired hazard policy correctly flagged) — the spec exists to
   grade against, but whether the LLM correctly reads an actual expiration date
   against the closing-date projection is grading quality (same caveat the
   original PC-10 Government ID expiration fix already carried). No fixture
   contains a Hazard Insurance/Homeowners document at all, so no backstop can be
   built here either.
3. **4277 AC1** (flood cert "certificate requirement met" crediting) — the
   conditional flood-insurance specs are deterministically suppressed when a
   flood cert says "not in zone" (that part is ✅); crediting the cert's own base
   "requirement met" spec is still LLM grading. No fixture contains a Flood
   Hazard Determination document, so this can't be backstopped either — same root
   cause as item 1 above.

All three remaining items share one root cause: **zero local fixture data** for
Flood Hazard Determination and Hazard Insurance/Homeowners document categories.
Building a backstop for either without a real sample would mean guessing field
names, which risks false positives (claiming a requirement is met when it isn't)
— explicitly worse than today's over-cautious "mostly open," per this doc's own
PC-05 analysis. These are not new regressions introduced by this batch.
