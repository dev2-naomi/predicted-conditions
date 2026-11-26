# PC accuracy (3-loan UAT, 17 Nov 2026) — fix status report

Source report: *"Predicted Conditions engine: accuracy on 3 real loans (UAT, 17 Nov 2026)"*
(`pc-accuracy-3-loans-1117.pdf`). 15 issues, IDs PC-01 through PC-15.

This report answers: **what did the 19 Nov 2026 fix commit actually fix, what does a
live run on today's code (23 Nov 2026, branch `fix/pc-accuracy-uat` / `dev`) confirm,
and what's still open.**

## Method

1. **Code review** of commit `84ec0e0` ("Fix 10 engine accuracy issues from 3-loan
   UAT audit") against the report's 15 issues, line by line.
2. **Live run** against the deployed **dev** Lambda (the same one just redeployed
   with these fixes + the `current_program` field) using `compiled_inputs/nyarko` —
   a local fixture that is the closest available stand-in for UAT loan **511**
   (Investor DSCR, purchase, investment property; the real 510/511/512 loan data is
   real-borrower PII and isn't committed to this repo).
   - Thread `ddb5535b-f802-48df-91e7-5cfab0e8180b`, run `0ab864a9-73d2-4b85-bb1e-ec40fa3f319e`,
     finished `success` in 157s.
   - Output: `test_results/05_15/nyarko_cloud/nyarko_cloud_output.json` (25 document
     requests) — compared against the **pre-fix** baseline already on disk at
     `test_results/nyarko/nyarko_output.json` (18 document requests, generated before
     the 19 Nov fix).
3. **A real gap found by that live run** was fixed today and covered by a new unit
   test (`tests/test_closing_stage_doc_suppression.py`, 5/5 passing) — see PC-01/PC-04
   below.

Full suite: **207 passed, 48 skipped** (skips are pre-existing, gated on local-only
real-loan fixtures not committed to git; 0 failures) as of the 23 Nov 2026 ticket
4275 follow-up (+9 tests: `tests/test_task_tile_category_mapping.py`'s 8, plus 1
new test in `tests/test_manifest_party_resolution.py`).

## Status summary

| ID | Issue | Status |
|---|---|---|
| PC-01 | Closing-stage docs predicted pre-closing (Deed of Trust, Grant Deed, Owner Occ. Cert., Title Invoice) | **Fixed** (Deed of Trust / Grant Deed / Owner Occ. Cert.) · Title Invoice still open |
| PC-02 | Docs that don't apply to program/purpose/occupancy | **Partially fixed** — 4506-C done; DSCR-purchase lease/rental-worksheet still open |
| PC-03 | Accepted alternative requested anyway (VOD vs statements, VOR vs mortgage statements) | **Partially fixed** — VOD done; VOR vs mortgage statements still open |
| PC-04 | Owner Occupancy Certification predicted on every loan | **Fixed** (folded into PC-01's gate) |
| PC-05 | Linked doc, nothing marked met (Appraisal, Flood, Title Commitment, UCDP SSR) | **Not addressed** |
| PC-06 | Conditional/alternative/unrelated requirements stay open | **Partially fixed** — flood wiring + 2-4-unit bug done; per-peril Hazard lines, EMD either-or, cross-doc requirement bleed still open |
| PC-07 | Task Tile category names not mapped | **Fixed and unit-tested** (5/5 category gaps closed per ticket 4275's acceptance criteria) |
| PC-08 | No rule for choosing between duplicate copies | **Fixed** |
| PC-09 | Period-coverage requirements checked against one document | **Likely fixed for Bank Statement** (live-confirmed, 5 docs linked); not independently reverified for paystubs this run |
| PC-10 | One ID per borrower + expiry check | **Fixed** |
| PC-11 | Duplicate conditions in one run | **Partially addressed** — root-cause attribution uncertain, see notes |
| PC-12 | Empty extracted data read as a clean result | **Fixed** |
| PC-13 | Conditions with no document and no requirements | **Incidentally improved**, no generic rule added |
| PC-14 | Gap to the underwriter | **Out of scope** — report explicitly calls this a future objective, not a defect |
| PC-15 | Failed/blocked runs leave no trace | **Not addressed** — infra/observability, outside this commit's scope |

**10 of 15 fixed or effectively fixed (01, 04, 07, 08, 09, 10, 12, plus today's PC-01
gate-hardening); 4 partially fixed (02, 03, 06, 11); 1 incidentally improved (13); 2
explicitly out of scope for this version (14, 15).**

## Detail

### PC-01 / PC-04 — closing-stage documents

**19 Nov fix:** removed Deed of Trust, Grant Deed, and Owner Occupancy Certification
from the unconditional document floor (`tools/doc_rules.py::mandatory_docs()` /
`conditional_docs()`).

**Live-run finding (today):** that fix was **incomplete**. The pre-fix baseline
(`test_results/nyarko/nyarko_output.json`) has all three. A fresh run on dev
*after* the 19 Nov fix (before today's patch) still predicted **Deed of Trust**
and **Owner Occupancy Certification** — both with zero `document_ids`, status
`needed` — because the per-module LLM steps (01-07) independently re-propose them
from their own guideline reasoning; removing them from the deterministic *floor*
never stopped the *LLM* from asking for them anyway. Grant Deed happened not to
recur on this particular fixture, but had no gate either.

**Fixed today:** added `_CLOSING_STAGE_SUPPRESSED` to
`apply_negative_gates()` (`tools/doc_rules.py`) — an unconditional gate that strips
all three document types regardless of which layer produced them. Covered by
`tests/test_closing_stage_doc_suppression.py` (5 tests: plain purchase, DSCR
investment purchase, refinance, case-insensitivity, unrelated docs survive).

**Re-verified live, post-deploy (2026-11-23):** pushed the fix to `dev`, redeployed
(`PredictedConditionsStack-Dev`, run succeeded), then re-ran the exact same
`compiled_inputs/nyarko` case against the freshly-deployed Lambda a third time
(thread `dc0cd230-a4c7-4d02-87ed-3b8d6eed8f2f`, run `99837772-0ef7-4175-bb49-4b39379d24bd`,
success in 142s). Result: **25 → 22 document requests** — "Deed of Trust" and "Owner
Occupancy Certification" are both gone, and nothing else that should still be there
(Title Invoice, Payoff Statement, Current Lease Agreement, etc. — the still-open
items below) was accidentally dropped. "Investment Account Statement" also didn't
recur on this run, but that's ordinary LLM run-to-run variance, not caused by this
gate (which only pattern-matches the three closing-stage type names).

**Still open:** Title Invoice. The report flags it as predicted on all 3 loans with
only 1 of 3 being a genuine match; the live run also predicts it unconditionally
(`needed`, 0 `document_ids`) on nyarko. No fix was attempted for this one — it isn't
mentioned in the 19 Nov commit message at all.

### PC-02 — documents that don't apply to program/purpose/occupancy

**Fixed:** IRS 4506-C Authorization suppressed on DSCR and bank-statement loans
(`_DSCR_SUPPRESSED` / `_BANK_STATEMENT_SUPPRESSED`). Live-confirmed: absent from the
nyarko (DSCR) run output. Paystub was already suppressed for DSCR pre-existing this
commit; also live-confirmed absent.

**Still open:** on a DSCR **purchase** (no lease exists yet, pre-closing — the
report's own reasoning), `doc_rules.py`'s `if flags["is_dscr"]:` block
unconditionally injects **"Rental Income Calculations Worksheet"** and **"Current
Lease Agreement"/"Rental Agreement"** with no purpose gate. Both appeared, unlinked,
in the live run — exactly the report's PC-02 finding for loan 511 ("the underwriter
used the appraisal's rent schedule" / "there is no lease yet").

### PC-03 — accepted alternatives

**Fixed:** Verification of Deposit suppressed once a real bank/401K/brokerage
statement is in the file (`_VOD_DOCS` + `_has_asset_statement()`). Live-confirmed:
absent from the nyarko run despite Bank Statement + Investment Account Statement
both being present and linked.

**Still open:** Verification of Rent as an alternative to mortgage-statement housing
history (the report's loan-511 finding) has no equivalent gate. Live run shows
**Verification of Rent** (`needed`, unlinked) predicted alongside **Verification of
Mortgage** (`needed`, also unlinked but injected because `has_reo`/REO is true) —
i.e. the same "ask for the alternative on top of the real evidence" pattern the
report flagged, just not yet fixed for this pair.

### PC-05 — linked document, nothing marked met

**Not addressed.** No changes were made to `cross_check_satisfaction`'s
satisfaction-grading logic or to the Appraisal/Flood/Title Commitment/UCDP SSR
canonical specs. Live evidence: **Appraisal Report** was linked (1 real document)
and only 1 of 16 specs were marked satisfied — consistent with the report's "0 or 1
of 13-18" finding. Flood/Title Commitment/Credit Report/UCDP SSR had no document
linked at all on this fixture (a different, matching-related symptom — this
fixture's manifest doesn't carry those categories the same way the real UAT loans
did), so this run doesn't independently confirm or deny PC-05 for those four, but
nothing in the codebase addresses the underlying issue either way.

### PC-06 — conditional/alternative/unrelated requirements stay open

**Fixed:** the loan XML's real flood determination
(`special_flood_hazard`/`flood_insurance_required`) is now wired through
`xml_parser.py` → `scenario_tools.py` → `doc_rules.py` instead of a hardcoded
`True`; and the `property_units` resolver bug that conflated "2-4 unit" with "1-4
unit" (which force-applied a multi-unit-only appraisal-form requirement to every
single-family loan) is fixed.

**Still open:** one requirement line per insured peril on Hazard Insurance (live run
shows 31 specs on Hazard Insurance — matches the report's "25-27 open" complaint
almost exactly); EMD Check's mutually-exclusive "cleared"/"not yet cleared"
either-or wording; and a document's open-requirements list bleeding in
requirements that belong to a *different* document (the report's W-2/paystub/4506-C
example). None of these were touched by the 19 Nov commit.

### PC-07 — Task Tile category name mapping

**Fixed, now test-verified.** `_DOCTYPE_ALIASES` in `tools/merger_tools.py` gained
"Verification of Income" → Verification of Employment, "Transaction History" →
EMD Check, and a new "Non QM Bank Statement Analysis Worksheet" entry aliasing
"Income Calculations Worksheet".

**23 Nov 2026 follow-up (ticket 4275):** reviewing this ticket's 6 explicit
acceptance criteria line-by-line against the code found 2 more category names
the first pass missed, both now fixed:

- "4506-C IVES" → IRS 4506-C Authorization (loan 513 — the underwriter's actual
  4506-C copy was filed under this category and was never recognized)
- "Asset Worksheet" → Asset Depletion Worksheet (loan 513)

All 6 of ticket 4275's acceptance criteria are now covered by a dedicated,
passing unit test suite: `tests/test_task_tile_category_mapping.py` (8/8
passing), which tests the exact `_find_all_submitted_docs()` lookup used by
the real satisfaction pass — not just code review. Confirmed the two new
aliases were genuine gaps before the fix (0 matches under the pre-fix alias
sets, re-verified by temporarily reverting the change and re-running).
One pre-existing test in `tests/test_manifest_party_resolution.py` had to be
updated because it used "Asset Depletion Worksheet" as its canonical example
of "a document type with zero masterlist representation" — that's no longer
true now that "Asset Worksheet" (masterlist doctype_id 1824) is a recognized
alias, which is itself a second, free side-effect fix: Asset Depletion
Worksheet requests now also get a real `category_ids` value instead of
staying empty.

Full category → condition mapping table (ticket 4275's "send the full list"
ask): see [`docs/task-tile-category-mapping.md`](./task-tile-category-mapping.md).

Not yet independently re-exercised against a *live* cloud run (no committed
fixture uses these exact category names) — verified via unit test against the
real matching function, not an end-to-end run.

### PC-08 — duplicate-copy selection rule

**Fixed.** `tools/shared/manifest_parser.py`'s dedup now ranks candidates by
`(is_signed, confidence, timestamp)` instead of timestamp alone (`_doc_rank()` /
`_is_signed()`), so a signed copy always wins over an unsigned one regardless of
which was indexed first. Verified by code review; not independently re-exercised
live this run (would need a manifest fixture with mixed signed/unsigned copies of
the same form to confirm end-to-end).

### PC-09 — period-coverage requirements

**Likely fixed for Bank Statement**, live-confirmed: the nyarko run links **5**
Bank Statement documents to the one condition (not just 1), and the condition
grades `satisfied_but_review_required` rather than hard-failing on partial-period
coverage. Paystub's existing 30-day-window merge logic predates this commit and
wasn't independently reverified this run.

### PC-10 — one ID per borrower + expiry

**Fixed.** `_person_signature()` in `manifest_parser.py` now also falls back to
`name`/`fullName` fields (not just `owner`/`customer`) so passports without an
`owner` field still split correctly per borrower instead of colliding. A new
canonical spec ("Government ID must remain valid through the anticipated closing
date") was added to `data/canonical_doc_specs.json` for the expiry-flagging half of
this issue. nyarko is single-borrower, so the per-borrower-split half isn't
independently re-exercised this run.

### PC-11 — duplicate conditions in one run

**Partially addressed, root cause uncertain.** `data/document_party_scope.json` now
marks EMD Check, Title Invoice, and Prequal Response Form as loan-level
(`party=None`, one shared copy) instead of per-borrower — confirmed via
`tools/coborrower.py::apply_coborrower_pass()` that this does prevent those three
specific document types from being duplicated once per party on a multi-borrower
loan. **However**, the report's actual symptom (512: 34 total conditions, with
conditions 22-34 being a near-exact repeat of 9-21 by name) looks more like a
*whole-batch* duplication — possibly STEP_08's merge/rank/finalize chain running
twice — rather than specifically the 2-3 loan-level document types this fix
targets. nyarko is single-party, so this isn't independently re-exercisable with
the fixtures on hand. **Recommend confirming the real root cause against an actual
multi-condition-set duplicate before considering this fully closed.**

### PC-12 — empty extracted data read as a clean result

**Fixed.** `tools/merger_tools.py::_apply_tradeline_mortgage_backstop()` no longer
auto-satisfies tradeline-detail specs just because `creditTradeLines` is a
null-stub/empty field (kept only for `mortgageSummary`, where a genuinely-zero
mortgage history is plausible). Not independently re-exercised live this run (would
need a credit-report fixture with an empty tradelines field to confirm end-to-end).

### PC-13 — conditions with no document and no requirements

**No dedicated rule added.** Today's PC-01/PC-04 gate incidentally eliminates the
exact zero-spec/zero-reason "Owner Occupancy Certification" instance seen in the
live run, but there's still no *generic* filter in `generate_final_output()` (or
elsewhere) that drops ANY condition carrying neither a linked document nor at least
one requirement/reason, as the report recommends ("a condition should always carry
at least one requirement, or not be predicted"). Recommend adding one as
defense-in-depth.

### PC-14 — gap to the underwriter

**Explicitly out of scope for this version** — the report itself states this is
"the future objective, not a defect of this version," measuring the gap rather than
flagging a bug. No action taken, correctly.

### PC-15 — failed/blocked runs leave no trace

**Not addressed.** This is an observability/infra concern (visible `failed`/
`blocked` run events, auto-starting the run when Task Tile completes), not
document-generation logic — outside `tools/doc_rules.py`/`merger_tools.py`'s scope
and untouched by the 19 Nov commit. Would need to be addressed in the
orchestration/trigger layer (outside this repo, or in `api/`).

## Recommended next fixes (in rough priority order)

1. **PC-02/PC-03 remaining gaps** — gate "Current Lease Agreement"/"Rental Income
   Calculations Worksheet" off on DSCR *purchase* (no lease exists pre-closing);
   gate "Verification of Rent" off when mortgage-statement/REO history already
   covers it (mirror the VOD fix's `_has_asset_statement()` pattern).
2. **PC-01 Title Invoice** — same unconditional-gate treatment, or a conditional
   gate keyed on whether a title-invoice-shaped document actually exists in the
   manifest.
3. **PC-13 generic empty-condition filter** in `generate_final_output()`.
4. **PC-11 root-cause confirmation** — reproduce the 34-conditions/13-repeats
   symptom (or get a sanitized copy of loan 512's actual engine output) to confirm
   whether the document-party-scope fix is sufficient or whether STEP_08 itself can
   double-run.
5. **PC-06 remaining items** — collapse per-peril Hazard Insurance lines into one
   "standard homeowners coverage" requirement; fix EMD Check's either-or wording;
   scope each condition's open-requirements list to only that document's own specs.
6. **PC-05** — the biggest remaining lift: improve satisfaction-grading for
   Appraisal/Flood/Title Commitment/UCDP SSR so that when the right document *is*
   linked, its baseline requirements get credited instead of staying open by
   default.
7. **PC-15** — add a visible failed/blocked run event + auto-start-on-Task-Tile-
   completion, in the orchestration layer.

## 23 Nov 2026 addendum — tickets 4276/4277/4278

Three follow-up tickets (4276 "Link the Right Copy of Each Document", 4277 "Keep
Each Condition's Requirements to What Applies", 4278 "Add the Documents Almost
Every Loan Needs") were implemented together and pushed to `dev` in commit
`7475ea3`. This closes most of **PC-06**'s remaining items and adds new coverage
beyond the original 15-issue report.

**Ticket 4276 (duplicate-copy selection + property scoping):**
- `_doc_rank()` (`tools/shared/manifest_parser.py`) now orders candidates by
  content-date before confidence ("use the newer copy; same date → higher
  confidence"), refining PC-08's signed/confidence/timestamp rule rather than
  replacing it.
- New `_filter_to_subject_property()` (`tools/merger_tools.py`) excludes
  Hazard Insurance / Flood Hazard Determination / Rental Agreement documents
  that carry a different property's address than the subject property.
- New expiration-check specs for Hazard Insurance and Flood Hazard
  Determination, mirroring PC-10's Government ID expiration pattern.
- Tests: `tests/test_ticket_4276_duplicate_copy_selection.py` (11/11 passing).

**Ticket 4277 (requirement scoping — closes most of PC-06):**
- `channel` condition resolver wired up (`is_wholesale`/`is_retail`/
  `is_correspondent` flags) — also retroactively fixes several dormant
  `channel=Wholesale`/`channel=retail_or_wholesale` conditions already sitting
  in `data/canonical_doc_specs.json` with no resolver before this.
- `flood_insurance_required` now also sourced from a submitted Flood Hazard
  Determination document's own extracted zone data when the loan XML itself
  carries no signal.
- Hazard Insurance's 8 per-peril coverage lines consolidated into one standard-
  coverage requirement (PC-06's "25-27 open specs" complaint).
- Paystub/4506-C/VOE items that were bleeding into the W-2 canonical spec
  removed (PC-06's W-2/paystub/4506-C cross-bleed example).
- EMD Check's either-or: `has_emd_evidence` flag suppresses the "check has not
  yet cleared" branch once clearing evidence is on file (PC-06's EMD either-or
  wording issue).
- Generic fallback requirement (`apply_guideline_canonicalization()`) when a
  scenario resolves every conditioned item in a doc_type to False, so a
  condition is never left with zero requirements — a defense-in-depth measure
  in the same spirit as **PC-13**'s recommendation, though scoped to this one
  function rather than a global filter in `generate_final_output()`.
- Tests: `tests/test_ticket_4277_requirement_scoping.py` (12/12 passing).

**Ticket 4278 (new baseline documents):**
- 7 new deterministic document types: Title Company Wiring Instructions,
  Title Company E&O Insurance, Closing Protection Letter, Property Tax
  Certificate (all gated on `is_wholesale`); Appraisal Delivery Confirmation
  (every loan); W-2 or 1099 Tax Transcripts (`has_wage_or_1099_income`);
  Articles of Organization + Federal Tax Id Number (`is_llc` — specs already
  existed in `canonical_doc_specs.json`, just had no request-generation);
  Appraisal Transfer Letter + AIR Certification (`appraisal_different_lender`).
- New `_DOCTYPE_ALIASES` entries and `data/document_party_scope.json` entries
  for all 9 new types (unconfirmed against real Task Tile category names —
  see inline code comments — unlike PC-07/4275's aliases, which were all
  confirmed against real UAT manifests).
- Tests: `tests/test_ticket_4278_new_documents.py` (11/11 passing).

**Full regression:** 241 passed, 48 skipped, 0 failures (up from 207 passed
before this batch — +34 new tests across the three ticket-specific files).

**Live e2e verification (post-deploy, dev):**
- `compiled_inputs/aubrey` (Wholesale channel, LLC=False) — thread
  `b3f4cb15-d886-4d52-9700-b94cc159a716`, success in 158s. Confirmed live:
  Closing Protection Letter, Title Company Wiring Instructions, Title Company
  E&O Insurance, Property Tax Certificate, and Appraisal Delivery Confirmation
  all generated; Hazard Insurance's specs show the single consolidated
  coverage line + new expiration check, not the old 8 per-peril lines.
- `compiled_inputs/niccum` (Retail channel — negative control) — thread
  `8490afa9-b8b6-49b6-8d0f-27b683471fe9`, success in 154s. Confirmed live:
  none of the 4 wholesale-gated document types appear; Appraisal Delivery
  Confirmation still appears (universal trigger); EMD Check's "not yet
  cleared" branch stays open as expected (no EMD-evidence document on file).
- `compiled_inputs/montes` (Wholesale, has "EMD Docs" — would have been the
  best live test of the EMD-evidence suppression) **originally failed before
  reaching the agent at all**: `500 - "An error occurred (ValidationException)
  when calling the PutItem operation: Item size has exceeded the maximum
  allowed size"`. Root cause confirmed via CloudWatch/direct API call: `api/
  thread_store.py`'s `create_run()` stores the full raw-input payload
  (`loan_file_xml` + `manifest_json` + `eligibility_json`) as one DynamoDB
  item, and montes' combined ~455KB exceeds DynamoDB's 400KB item-size cap.
  This was a pre-existing infra limitation, not a regression from any batch's
  document-generation diffs — aubrey's ~365KB combined payload (just under
  the cap) always succeeded cleanly on the same deploys.

  **✅ Fixed 24 Nov 2026.** `DynamoDBThreadStore.create_run()` now offloads
  any run_body over ~300KB to S3 (reusing the exact same `CheckpointOffloadBucket`
  `api/checkpointer.py`'s `DynamoDBSaver` already uses for oversized
  checkpoints — same IAM grants, same bucket, no new infra needed) and stores
  a small pointer (`input_payload_ref`) in DynamoDB instead of the payload
  itself. `get_run()` transparently resolves that pointer back to
  `input_payload` on read, so every caller (`load_persisted_run`, the Lambda
  self-invoke dispatch path, `execute_background_run`) needed zero changes.
  10 new unit tests in `tests/test_thread_store_s3_offload.py` cover: small
  payloads staying inline (no regression), oversized payloads offloading
  with the pointer round-tripping correctly, the no-bucket-configured
  fallback (still fails loudly rather than silently losing data), and
  backward compatibility with the pre-existing decimal-conversion test
  file's `__new__`-bypass-`__init__` construction pattern.

  **Live-confirmed, 24 Nov 2026**: redeployed (`e12f616`, `deploy` job green
  in 3m27s) and re-ran `compiled_inputs/montes` end-to-end. Confirmed directly
  against AWS, not just the client-side response: the run's DynamoDB item
  (`RUN#922d985e-2e72-433a-893c-b0bacf7ba610`) carries an `input_payload_ref`
  pointer instead of an inline payload, pointing at a 486,258-byte object in
  the `CheckpointOffloadBucket` (`runs/922d985e-.../input.json`) — montes'
  real combined payload, offloaded exactly as designed. The run itself
  finished with `status=success` in 143s (no 500, no ValidationException),
  producing 27 document requests. This also finally unblocked the ORIGINAL
  reason montes was picked for this round of testing — the EMD-evidence
  suppression: EMD Check came back `satisfied_but_review_required`, correctly
  crediting "acceptable source of funds" from the real title-company EMD
  document + matching bank statement, and correctly marking the 12-month
  escrow-letter spec "Not applicable" since the contract (2026-02-09) and the
  EMD document (2026-03-02) are less than a month apart — both exactly the
  kind of real-data confirmation this fixture was originally chosen for but
  couldn't reach before this fix.

**Deferred / not addressed (same ticket text, explicitly out of scope for this
batch):** period-set "names the missing month" naming enhancement (4276); the
Note's pre-closing cross-document check (4277); explicit "not verified"
wording for empty-tradeline credit report specs (4277 — the underlying
false-positive bug was already fixed by PC-12); UCDP SSR cross-run consistency
(4277 — judged an LLM-grading-variance characteristic, not a fixable
deterministic-code bug). Property-address matching, flood-zone field names,
and appraisal-lender-mismatch detection are implemented as best-effort against
plausible field names (see inline code comments in `tools/merger_tools.py` and
`tools/doc_rules.py`) since no local fixture carries real data to confirm the
exact extracted-field keys against.

## 24 Nov 2026 addendum — ticket 4274 (closes PC-01's Title Invoice item, PC-02's
DSCR-purchase lease item, PC-03's VOR item, and PC-11)

Ticket 4274 ("Don't Ask for Documents the Loan Can't Need") restates PC-01/02/
03/04/11 with 8 explicit, directly testable acceptance criteria plus a Title
Invoice item that's explicit in "what's asked" but not in the numbered ACs.
Audited each AC against current code; 4 were already satisfied by prior
fixes (AC1 Deed of Trust/Grant Deed, AC2 Owner Occupancy Certification, AC3
DSCR wage docs, AC5 VOD, AC8 wage-earner negative control), 3 were genuinely
open and fixed this round (AC4, AC6, AC7), plus the non-numbered Title
Invoice item.

**AC4 — DSCR purchase + appraisal rent schedule → no Current Lease
Agreement / Rental Income Calculations Worksheet (closes PC-02's remaining
item):** new `_has_appraisal_rent_schedule()` (`tools/doc_rules.py`) scans a
submitted Appraisal Report's extracted fields for Form 1007/1025
rent-schedule evidence (best-effort field-key + free-text scan, no local
fixture has real appraisal extraction data to confirm exact field names
against). New `_RENT_SCHEDULE_SUPPRESSED` gate drops Rental Agreement /
Rental Income Calculations Worksheet when `is_purchase AND
has_appraisal_rent_schedule`. Guideline-grounded:
`data/canonical_doc_specs.json`'s "Single Family Comparable Rent Schedule
(FNMA Form 1007) must be included for all 1-unit Investment Properties when
rental income is used to qualifying" and
`data/conditions_masterlist.json`'s "[provide] Form 1007 Comparative Rent
Schedule (if income is being used)" both treat the 1007/1025 as a standalone
alternative to a lease, not an additional requirement on top of it. Scoped
to purchases only (a refinance with an existing tenant still needs its own
lease evidence even if the appraisal also carries a 1007).

**AC6 — mortgage statements or a rent-free letter in file → no Verification
of Rent (closes PC-03's remaining item):** new
`_has_mortgage_statement_evidence()` / `_has_rent_free_letter()` +
`_VOR_DOCS` gate, same alternative-evidence pattern as the existing
VOD/`_has_asset_statement()` fix. Guideline-grounded:
`data/canonical_doc_specs.json`'s "Borrowers living rent-free must provide a
rent-free letter from the property owner instead of a VOR";
`data/doctype_masterlist.json` has "Mortgage Statement" as its own real
document type distinct from Verification of Mortgage.

**AC7 — no condition name appears twice on a multi-borrower, large file
(closes PC-11):** root cause confirmed as suspected —
`merge_document_requests`'s existing `_merge_key` (document_type +
document_context) lets two per-module LLM proposals for the SAME real-world
document need survive as separate groups whenever document_context is
populated inconsistently between them (one proposal tags
`{"borrower": "..."}`, another for the identical need leaves it empty) —
each then carries different (sometimes contradictory) content under the
same display name, matching the report's "34 conditions, 13 repeated
another by name with a different result" symptom. New
`_collapse_duplicate_canonical_types()` (`tools/merger_tools.py`) runs a
second, coarser merge pass keyed on canonical document_type alone,
immediately after the existing context-aware merge — consistent with how
every other multi-copy scenario in this codebase is represented (one
document_request carrying multiple document_ids, not multiple rows for the
same type). Does not affect `tools/coborrower.py`'s later, intentional
one-row-per-party split for borrower-specific types on a multi-borrower
loan (that pass re-derives its own rows from this now-deduped canonical set
afterward).

**Title Invoice ("what's asked", not a numbered AC):** new
`_has_title_invoice_evidence()` + conditional gate — stops flooring Title
Invoice as an open "needed" ask when nothing in the submitted-document set
suggests one actually exists yet, but (unlike Deed of Trust/Grant
Deed/Owner Occupancy Certification) does NOT ban it outright, since an
earlier 10x-consistency audit found it genuinely missing from engine output
on loans where the underwriter DID expect a Title Fee Sheet/Smart Fee per
NQMF submission requirements (see `mandatory_docs()`'s existing comment —
a real, contradictory signal from an earlier audit). **Flagged per the
ticket's own "confirm against guidelines before changing any rule"
instruction: this is a proposal, not a guideline-verified rule** — the
open question is whether the title company's invoice legitimately doesn't
exist until near/at closing on every program, or only on some.

**Tests:** `tests/test_ticket_4274_predicted_conditions_scope.py`, 28/28
passing — one test class per AC (plus the Title Invoice item), each run
against the real `mandatory_docs()`/`conditional_docs()`/
`derive_income_docs()`/`apply_negative_gates()` pipeline (not a synthetic
doc list standing in for it), including simulated-LLM-reproposal tests for
the 3 ACs whose document types the floor no longer injects at all (Deed of
Trust, Grant Deed, Owner Occupancy Certification) — confirming the gate
itself still catches them, not just that the floor's absence was never
exercised. Full regression: **291 passed, 48 skipped, 0 failures** (up from
263 — +28 new tests).

**Live e2e verification + a real bug the first deploy of this ticket found:**
first deploy's `compiled_inputs/nyarko` run still showed "Title Invoice" on
the list despite nyarko having no title-invoice-shaped evidence. Root cause:
`apply_deterministic_rules()`'s `eligibility_required_docs()` injects "Title
Invoice" via a `bypass_negative_gates=True` path (added earlier so
authoritative eligibility-engine-required docs like Bank Statement/Profit
and Loss aren't wrongly swept by broad heuristic gates) — this path skips
`apply_negative_gates()` entirely, so the new evidence gate never actually
ran on a real loan whose eligibility JSON flags "Title Invoice" as a Minimum
Required Document category, which is exactly what every one of the 4 UAT
loans' real eligibility JSON does (confirmed in
`data/input/*/eligibility_output.json`'s `"Title Invoice": true`/`"Missing
base documents: ... Title Fee Sheet ..."` entries) — i.e. this bypass path
*was* the actual mechanism producing the ticket's reported over-ask.
**Fixed:** added a narrow carve-out directly in `_inject()` so "Title
Invoice" specifically always goes through its own evidence check regardless
of `bypass_negative_gates`, while every other eligibility-required doc type
keeps bypassing normally (confirmed via a dedicated negative-control test —
see `test_other_eligibility_required_docs_still_bypass_normally`). Re-ran
`compiled_inputs/nyarko` against the corrected redeploy: total document
requests dropped from 28 to 27 (exactly the "Title Invoice" removal, with
nothing else changed), "Deed of Trust"/"Grant Deed"/"Owner Occupancy
Certification" still correctly absent, "Verification of Rent"/"Current
Lease Agreement"/"Rental Income Calculations Worksheet" are all still
correctly PRESENT (nyarko has no mortgage-statement/rent-free-letter/
appraisal-rent-schedule evidence, so AC4/AC6's gates correctly did NOT
over-suppress — negative control intact), the Appraisal Report's Note
cross-checks still show `not_yet_available_pre_closing` (4277 regression
check), and no document type name repeats across the 27-item output (4274
AC7 regression check on a real, not synthetic, run).
