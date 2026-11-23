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

Full suite: **198 passed, 48 skipped** (skips are pre-existing, gated on local-only
real-loan fixtures not committed to git; 0 failures).

## Status summary

| ID | Issue | Status |
|---|---|---|
| PC-01 | Closing-stage docs predicted pre-closing (Deed of Trust, Grant Deed, Owner Occ. Cert., Title Invoice) | **Fixed** (Deed of Trust / Grant Deed / Owner Occ. Cert.) · Title Invoice still open |
| PC-02 | Docs that don't apply to program/purpose/occupancy | **Partially fixed** — 4506-C done; DSCR-purchase lease/rental-worksheet still open |
| PC-03 | Accepted alternative requested anyway (VOD vs statements, VOR vs mortgage statements) | **Partially fixed** — VOD done; VOR vs mortgage statements still open |
| PC-04 | Owner Occupancy Certification predicted on every loan | **Fixed** (folded into PC-01's gate) |
| PC-05 | Linked doc, nothing marked met (Appraisal, Flood, Title Commitment, UCDP SSR) | **Not addressed** |
| PC-06 | Conditional/alternative/unrelated requirements stay open | **Partially fixed** — flood wiring + 2-4-unit bug done; per-peril Hazard lines, EMD either-or, cross-doc requirement bleed still open |
| PC-07 | Task Tile category names not mapped | **Fixed** |
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

**Fixed.** `_DOCTYPE_ALIASES` in `tools/merger_tools.py` gained "Verification of
Income" → Verification of Employment, "Transaction History" → EMD Check, and a new
"Non QM Bank Statement Analysis Worksheet" entry aliasing "Income Calculations
Worksheet". Not independently re-exercised this run (nyarko's manifest doesn't use
these exact category names), verified by code review only.

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
