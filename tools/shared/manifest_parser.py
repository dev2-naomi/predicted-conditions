"""
manifest_parser.py — Transforms a Tasktile manifest JSON into the
submitted_documents_json format expected by the pipeline's
parse_submitted_documents tool.

De-duplication strategy:
  - Group documents by (category_id, group_name). Documents with the
    same category AND group_name represent the same physical document
    extracted in multiple processing passes.
  - Within each group, keep the document whose indexing task has the
    latest end_time (most recent / most accurate extraction).
  - Across groups within the same category, keep all entries — they
    represent distinct physical documents (e.g., two different paystubs
    for different pay periods).

Output format per document:
  {
      "doc_id": "117",               # str(category_id)
      "name":   "Credit Report",     # category_name
      "doc_type": "credit_report",   # from CATEGORY_ID_TO_DOC_TYPE
      "extracted_fields": { ... },   # entity metadata from indexing
      "flags": [],
  }
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Union


# ---------------------------------------------------------------------------
# Keys in doc.metadata that are housekeeping / not extracted entity data
# ---------------------------------------------------------------------------

NON_ENTITY_META_KEYS: frozenset[str] = frozenset({
    "category",
    "confidence",
    "exceptions",
    "group_index",
    "group_name",
    "object_name",
    "total_pages",
    "vision_check",
})


# ---------------------------------------------------------------------------
# Authoritative category_id → doc_type mapping
# Bypasses the fuzzy name-matching in _map_doc_name_to_type() entirely.
# Categories not listed here fall back to "other".
# ---------------------------------------------------------------------------

CATEGORY_ID_TO_DOC_TYPE: dict[int, str] = {
    # Income
    16:   "paystub",
    363:  "income_worksheet",
    436:  "VOE",
    449:  "VOR",
    2103: "1099",

    # Assets / Bank
    502:  "bank_statement",
    1080: "emd",
    210:  "other",          # Wire Transfer

    # Credit
    117:  "credit_report",
    119:  "credit_report",  # Credit Supplement
    324:  "other",          # DU Underwriting Findings

    # Property / Appraisal
    162:  "appraisal",
    957:  "appraisal",      # Appraisal Update
    1472: "appraisal",      # Appraisal Review
    468:  "appraisal",      # Automated Valuation Model (AVM)
    687:  "other",          # UCDP SSR
    683:  "other",          # Disaster Search Results
    356:  "other",          # Zip Code Lookup
    1479: "insurance",      # Property Insurance
    1481: "tax_record",     # Property Tax
    538:  "insurance",      # Flood Certification
    1800: "insurance",      # Flood Hazard Determination
    993:  "other",          # Replacement Cost Estimate

    # Title / Closing
    171:  "title_commitment",   # Deed of Trust
    180:  "title_commitment",   # Grant Deed
    194:  "title_commitment",   # Preliminary Report
    1484: "mortgage_note",      # Mortgage Note
    1508: "other",              # Other Title Related Documentation
    200:  "purchase_contract",
    169:  "purchase_contract",  # Counteroffer
    819:  "closing_disclosure",
    165:  "other",              # Closing Costs
    166:  "other",              # Closing Instructions
    167:  "other",              # Closing Protection Letter
    369:  "other",              # Escrow Instructions
    523:  "other",              # Escrow Fee Sheet
    507:  "other",              # Wiring Instructions
    556:  "other",              # MERS
    245:  "other",              # Fees Worksheet
    312:  "other",              # Amortization Schedule
    140:  "mortgage_statement",

    # Compliance / Identity / Loan App
    349:  "loan_application",   # URLA 1003
    351:  "other",              # Transmittal Summary 1008
    323:  "ID",                 # Drivers License
    280:  "affidavit",          # Signature Affidavit (NOT ID)
    1106: "affidavit",          # Affidavit of Occupancy
    390:  "compliance_notice",  # Owner Occupancy Certification
    237:  "compliance_notice",  # Borrowers Authorization
    1849: "compliance_notice",  # SSA 89
    2100: "compliance_notice",  # IRS 4506-C
    1096: "compliance_notice",  # Compliance Report
    352:  "other",              # Underwriting Decision
    1118: "other",              # Fraud Report
    2105: "other",              # Exception Request Form
    2176: "other",              # CTC Checklist
    363:  "other",              # Income Calculations Worksheet

    # HOA / Condo
    # (no condo questionnaire in this manifest)

    # Admin / disclosures — all "other" so they route to crosscutting
    # but do not create noise in substantive facets
    28:   "other",    # Email Trail
    38:   "other",    # Fax Cover Sheet
    33:   "other",    # Appraisal Invoice
    34:   "other",    # Appraisal Payment
    231:  "other",    # Affiliated Business Arrangement Disclosure
    241:  "other",    # Disclosure Notices
    242:  "other",    # ECOA
    244:  "other",    # Fair Lending Notice
    254:  "other",    # Initial Escrow Account Disclosure
    275:  "other",    # Privacy Policy Disclosure
    277:  "other",    # Right to Receive Appraisal
    279:  "other",    # Servicing Disclosure
    283:  "other",    # Tax Request W9
    339:  "other",    # Lock Confirmation
    348:  "other",    # Shipment Label
    387:  "other",    # Loan Info LOE
    458:  "other",    # Credit Invoice
    473:  "other",    # Appraisal Valuation Acknowledgement
    521:  "other",    # Settlement Service Provider List
    575:  "other",    # Waiver — Three Day Appraisal
    593:  "other",    # Hazard Insurance Disclosure
    686:  "other",    # Misc Invoices
    809:  "other",    # Other Disclosures
    830:  "other",    # Other Loan Info
    866:  "other",    # Per Diem Interest Charge Disclosure
    877:  "other",    # Anti Coercion Statement
    905:  "other",    # Machine Copies Notice
    984:  "other",    # Loan Estimate
    1019: "other",    # Acknowledgement of Receipt of Loan Estimate
    1020: "other",    # Homeownership Counseling Org List ack
    1021: "other",    # Appraisal Report for Lenders Use Disclosure
    1022: "other",    # Loan Impound Disclosure
    1025: "other",    # Mortgage Fraud FBI notice
    1026: "other",    # Notice of Furnishing Negative Information
    1027: "other",    # Voluntary Info for Gov Monitoring
    1028: "other",    # Acknowledgement of Intent to Proceed
    1029: "other",    # Title Insurance Notice
    1038: "other",    # Homeownership Counseling Org List
    1081: "other",    # Fair Credit Report Act Disclosure
    1088: "other",    # NMLS
    1099: "other",    # Your Home Loan Toolkit
    1107: "other",    # Disbursement Instructions
    1108: "other",    # First Payment Letter
    1110: "other",    # Data Entry Proof Sheet
    1111: "other",    # Hazard Insurance Endorsement Letter
    1113: "other",    # Tax Record Information Sheet
    1114: "other",    # Printer Settings Document
    1115: "other",    # Privacy Policy VT and CA
    1124: "other",    # CA Statement of Interest Addendum
    1125: "other",    # CA Mortgage Broker Agreement
    1127: "other",    # CA Addendum to Loan Application
    1461: "other",    # Credit Score Disclosure
    1463: "other",    # CA Notice to Home Loan Applicant
    1473: "other",    # Insurance for E&O
    1475: "other",    # CA Finance Lenders Law Statement
    1509: "other",    # Other Appraisal Related Documentation
    1531: "other",    # Notice of Right to Copy of Appraisal
    1753: "other",    # CA Hazard Insurance Disclosure
    1754: "other",    # CA Notice Right to Receive Appraisal
    1766: "other",    # Patriot Act Information Disclosure
    1768: "other",    # Consent to Receive Calls/Texts
    1796: "other",    # Compliance Agreement
    1864: "other",    # CA Impound Account Statement
    1961: "other",    # Receipt of Your Home Loan Toolkit
    1986: "other",    # CA Lock In Agreement
    2012: "other",    # CA Importance of Owner Title Insurance
    2050: "other",    # Correction Agreement Limited POA
    2083: "other",    # Wire Fraud Warning
    2098: "other",    # Transaction History
    2140: "other",    # ECOA Notice
    2142: "other",    # Certified Report Delivery Confirmation
    2144: "other",    # Acknowledgement of Receipt of Closing Disclosure
    2148: "other",    # Automatic Payment Authorization
    2164: "other",    # Supplemental Consumer Information Form 1103
    2165: "other",    # Prequal Response Form
    2170: "other",    # Ability to Repay Notice
    2177: "other",    # Notice to Borrowers About Language
    2185: "other",    # LOE for HOA Dues
    2312: "other",    # Attention Closing Agents
}


def _build_task_index(tasks: list[dict]) -> dict[str, int]:
    """
    Returns {document_id: latest_indexing_end_time_ms} for all docs
    that have at least one completed indexing task.
    Falls back to any task end_time if no indexing task exists.
    """
    indexing: dict[str, int] = {}
    fallback: dict[str, int] = {}

    for task in tasks:
        doc_id = task.get("document_id")
        if not doc_id:
            continue
        end_time_raw = task.get("end_time")
        if not end_time_raw:
            continue
        try:
            end_time = int(end_time_raw)
        except (ValueError, TypeError):
            continue

        if task.get("type") == "indexing":
            if end_time > indexing.get(doc_id, 0):
                indexing[doc_id] = end_time
        else:
            if end_time > fallback.get(doc_id, 0):
                fallback[doc_id] = end_time

    # Merge: indexing wins; use fallback only when no indexing task found
    result = dict(fallback)
    result.update(indexing)
    return result


def _extract_entity_fields(metadata: dict) -> dict:
    """Strip housekeeping keys from metadata, return remaining entity data."""
    return {k: v for k, v in metadata.items() if k not in NON_ENTITY_META_KEYS}


def parse_manifest(manifest_path: Union[str, Path]) -> list[dict]:
    """
    Parse a Tasktile manifest JSON and return a submitted_documents list
    compatible with the pipeline's parse_submitted_documents tool.

    Each entry in the returned list:
        {
            "doc_id":          str,   # str(category_id)
            "name":            str,   # category_name
            "doc_type":        str,   # from CATEGORY_ID_TO_DOC_TYPE
            "extracted_fields": dict, # entity metadata from indexing
            "flags":           list,  # always [] (no flag data in manifest)
        }

    De-duplication: for each (category_id, group_name) pair, the doc with
    the latest indexing task end_time is kept.  Distinct group_names within
    the same category represent separate physical documents (e.g., two
    different paystubs) and are both included.
    """
    manifest_path = Path(manifest_path)
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)

    tasks: list[dict] = manifest.get("job", {}).get("tasks", [])
    documents: list[dict] = manifest.get("documents", [])

    task_index = _build_task_index(tasks)

    # Group documents by (category_id, group_name)
    # key → (doc dict, latest_ts)
    groups: dict[tuple, tuple[dict, int]] = {}

    for doc in documents:
        cat = doc.get("category") or {}
        category_id = cat.get("category_id")
        if category_id is None:
            continue

        metadata = doc.get("metadata") or {}
        group_name = str(metadata.get("group_name", ""))

        key = (category_id, group_name)
        doc_id = doc.get("id", "")
        ts = task_index.get(doc_id, 0)

        existing = groups.get(key)
        if existing is None or ts > existing[1]:
            groups[key] = (doc, ts)

    # Build output — one entry per unique (category_id, group_name)
    result: list[dict] = []
    for (category_id, _group_name), (doc, _ts) in sorted(
        groups.items(), key=lambda x: (x[0][0], x[0][1])
    ):
        cat = doc.get("category") or {}
        category_name = cat.get("category_name", "unknown")
        metadata = doc.get("metadata") or {}

        doc_type = CATEGORY_ID_TO_DOC_TYPE.get(category_id, "other")
        extracted = _extract_entity_fields(metadata)

        result.append({
            "doc_id": str(category_id),
            "name": category_name,
            "doc_type": doc_type,
            "extracted_fields": extracted,
            "flags": [],
        })

    return result


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="Transform a Tasktile manifest JSON into submitted_documents_json format."
    )
    parser.add_argument("manifest", help="Path to the manifest JSON file")
    parser.add_argument(
        "--output", "-o", default="-",
        help="Output file path (default: stdout)"
    )
    parser.add_argument(
        "--pretty", action="store_true", default=True,
        help="Pretty-print JSON output (default: true)"
    )
    args = parser.parse_args()

    docs = parse_manifest(args.manifest)
    indent = 2 if args.pretty else None
    out_json = json.dumps(docs, indent=indent, default=str)

    if args.output == "-":
        sys.stdout.write(out_json + "\n")
    else:
        Path(args.output).write_text(out_json + "\n", encoding="utf-8")
        print(f"Wrote {len(docs)} documents to {args.output}", file=sys.stderr)
