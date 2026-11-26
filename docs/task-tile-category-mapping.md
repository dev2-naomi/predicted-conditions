# Task Tile category → condition mapping (ticket 4275)

Full list, as requested in ticket 4275 ("send the full list of which Task Tile
categories each condition accepts today, for joint review"). Generated
directly from `tools/merger_tools.py`'s `_DOCTYPE_ALIASES` /
`_DOCTYPE_EVIDENCE_ALIASES` / `_DOCTYPE_BLANKET_ALIASES` on 23 Nov 2026 — not
hand-maintained, so it will go stale if read later; regenerate with:

```bash
python3 -c "
import tools.merger_tools as mt
for canon, variants in sorted(mt._DOCTYPE_ALIASES.items()):
    print(canon.title(), '->', sorted(variants))
"
```

A submitted manifest document is recognized as evidence for a condition if
its Task Tile `category_name` (carried through as the submitted-doc `name`
field) — or its own literal document_type name — appears in the table below.
Matching is case-insensitive and underscore/space-insensitive.

## Standard aliases (name variants of the same document)

| Condition (canonical `document_type`) | Task Tile category names accepted |
|---|---|
| 1099 | "1099 form", "1099 income", "1099-nec", "form 1099-nec" |
| Appraisal Report | "appraisal", "appraisal report (urar)" |
| Asset | "asset statement", "other asset", "proof of assets" |
| **Asset Depletion Worksheet** | "asset depletion calculation", "asset utilization calculation", "asset utilization worksheet", **"asset worksheet"** ← added (ticket 4275 / AC4) |
| Balance Sheet | "business balance sheet", "company balance sheet" |
| Bank Statement | "business bank statements", "most recent asset statement for proof reserves", "other asset", "personal bank statements" |
| Borrower Authorization To Sign | "authorization to sign", "borrowing certificate", "certificate of authorization", "llc borrowing certificate", "signing authority", "signing authorization" |
| Borrower Certification As To Business Purpose | "borrower certifications and disclosure" |
| Certificate Of Good Standing | "certificate of status", "good standing certificate" |
| Credit Report | "credit report (rmcr)", "tri-merge credit report" |
| **EMD Check** | "earnest money check", "earnest money deposit check", "emd", **"transaction history"** ← added (PC-07 / ticket 4275 AC2) |
| Federal Tax Id Number | "ein confirmation", "ein letter", "employer identification number", "federal tax id", "tax id number" |
| Flood Hazard Determination | "flood certification", "flood determination" |
| Form 1040 | "1040", "individual tax return", "irs form 1040", "personal tax return" |
| Government-Issued Photo Id | "drivers license", "identification", "passport", "photo id" |
| Hazard Insurance | "homeowners insurance", "insurance binder", "property insurance" |
| Income LOE | "income explanation letter", "income letter of explanation", "letter of explanation for income" |
| Investment Account Statement | "brokerage account statement", "brokerage statement", "investment statement", "other asset" |
| **IRS 4506-C Authorization** | "4506-c", "4506c", "irs form 4506-c", "tax transcript authorization", **"4506-c ives"** ← added (ticket 4275 / AC5) |
| Loan Application (1003) | "1003", "final loan application", "final loan application (1003)", "initial loan application", "initial loan application (1003)", "loan application", "loan_application", "uniform residential loan application", "urla", "urla 1003" |
| LOE For HOA Dues | "hoa dues letter", "hoa letter of explanation", "hoa loe", "letter of explanation for hoa dues" |
| **Non QM Bank Statement Analysis Worksheet** | "bank statement analysis worksheet", "income calculation worksheet", **"income calculations worksheet"** ← added (PC-07 / ticket 4275 AC3) |
| Operating Or Partnership Agreement | "list of llc members", "llc member list", "llc members", "member list", "operating agreement", "partnership agreement" |
| Owner Occupancy Certification | "occupancy affidavit", "occupancy certification" |
| Prequal Response Form | "ca lock in agreement", "california lock in agreement", "completed submission form", "interest rate lock agreement", "interest rate lock in agreement", "loan pricing worksheet", "loannex product & pricing results", "loannex product and pricing results", "loannex results", "lock confirmation", "new york interest rate lock in agreement", "pricing results", "product & pricing results", "product and pricing results", "rate lock agreement", "rate lock confirmation", "submission form" |
| Purchase Contract | "contract of sale", "purchase agreement", "sales contract" |
| Rental Agreement | "current lease agreement", "lease", "lease agreement", "rental lease" |
| Rental Income Calculations Worksheet | "dscr calculation worksheet", "dscr documentation", "rental income ltr sfr purchase", "rental income ltr sfr refinance" |
| Title Commitment | "preliminary title report", "title report" |
| Trust Documents | "inter vivos trust", "living trust", "revocable trust documents", "trust agreement", "trust certification" |
| Verification Of Deposit | "deposit verification", "vod" |
| **Verification Of Employment** | "verbal verification of employment", "voe", **"verification of income"** ← added (PC-07 / ticket 4275 AC1) |
| Verification Of Mortgage | "mortgage verification", "vom" |
| Verification Of Rent | "rent verification", "vor" |

## Evidence-only aliases (counts as supporting evidence, does NOT merge the two into one request)

| Condition | Task Tile category names accepted as evidence |
|---|---|
| Asset | "bank statement", "bank_statement", "brokerage statement", "investment account statement" |

## Blanket aliases (one match satisfies ALL specs on the request)

| Condition | Task Tile category names accepted |
|---|---|
| Borrower Certification As To Business Purpose | "borrowers authorization" |

## Bolded rows = added for ticket 4275

Four category names were missing entirely before today and have been added:

1. "Verification of Income" → Verification of Employment (loan 510)
2. "Transaction History" → EMD Check (loan 510)
3. "Income Calculations Worksheet" → Non QM Bank Statement Analysis Worksheet (loan 512)
4. "Asset Worksheet" → Asset Depletion Worksheet (loan 513)
5. "4506-C IVES" → IRS 4506-C Authorization (loan 513)

(Items 1–3 were added on 23 Nov 2026 under the PC-07 banner before this
ticket's exact acceptance criteria were reviewed line by line; items 4–5
were found missing during that review and added in the same pass — see
`tests/test_task_tile_category_mapping.py` for proof of all 5 plus the
negative control.)

## Asset-depletion statement linking (AC4, second half)

For an asset-depletion loan, the bank/retirement/brokerage statements
themselves (not just the worksheet) link to the mandatory-floor "Bank
Statement" request via the existing "other asset" alias — Task Tile's own
generic bucket for brokerage/IRA/retirement account statements. This was
already working before today (confirmed in
`TestAC4_AssetWorksheetToAssetDepletionWorksheet::test_statements_link_to_income_condition`)
and required no change.
