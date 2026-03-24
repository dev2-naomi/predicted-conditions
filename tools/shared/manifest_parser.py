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
from datetime import date, timedelta
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


def _parse_date(val: object) -> date | None:
    """Parse a YYYY-MM-DD string into a date, returning None on failure."""
    if not val or not isinstance(val, str):
        return None
    try:
        return date.fromisoformat(val.strip())
    except ValueError:
        return None


def _merge_paystubs(
    paystub_entries: list[dict],
    target_days: int = 30,
) -> list[dict]:
    """
    Combine consecutive paystub entries so that the merged period spans
    at least `target_days`.

    Strategy:
    - Parse payStubPeriodFrom / payStubPeriodTo from each entry's
      extracted_fields.
    - Sort entries chronologically (most recent last).
    - Walk backwards from the most recent stub, accumulating entries
      until the combined span (earliest_from → latest_to) >= target_days
      OR all available stubs are consumed.
    - Merge the selected entries into a single output entry:
        * payStubPeriodFrom  — earliest date in the window
        * payStubPeriodTo    — latest date in the window
        * current_total      — sum of all per-period gross amounts
        * YTD                — from the most recent stub (highest value)
        * hours_total        — sum of hoursWorked across the window
        * pay_periods        — list of individual period summaries (for
                               traceability)
        * all other fields   — taken from the most recent stub
    - Entries that have no parseable dates are returned unchanged.

    The function handles any pay frequency: weekly, bi-weekly,
    semi-monthly, monthly, etc.
    """
    # Separate entries with parseable periods from those without
    dated: list[tuple[date, date, dict]] = []
    undated: list[dict] = []

    for entry in paystub_entries:
        fields = entry.get("extracted_fields", {})
        from_dt = _parse_date(fields.get("payStubPeriodFrom"))
        to_dt = _parse_date(fields.get("payStubPeriodTo"))
        if from_dt and to_dt:
            dated.append((from_dt, to_dt, entry))
        else:
            undated.append(entry)

    if not dated:
        return undated

    # Sort chronologically by period start, then end
    dated.sort(key=lambda x: (x[0], x[1]))

    # Walk backwards from most-recent, accumulating until >= target_days
    selected: list[tuple[date, date, dict]] = []
    for item in reversed(dated):
        selected.append(item)
        earliest = min(x[0] for x in selected)
        latest = max(x[1] for x in selected)
        if (latest - earliest).days + 1 >= target_days:
            break

    # If we have only one stub and it doesn't cover target_days,
    # include all available stubs regardless
    if len(selected) < len(dated):
        earliest = min(x[0] for x in selected)
        latest = max(x[1] for x in selected)
        if (latest - earliest).days + 1 < target_days:
            selected = dated[:]  # use all

    # Re-sort selected oldest→newest so we aggregate correctly
    selected.sort(key=lambda x: (x[0], x[1]))

    # Most-recent stub supplies the "header" fields
    _, _, most_recent_entry = selected[-1]
    most_recent_fields = most_recent_entry.get("extracted_fields", {})

    # Aggregate numeric fields
    current_total = 0.0
    hours_total = 0.0
    ytd = 0.0
    pay_periods: list[dict] = []

    for from_dt, to_dt, entry in selected:
        fields = entry.get("extracted_fields", {})
        try:
            current_total += float(fields.get("current") or 0)
        except (TypeError, ValueError):
            pass
        try:
            hours_total += float(fields.get("hoursWorked") or 0)
        except (TypeError, ValueError):
            pass
        try:
            candidate_ytd = float(fields.get("YTD") or 0)
            if candidate_ytd > ytd:
                ytd = candidate_ytd
        except (TypeError, ValueError):
            pass
        pay_periods.append({
            "from": from_dt.isoformat(),
            "to": to_dt.isoformat(),
            "current": fields.get("current"),
            "hoursWorked": fields.get("hoursWorked"),
        })

    earliest_from = selected[0][0]
    latest_to = selected[-1][1]
    span_days = (latest_to - earliest_from).days + 1

    # Build merged extracted_fields on top of the most-recent stub's fields
    merged_fields = dict(most_recent_fields)
    merged_fields.update({
        "payStubPeriodFrom": earliest_from.isoformat(),
        "payStubPeriodTo": latest_to.isoformat(),
        "periodSpanDays": span_days,
        "current_total": current_total,
        "hours_total": hours_total,
        "YTD": ytd if ytd else most_recent_fields.get("YTD"),
        "pay_periods": pay_periods,
    })

    merged_entry = dict(most_recent_entry)
    merged_entry["extracted_fields"] = merged_fields
    merged_entry["name"] = (
        f"Paystub ({earliest_from.isoformat()} – {latest_to.isoformat()}, "
        f"{len(selected)} period{'s' if len(selected) != 1 else ''}, "
        f"{span_days} days)"
    )

    # Return the single merged entry plus any undated stubs
    return [merged_entry] + undated


def parse_manifest(manifest_path: Union[str, Path]) -> list[dict]:
    """
    Parse a Tasktile manifest JSON file and return a submitted_documents list
    compatible with the pipeline's parse_submitted_documents tool.
    """
    manifest_path = Path(manifest_path)
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)
    return _parse_manifest_dict(manifest)


def parse_manifest_from_string(raw_json: str) -> list[dict]:
    """
    Parse a raw manifest JSON string (for cloud callers that pass the
    manifest as a state field instead of a file path).
    """
    manifest = json.loads(raw_json)
    return _parse_manifest_dict(manifest)


def _parse_manifest_dict(manifest: dict) -> list[dict]:
    """
    Core manifest parsing logic. Accepts an already-parsed dict.

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
    # Collect paystubs separately so they can be merged into a 30-day window.
    result: list[dict] = []
    paystub_entries: list[dict] = []

    for (category_id, _group_name), (doc, _ts) in sorted(
        groups.items(), key=lambda x: (x[0][0], x[0][1])
    ):
        cat = doc.get("category") or {}
        category_name = cat.get("category_name", "unknown")
        metadata = doc.get("metadata") or {}

        doc_type = CATEGORY_ID_TO_DOC_TYPE.get(category_id, "other")
        extracted = _extract_entity_fields(metadata)

        entry = {
            "doc_id": str(category_id),
            "name": category_name,
            "doc_type": doc_type,
            "extracted_fields": extracted,
            "flags": [],
        }

        if doc_type == "paystub":
            paystub_entries.append(entry)
        else:
            result.append(entry)

    # Merge paystubs into a combined 30-day window entry
    if paystub_entries:
        result.extend(_merge_paystubs(paystub_entries, target_days=30))

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
