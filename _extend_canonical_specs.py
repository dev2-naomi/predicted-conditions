"""One-time script: extend data/canonical_doc_specs.json with the 24
previously-not-standardized document types identified by
generate_conditions_excel.py's Overview tab.

Content is grounded either in the actual NQMF guidelines.md text (via
tools/guideline_reader.py — same source-of-truth approach as
_guideline_spec_extraction.py) for the 13 types that have a matching
guideline section, in the existing hand-curated deterministic content
already living in tools/doc_rules.py's _eligibility_category_doc_map()/
mandatory_docs() for the types that were already deterministic via that
separate mechanism (registering them here too closes the gap where they'd
still drift to LLM-freeform whenever NOT flagged by the eligibility engine
— e.g. Loan Application (1003) showed only 54% spec consistency despite
having a doc_rules.py template, because that template only fires when the
eligibility engine's required_doc_categories flags it), or hand-curated
(mirroring the existing UCDP SSR / Payoff Statement / Deed of Trust
treatment) for the remaining closing-mechanics/entity types with no
guidelines.md section at all.

Run once: python3 _extend_canonical_specs.py
"""
import json

PATH = "data/canonical_doc_specs.json"

NEW_ENTRIES: dict[str, dict] = {
    "1099": {
        "source_headings": ["IRS FORM 1099 (ALT DOC)"],
        "items": [
            {"text": "1099 income documentation is limited to individual borrowers being paid via a 1099 who are not the business owner of the entity issuing the 1099", "condition": None},
            {"text": "Self-employed borrowers who are business owners paying their own earnings via a 1099 are not eligible to use this income documentation type", "condition": None},
            {"text": "If the most recent 1099 is more than 90 days from the Note Date, one of the following must be provided: evidence of year-to-date earnings via YTD bank statements, a printout of YTD wages from the employer(s), or the lender's Verification of Employment (or similar form) with YTD income completed by each employer", "condition": None},
            {"text": "If the 1099 is paid to the borrower's business rather than the borrower individually, an Expense Letter from the borrower's CPA/EA/PTIN-licensed tax preparer is required to determine the qualifying income", "condition": None},
            {"text": "A 10% automatic expense ratio applies when the 1099 is paid to the borrower individually", "condition": None},
            {"text": "Must match the income source and amount reported on the loan application", "condition": "loan_application_submitted=true"},
        ],
    },
    "consolidated 1099": {
        "source_headings": ["ASSETS", "ASSET DOCUMENTATION"],
        "items": [
            {"text": "Must show the account holder name matching the borrower", "condition": None},
            {"text": "Must show the financial institution/brokerage name and account number", "condition": None},
            {"text": "Must be dated within 90 days of the note date", "condition": None},
            {"text": "All pages of the statement are required; summaries will not be accepted", "condition": None},
            {"text": "Large deposits, defined as > 50% of the total gross income for all borrowers, must be documented on Full and Alt Doc purchase transactions when personal accounts are used", "condition": "loan_purpose=purchase"},
            {"text": "Large deposits are not required to be sourced on refinances or DSCR transactions", "condition": "loan_purpose=refinance OR program=dscr"},
        ],
    },
    "asset": {
        "source_headings": ["ASSETS", "ASSET DOCUMENTATION"],
        "items": [
            {"text": "Loan file must evidence sufficient funds from an acceptable source for down payment, closing costs, prepaid items, debt payoff, and applicable reserves", "condition": None},
            {"text": "Assets must be dated within 90 days of the note date", "condition": None},
            {"text": "Documentation must include a one-month (or most recent quarterly) account statement showing opening and closing balances, borrower listed as account holder, account number, statement date/period, and current balance in U.S. dollars — or a Written Verification of Deposit completed by the financial institution — or verification via an approved third-party vendor", "condition": None},
            {"text": "Non-borrowing parties on the account (excluding a non-borrowing spouse) must provide a written statement that the borrower has full access and use of the funds", "condition": None},
            {"text": "Large deposits, defined as > 50% of total gross income for all borrowers, must be documented on Full and Alt Doc purchase transactions when personal accounts are used", "condition": "loan_purpose=purchase"},
            {"text": "Large deposits are not required to be sourced on business accounts when used for assets", "condition": None},
            {"text": "Large deposits are not required to be sourced on refinances or DSCR transactions", "condition": "loan_purpose=refinance OR program=dscr"},
            {"text": "All pages of any statement provided are required; summaries will not be accepted", "condition": None},
        ],
    },
    "asset depletion worksheet": {
        "source_headings": ["ASSET UTILIZATION (ALT DOC)", "QUALIFIED ASSETS", "NET QUALIFIED ASSET CALCULATION"],
        "items": [
            {"text": "Qualified assets can be comprised of publicly traded stocks, bonds, mutual funds, the vested amount of retirement accounts, bank or investment accounts, and crypto-currency", "condition": None},
            {"text": "Three months' seasoning of all assets is required (three most recent and consecutive statements)", "condition": None},
            {"text": "All individuals listed on the asset account must be on the note and mortgage", "condition": None},
            {"text": "Assets held in the name of a business are not eligible to be used for the asset utilization calculation", "condition": None},
            {"text": "Asset statements utilized for asset utilization cannot be used to document any other income source", "condition": None},
            {"text": "Assets held in a revocable trust are eligible only if the borrower is the trustee; assets held in an irrevocable trust are eligible only if the borrower is the beneficiary with immediate access to the trust assets", "condition": None},
            {"text": "Qualified asset calculation: 100% of checking/savings/money market, 80% of publicly traded stocks & bonds, 70% of vested retirement accounts for borrowers 59½ or older (60% if under 59½), 100% of the pre-maturity surrender value of CDs (minus early-withdrawal penalty), and 60% of crypto funds based on current Coinbase valuation", "condition": None},
            {"text": "Ineligible asset sources: business assets, unseasoned foreign assets, 529 college savings plans, proceeds from sale of real estate not seasoned 3 months, privately traded/restricted/non-vested stock, and assets that produce income already included in the income calculation", "condition": None},
            {"text": "Net qualified assets = qualified assets less funds used for down payment, closing costs, and prepaids (when used as the sole source of income) or less funds used for down payment, closing costs, prepaids AND reserves (when used as supplemental income)", "condition": None},
            {"text": "Depletion calculation methodology and resulting monthly income must be documented on the worksheet", "condition": None},
        ],
    },
    "balance sheet": {
        "source_headings": [],
        "items": [
            {"text": "Must show assets, liabilities, and equity for the business", "condition": None},
            {"text": "Must be current as of a date within the required reporting period (typically the most recent month-end)", "condition": None},
            {"text": "Must be signed and dated by the borrower (or the business's accountant/preparer)", "condition": None},
            {"text": "Must show the business name consistent with the business name on the borrower's tax returns and loan application", "condition": None},
            {"text": "Should reconcile to the business's most recent business tax return (e.g. Schedule L) where a matching tax return is on file", "condition": None},
            {"text": "Must support the business's financial position and continuity as represented on the loan application", "condition": "income_type=self_employed"},
        ],
    },
    "borrower authorization": {
        "source_headings": [],
        "items": [
            {"text": "Signed authorization for the lender to verify credit, employment, income, and asset information", "condition": None},
            {"text": "Signed and dated by all borrowers", "condition": None},
        ],
    },
    "borrower authorization to sign": {
        "source_headings": [],
        "items": [
            {"text": "Must identify the individual being authorized to sign loan and/or closing documents", "condition": None},
            {"text": "Must identify the borrower or entity on whose behalf the individual is authorized to sign", "condition": None},
            {"text": "Must be signed and dated by the borrower (or an authorized officer/manager of the entity) granting the authorization", "condition": None},
            {"text": "Must specify the scope of documents/transactions the authorization covers", "condition": None},
        ],
    },
    "business license": {
        "source_headings": [],
        "items": [
            {"text": "Current, unexpired business license (or equivalent registration) for the borrower's self-employed business", "condition": None},
            {"text": "Business name matches the business name on the loan application", "condition": None},
        ],
    },
    "certificate of good standing": {
        "source_headings": [],
        "items": [
            {"text": "Must be issued by the Secretary of State (or equivalent state authority) for the state in which the entity was formed and/or the state in which the subject property is located", "condition": None},
            {"text": "Must confirm the entity is currently active and in good standing", "condition": None},
            {"text": "Must be dated within 90 days of closing", "condition": None},
            {"text": "Entity name must match the entity name on the loan application, vesting documents, and title", "condition": None},
        ],
    },
    "emd check": {
        "source_headings": ["EARNEST MONEY/CASH DEPOSIT ON SALES CONTRACT"],
        "items": [
            {"text": "Must be an acceptable source of funds: copy of the borrower's canceled check, certification from the deposit holder acknowledging receipt of funds, or a VOD/bank statement showing sufficient average balance to cover the deposit at the time it was made", "condition": None},
            {"text": "If the earnest money check has cleared the bank, bank statements must cover the period up to and including the date the check cleared", "condition": None},
            {"text": "If the check has not yet cleared, a copy of the check may be obtained along with a processor's certification verifying the date it cleared, the dollar amount, and the individual who provided the information", "condition": None},
            {"text": "If funds were given more than 12 months ago per the sales contract, an escrow letter will suffice for sourcing", "condition": None},
            {"text": "Amount must match the earnest money deposit disclosed on the purchase contract", "condition": None},
        ],
    },
    "federal tax id number": {
        "source_headings": [],
        "items": [
            {"text": "Must show the Employer Identification Number (EIN) issued by the IRS for the borrower's business entity", "condition": None},
            {"text": "Entity name on the EIN documentation must match the entity name on the loan application and vesting documents", "condition": None},
            {"text": "Must be an IRS-issued document (e.g. CP 575 notice, EIN confirmation letter, or SS-4 confirmation) rather than a self-reported number alone", "condition": None},
        ],
    },
    "form 1040": {
        "source_headings": [],
        "items": [
            {"text": "Must be the complete IRS Form 1040 for the required tax year(s), including all filed schedules (e.g. Schedule C, Schedule E, Schedule 1, Schedule 2, Schedule 3) relevant to the borrower's income sources", "condition": None},
            {"text": "Must be signed and dated by the borrower, or include an electronic filing confirmation/IRS transcript in lieu of signature", "condition": None},
            {"text": "Borrower name on the return must match the borrower name on the loan application", "condition": None},
            {"text": "Must include Schedule C if self-employment income is reported", "condition": "income_type=self_employed"},
            {"text": "Must include Schedule E if rental income is reported", "condition": None},
            {"text": "Tax returns must be validated against IRS 4506-C transcripts where required by the program", "condition": None},
            {"text": "Two years of returns are required unless the program guidelines specifically permit one year", "condition": None},
        ],
    },
    "income loe": {
        "source_headings": ["DECLINING INCOME"],
        "items": [
            {"text": "Required when income shows a consistent decline over the prior years — declining income should not be considered stable or usable for qualification without further review", "condition": None},
            {"text": "Must be a signed, written explanation for the decline obtained from the borrower and/or employer", "condition": None},
            {"text": "Must address whether the income is stable and expected to continue", "condition": None},
            {"text": "When there is sufficient information to support use of the income despite the decline, the most recent (lower) income over the prior 2-year period must be used and may not be averaged with the higher prior-year figure", "condition": None},
            {"text": "Underwriter must determine whether the income is stable enough to use for qualification based on the explanation and supporting documentation provided", "condition": None},
        ],
    },
    "loan application (1003)": {
        "source_headings": [],
        "items": [
            {"text": "Final signed and dated URLA (Form 1003) for all borrowers", "condition": None},
            {"text": "All sections complete and consistent with the loan terms and program", "condition": None},
        ],
    },
    "investment account statement": {
        "source_headings": ["ASSETS", "ASSET DOCUMENTATION", "QUALIFIED ASSETS"],
        "items": [
            {"text": "Must show the account holder name matching the borrower", "condition": None},
            {"text": "Must show the financial institution name and account number", "condition": None},
            {"text": "Must show the current market value/balance of holdings and the statement date/period covered", "condition": None},
            {"text": "Must be the most recent statement, dated within 90 days of the note date", "condition": None},
            {"text": "All pages of the statement are required; summaries will not be accepted", "condition": None},
            {"text": "If assets are being used for asset utilization/depletion, three months' seasoning (three most recent consecutive statements) is required", "condition": None},
            {"text": "Assets held in the name of a business are not eligible for asset utilization calculations", "condition": None},
        ],
    },
    "loe for hoa dues": {
        "source_headings": [],
        "items": [
            {"text": "Required only if the subject property is in an HOA, condominium, or PUD", "condition": None},
            {"text": "Must show current HOA/condo association dues amount and payment status", "condition": None},
            {"text": "Must show HOA/management company contact information", "condition": None},
            {"text": "Must be dated within 30 days of closing", "condition": None},
            {"text": "Must disclose any outstanding assessments, liens, or special assessments", "condition": None},
            {"text": "Must identify the subject property address", "condition": None},
        ],
    },
    "llc member list": {
        "source_headings": [],
        "items": [
            {"text": "Must list all members/owners of the LLC and their respective ownership percentages", "condition": None},
            {"text": "Must identify the manager(s) or managing member(s) authorized to act on behalf of the LLC", "condition": None},
            {"text": "Ownership percentages must be consistent with the entity's Operating Agreement and, where applicable, the borrower's tax returns/K-1", "condition": None},
            {"text": "Must be dated and, where required, certified/signed by an authorized officer or manager of the LLC", "condition": None},
        ],
    },
    "loannex product & pricing results": {
        "source_headings": [],
        "items": [
            {"text": (
                "LoanNex product and pricing results matching the loan program, rate, and price "
                "reflected in the loan file — or, if LoanNex results are unavailable, the completed "
                "Submission Form (Program, Loan Purpose, Loan Amount, Appraised Value, Purchase Price, "
                "Occupancy, Property Type, Product Type, Term, Interest Rate, Interest Only, 2/1 Buydown, "
                "and Prepayment Penalty selections), or a signed Rate Lock Confirmation / Interest Rate "
                "Lock In Agreement confirming the locked rate and terms"
            ), "condition": None},
        ],
    },
    "ownership interest certification": {
        "source_headings": [],
        "items": [
            {"text": "Certifies borrower's percentage of ownership interest in the business", "condition": None},
            {"text": "Signed and dated by the borrower", "condition": None},
            {"text": "Ownership percentage consistent with tax returns/K-1", "condition": None},
        ],
    },
    "title invoice": {
        "source_headings": [],
        "items": [
            {"text": "Itemized title/closing fees from the title company", "condition": None},
            {"text": "Matches fees disclosed on the Closing Disclosure/Loan Estimate", "condition": None},
        ],
    },
    "trust documents": {
        "source_headings": ["INTER VIVOS REVOCABLE TRUST", "TRUST ACCOUNTS", "TRUST INCOME"],
        "items": [
            {"text": "A copy of the trust is required, or a signed attorney's opinion letter may be obtained in lieu of the trust documents", "condition": None},
            {"text": "The attorney's opinion letter (if used in lieu of the trust) must include: name of the trust, date executed, settler(s) of the trust, whether it is revocable or irrevocable, whether the trust has multiple trustees, name of the trustees, and the manner in which vesting will be held", "condition": None},
            {"text": "The primary beneficiary of the trust must be the individual(s) who established the trust", "condition": None},
            {"text": "The trustee must be either the individual establishing the trust (or at least one of them) or an institutional trustee authorized to act as trustee under applicable state law", "condition": None},
            {"text": "The trust must specify that the trustee has the power to hold title and mortgage the property", "condition": None},
            {"text": "A Power of Attorney is not eligible on loans when title is vested in a trust", "condition": None},
            {"text": "If trust funds are being used for down payment/closing costs/reserves, written documentation of the trust account's value from the trust manager or trustee is required, along with the conditions under which the borrower has access to the funds", "condition": None},
            {"text": "If trust income is being used to qualify: variable trust payments require a 24-month history of receipt documented with tax returns; fixed trust payments require at least one payment received prior to closing (provided the borrower is not the grantor) plus a current bank statement or equivalent proof of receipt", "condition": None},
        ],
    },
    "verification of income": {
        "source_headings": [],
        "items": [
            {"text": "Must independently confirm the borrower's reported income source, amount, and frequency", "condition": None},
            {"text": "Must be from an independent, verifiable third-party source (employer, CPA/tax preparer, or other qualified verifier) rather than borrower self-certification alone", "condition": None},
            {"text": "Income amount and source must be consistent with what is reported on the loan application and used in qualification", "condition": None},
            {"text": "Must be dated/current per the applicable documentation-recency requirements for the loan's income documentation type", "condition": None},
        ],
    },
    "verification of rent": {
        "source_headings": ["HOUSING HISTORY", "HOUSING HISTORY VERIFICATION (NON – DSCR)"],
        "items": [
            {"text": "Must be an institutional Verification of Rent (VOR) from the landlord or property management company, unless the borrower pays an individual/interested party", "condition": None},
            {"text": "Must cover a combined total of the most recent 12 months of rental payment history", "condition": None},
            {"text": "Rolling delinquent payments are not considered a single event — each occurrence of a contractual delinquency is considered individually for loan eligibility", "condition": None},
            {"text": "If the borrower pays an individual/interested party, one of the following is required: a VOR plus the most recent 6 consecutive months of canceled checks/bank statements/Venmo-PayPal documentation, OR a copy of the note/lease plus the most recent 12 consecutive months of canceled checks/bank statements/Venmo-PayPal documentation", "condition": None},
            {"text": "Must demonstrate a paid-as-agreed history, unless a past-due status requires updated documentation to verify the account is now current", "condition": None},
            {"text": "Properties held in the name of an LLC in which the borrower is personally obligated on the note must have the rental payment history documented", "condition": None},
            {"text": "Borrowers living rent-free must provide a rent-free letter from the property owner instead of a VOR", "condition": None},
            {"text": "Must identify the borrower and the rental property address", "condition": None},
            {"text": "Where DU waives the rental-history requirement, NQMF still requires documentation to support a 12-month housing history, though cancelled checks may be waived for eligible private-party VORs per DU", "condition": None},
        ],
    },
}


def main() -> None:
    with open(PATH) as f:
        data = json.load(f)

    added, updated = [], []
    for key, entry in NEW_ENTRIES.items():
        if key in data:
            updated.append(key)
        else:
            added.append(key)
        data[key] = entry

    with open(PATH, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")

    print(f"Added {len(added)} new doc types: {added}")
    if updated:
        print(f"Updated {len(updated)} existing doc types: {updated}")
    print(f"Total doc types in {PATH}: {len(data)}")


if __name__ == "__main__":
    main()
