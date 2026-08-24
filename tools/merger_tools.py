"""
merger_tools.py — Tools for STEP_08: Merger, De-Duper, Ranker (v2 Document-Centric).

Four tools:
  1. merge_document_requests      — collect & merge doc requests from modules 01-07
  2. rank_document_requests       — sort by severity/priority/category and assign status
  3. cross_check_satisfaction     — compare specs against submitted doc contents
  4. generate_final_output        — assemble the final output JSON with stats
"""

from __future__ import annotations

import re
from typing import Any

from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command
from typing_extensions import Annotated

from tools.shared.normalize import (
    apply_output_display_name,
    normalize_all,
    normalize_document_structure,
)


# ---------------------------------------------------------------------------
# Document-type alias map
# ---------------------------------------------------------------------------
# Two maps control how the engine matches requested documents against the
# manifest:
#
# _DOCTYPE_BLANKET_ALIASES — functionally equivalent documents.  When a
#   blanket alias matches, ALL specs are marked satisfied (the submitted doc
#   is a different document that fully covers the requirement).
#
# _DOCTYPE_ALIASES — naming variants of the same document.  When a name
#   variant matches, the normal extracted-fields check still runs so only
#   specs actually confirmed by extracted data are satisfied.

_DOCTYPE_BLANKET_ALIASES: dict[str, set[str]] = {
    "borrower certification as to business purpose": {
        "borrowers authorization",
    },
}

_DOCTYPE_ALIASES: dict[str, set[str]] = {
    "loan application (1003)": {
        "urla 1003", "urla", "1003", "loan application", "loan_application",
        "uniform residential loan application",
    },
    "personal bank statements": {
        "bank statement",
    },
    "bank statement": {
        "personal bank statements",
    },
    "hazard insurance": {
        "homeowners insurance", "property insurance", "insurance binder",
    },
    "government-issued photo id": {
        "drivers license", "passport", "photo id", "identification",
    },
    "irs 4506-c authorization": {
        "4506-c", "4506c", "irs form 4506-c", "tax transcript authorization",
    },
    "purchase contract": {
        "sales contract", "purchase agreement", "contract of sale",
    },
    "rental agreement": {
        "lease agreement", "lease", "rental lease",
    },
    "appraisal report": {
        "appraisal report (urar)", "appraisal",
    },
    "credit report": {
        "tri-merge credit report", "credit report (rmcr)",
    },
    "verification of employment": {
        "voe", "verbal verification of employment",
    },
    "verification of mortgage": {
        "vom", "mortgage verification",
    },
    "verification of deposit": {
        "vod", "deposit verification",
    },
    "verification of rent": {
        "vor", "rent verification",
    },
    "rental income calculations worksheet": {
        "dscr calculation worksheet", "dscr documentation",
    },
    "flood hazard determination": {
        "flood certification", "flood determination",
    },
    "owner occupancy certification": {
        "occupancy affidavit", "occupancy certification",
    },
    "title commitment": {
        "title report", "preliminary title report",
    },
}


def _get_aliases(doc_type: str) -> set[str]:
    """Return all names (including the original) that count as the same doc."""
    key = doc_type.strip().lower()
    aliases = {key, key.replace(" ", "_"), key.replace("_", " ")}
    for alias_map in (_DOCTYPE_ALIASES, _DOCTYPE_BLANKET_ALIASES):
        for mapped in alias_map.get(key, set()):
            m = mapped.strip().lower()
            aliases.update({m, m.replace(" ", "_"), m.replace("_", " ")})
    return aliases


def _is_blanket_alias(doc_type: str, sdoc_name: str) -> bool:
    """Return True if the match is a blanket alias (satisfy all specs)."""
    key = doc_type.strip().lower()
    blanket_names = _DOCTYPE_BLANKET_ALIASES.get(key, set())
    return sdoc_name.strip().lower() in blanket_names


# ---------------------------------------------------------------------------
# Ranking helpers
# ---------------------------------------------------------------------------

_SEVERITY_RANK = {"HARD-STOP": 0, "SOFT-STOP": 1}
_PRIORITY_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
_CATEGORY_RANK = {
    "Program Eligibility": 0, "Cross-Cutting": 0,
    "Compliance": 1, "Credit": 2, "Income": 3,
    "Assets": 4, "Property": 5, "Appraisal": 5,
    "Title": 6, "Closing": 6, "Other": 7,
}


def _severity_val(s: str) -> int:
    return _SEVERITY_RANK.get(s, 1)


def _priority_val(p: str) -> int:
    return _PRIORITY_RANK.get(p, 3)


def _category_val(c: str) -> int:
    return _CATEGORY_RANK.get(c, 7)


def _sort_key(dr: dict) -> tuple:
    return (
        _severity_val(dr.get("severity", "SOFT-STOP")),
        _priority_val(dr.get("priority", "P3")),
        _category_val(dr.get("document_category", "Other")),
    )


# ---------------------------------------------------------------------------
# Merge-key helpers
# ---------------------------------------------------------------------------

_CANONICAL_NAMES: dict[str, str] = {
    "executed lease agreement": "lease agreement",
    "executed lease": "lease agreement",
    "lease": "lease agreement",
    "rent loss insurance evidence": "rent loss insurance",
    "evidence of rent loss insurance": "rent loss insurance",
    "hazard insurance declaration page": "hazard insurance",
    "hazard insurance": "hazard insurance",
    "property insurance": "hazard insurance",
    "property insurance / hazard insurance": "hazard insurance",
    "flood determination / flood certificate": "flood determination",
    "flood certificate": "flood determination",
    "flood cert": "flood determination",
    "mortgage payment history": "mortgage payment history",
    "verification of mortgage": "verification of mortgage",
    "verification of mortgage (vom)": "verification of mortgage",
    "vom": "verification of mortgage",
    "verification of rent": "verification of rent",
    "verification of rent (vor)": "verification of rent",
    "vor": "verification of rent",
    "primary residence verification": "primary residence verification",
    "proof of primary residence ownership": "primary residence verification",
    "proof of primary residence": "primary residence verification",
    "rent loss insurance": "rent loss insurance",
    "rent loss insurance evidence": "rent loss insurance",
    "government-issued photo id": "government id",
    "government id": "government id",
    "drivers license": "government id",
    "passport": "government id",
    "borrower authorization form": "borrower authorization",
    "borrower authorization": "borrower authorization",
    "occupancy certification / investor certification": "occupancy certification",
    "occupancy certification": "occupancy certification",
    "investor certification": "occupancy certification",
}


# Reverse map built from _DOCTYPE_ALIASES so that naming variants
# (e.g. "verbal verification of employment" → "verification of employment",
# "flood certification" → "flood hazard determination") collapse to the same
# merge key.  This is the single source of truth for "same document, different
# name" and prevents the run-to-run naming drift seen in variance testing.
_ALIAS_TO_CANONICAL: dict[str, str] = {}
for _canon, _variants in _DOCTYPE_ALIASES.items():
    for _v in _variants:
        _ALIAS_TO_CANONICAL[_v.strip().lower()] = _canon


def _canonical_doc_type(name: str) -> str:
    key = name.strip().lower()
    # 1) explicit canonical-name overrides
    if key in _CANONICAL_NAMES:
        return _CANONICAL_NAMES[key]
    # 2) alias-variant → canonical document type
    if key in _ALIAS_TO_CANONICAL:
        return _ALIAS_TO_CANONICAL[key]
    return key


def _merge_key(dr: dict) -> str:
    """Compute a merge key from canonical document_type + document_context fields."""
    raw_type = dr.get("document_type") or dr.get("document_name") or ""
    doc_type = _canonical_doc_type(raw_type)
    ctx = dr.get("document_context")
    if isinstance(ctx, dict):
        ctx_parts = []
        for field in ("borrower", "employer", "account", "property", "business", "tax_year"):
            val = ctx.get(field)
            if val:
                ctx_parts.append(f"{field}={str(val).strip().lower()}")
        ctx_str = "|".join(ctx_parts) if ctx_parts else ""
    else:
        ctx_str = (str(ctx) if ctx else "").strip().lower()
    return f"{doc_type}|{ctx_str}"


# ---------------------------------------------------------------------------
# Collection helpers
# ---------------------------------------------------------------------------

def _as_list(val: Any) -> list:
    if val is None:
        return []
    if isinstance(val, list):
        return val
    if isinstance(val, dict):
        return [val]
    if isinstance(val, str):
        return [val] if val else []
    return [val]


def _union_strings(a: list, b: list) -> list:
    """Union two lists, deduplicating by string representation."""
    seen: set[str] = set()
    result: list = []
    for item in a + b:
        key = str(item)
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _union_by_field(a: list, b: list, field: str) -> list:
    """Union two lists of dicts, deduplicating by a key field."""
    a = [x if isinstance(x, dict) else {"value": x} for x in _as_list(a)]
    b = [x if isinstance(x, dict) else {"value": x} for x in _as_list(b)]
    seen: set[str] = set()
    result: list[dict] = []
    for item in a + b:
        k = str(item.get(field, item))
        if k not in seen:
            seen.add(k)
            result.append(item)
    return result


def _union_specs(a: list, b: list) -> list:
    """Union specifications, deduplicating by spec_id or text similarity."""
    seen_ids: set[str] = set()
    seen_text: set[str] = set()
    result: list = []
    for spec in _as_list(a) + _as_list(b):
        if isinstance(spec, dict):
            sid = spec.get("spec_id", "")
            if sid and sid in seen_ids:
                continue
            text_key = (spec.get("text") or spec.get("description") or "").strip().lower()
            if text_key and text_key in seen_text:
                continue
            if sid:
                seen_ids.add(sid)
            if text_key:
                seen_text.add(text_key)
            result.append(spec)
        else:
            key = str(spec).strip().lower()
            if key and key not in seen_text:
                seen_text.add(key)
                result.append(spec)
    return result


def _union_reasons(a: list, b: list) -> list:
    """Union reasons_needed, deduplicating by reason_id or text."""
    seen_ids: set[str] = set()
    seen_text: set[str] = set()
    result: list = []
    for reason in _as_list(a) + _as_list(b):
        if isinstance(reason, dict):
            rid = reason.get("reason_id", "")
            if rid and rid in seen_ids:
                continue
            text_key = (reason.get("text") or reason.get("reason") or "").strip().lower()
            if text_key and text_key in seen_text:
                continue
            if rid:
                seen_ids.add(rid)
            if text_key:
                seen_text.add(text_key)
            result.append(reason)
        else:
            key = str(reason).strip().lower()
            if key and key not in seen_text:
                seen_text.add(key)
                result.append(reason)
    return result


def _higher_severity(a: str, b: str) -> str:
    return a if _severity_val(a) <= _severity_val(b) else b


def _higher_priority(a: str, b: str) -> str:
    return a if _priority_val(a) <= _priority_val(b) else b


def _merge_two(base: dict, other: dict) -> dict:
    """Merge two document requests that share the same merge key."""
    merged = dict(base)

    merged["severity"] = _higher_severity(
        base.get("severity", "SOFT-STOP"),
        other.get("severity", "SOFT-STOP"),
    )
    merged["priority"] = _higher_priority(
        base.get("priority", "P3"),
        other.get("priority", "P3"),
    )

    merged["specifications"] = _union_specs(
        base.get("specifications", []),
        other.get("specifications", []),
    )
    merged["reasons_needed"] = _union_reasons(
        base.get("reasons_needed", []),
        other.get("reasons_needed", []),
    )
    merged["evidence_found"] = _union_strings(
        _as_list(base.get("evidence_found", [])),
        _as_list(other.get("evidence_found", [])),
    )
    merged["tags"] = _union_strings(
        _as_list(base.get("tags", [])),
        _as_list(other.get("tags", [])),
    )
    merged["guideline_trace"] = _union_by_field(
        base.get("guideline_trace", []),
        other.get("guideline_trace", []),
        "section",
    )
    merged["overlay_trace"] = _union_by_field(
        base.get("overlay_trace", []),
        other.get("overlay_trace", []),
        "overlay_id",
    )

    return merged


# ---------------------------------------------------------------------------
# Deterministic document rules live in tools/doc_rules.py and are applied
# inside merge_document_requests via apply_deterministic_rules().
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@tool
def merge_document_requests(
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
    state: Annotated[dict, InjectedState] = None,
) -> Command:
    """
    Collect document_requests from all modules (01-07), merge requests that
    refer to the same real-world document need (same document_type + context),
    aggregate specifications and reasons, and store the merged list.
    """
    s = state or {}
    module_outputs: dict = s.get("module_outputs", {})

    all_requests: list[dict] = []
    source_counts: dict[str, int] = {}

    _MODULE_CATEGORY = {
        "01": "Cross-Cutting", "02": "Income", "03": "Assets",
        "04": "Credit", "05": "Property", "06": "Title", "07": "Compliance",
    }

    for mod_key in ["01", "02", "03", "04", "05", "06", "07"]:
        mod = module_outputs.get(mod_key, {})
        raw_requests = _as_list(mod.get("document_requests", []))
        requests = normalize_all(raw_requests, default_category=_MODULE_CATEGORY.get(mod_key, "Other"))
        source_counts[mod_key] = len(requests)
        for dr in requests:
            dr.setdefault("source_module", mod_key)
        all_requests.extend(requests)

    groups: dict[str, dict] = {}
    for dr in all_requests:
        key = _merge_key(dr)
        if key in groups:
            groups[key] = _merge_two(groups[key], dr)
        else:
            groups[key] = dict(dr)

    merged = list(groups.values())
    llm_count = len(merged)

    # ------------------------------------------------------------------
    # Deterministic reconciliation (LLM proposes, rules dispose).
    # Applies: negative gates → mandatory floor → conditional docs →
    # derived income docs, deduping by canonical document type.
    # ------------------------------------------------------------------
    from tools.doc_rules import apply_deterministic_rules

    scenario_summary = s.get("scenario_summary", {})
    merged, det_stats = apply_deterministic_rules(
        merged, scenario_summary, canonical_fn=_canonical_doc_type,
    )
    injected_count = len(det_stats.get("injected", []))
    removed_count = len(det_stats.get("removed", []))

    sources_summary = ", ".join(
        f"{k}: {v}" for k, v in source_counts.items() if v > 0
    )
    msg = (
        f"Collected {len(all_requests)} document requests from modules "
        f"({sources_summary}) → {llm_count} after merging by "
        f"document_type + context (de-duped {len(all_requests) - llm_count})."
    )
    if removed_count:
        msg += f" Removed {removed_count} doc(s) via negative gates ({', '.join(det_stats['removed'])})."
    if injected_count:
        msg += f" Injected {injected_count} deterministic doc(s) ({', '.join(det_stats['injected'])})."
    msg += f" Final set: {len(merged)} documents."

    return Command(update={
        "module_outputs": {
            "08": {"merged_document_requests": merged},
        },
        "messages": [ToolMessage(msg, tool_call_id=tool_call_id)],
    })


def _build_inventory_types(inventory: list[dict]) -> set[str]:
    """Normalise document_inventory rows into a lowercase name lookup set.

    Inventory rows use detected_document_type / category / doc_type / name;
    canonical doc requests use "Credit Report" style names.  Both sides are
    normalised to lowercase (with space/underscore variants) so they match.
    """
    inventory_types: set[str] = set()
    for doc in inventory:
        for key in ("detected_document_type", "document_type", "doc_type", "name", "label", "category"):
            raw = (doc.get(key) or "").strip().lower()
            if raw:
                inventory_types.add(raw)
                inventory_types.add(raw.replace(" ", "_"))
                inventory_types.add(raw.replace("_", " "))
    return inventory_types


def assign_statuses(document_requests: list[dict], inventory: list[dict]) -> None:
    """Set each request's status to 'satisfied_but_review_required' when a
    matching document exists in *inventory*, else 'needed'. Mutates in place.

    Reused by rank_document_requests (borrower) and the co-borrower pass so
    both parties compute status against their own manifest inventory.
    """
    inventory_types = _build_inventory_types(inventory)
    for dr in document_requests:
        doc_type_lower = (dr.get("document_type") or "").strip().lower()
        all_names = _get_aliases(doc_type_lower) if doc_type_lower else set()
        if all_names & inventory_types:
            dr["status"] = "satisfied_but_review_required"
        else:
            dr["status"] = "needed"


@tool
def rank_document_requests(
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
    state: Annotated[dict, InjectedState] = None,
) -> Command:
    """
    Rank the merged document requests by severity (HARD-STOP first),
    priority (P0 first), then category order. Assign a status to each
    based on whether the document already exists in document_inventory.
    """
    s = state or {}
    mo = s.get("module_outputs", {})
    merged: list[dict] = _as_list(mo.get("08", {}).get("merged_document_requests", []))

    # Assign status (needed vs already-submitted) from document_inventory.
    inventory: list[dict] = _as_list(s.get("document_inventory", []))
    assign_statuses(merged, inventory)

    ranked = sorted(merged, key=_sort_key)

    status_counts: dict[str, int] = {}
    for dr in ranked:
        st = dr.get("status", "unknown")
        status_counts[st] = status_counts.get(st, 0) + 1

    status_summary = ", ".join(f"{k}: {v}" for k, v in status_counts.items())
    msg = (
        f"Ranked {len(ranked)} document requests. "
        f"Status breakdown: {status_summary}."
    )

    return Command(update={
        "module_outputs": {"08": {"ranked_document_requests": ranked}},
        "messages": [ToolMessage(msg, tool_call_id=tool_call_id)],
    })


# ---------------------------------------------------------------------------
# Satisfaction cross-check helpers
# ---------------------------------------------------------------------------

_SPEC_FIELD_MAP: dict[str, list[str]] = {
    "purchase price": ["purchasePrice", "purchase_price", "salesPrice", "sales_price"],
    "fully executed": ["signed", "dateSigned", "date_signed"],
    "all signatures": ["signed", "dateSigned", "date_signed"],
    "property address": ["propertyAddress", "property_address", "fullAddress", "address1"],
    "subject property": ["propertyAddress", "property_address", "fullAddress", "address1"],
    "parties": ["buyers", "sellers", "borrowers", "applicants"],
    "buyer": ["buyers", "borrowers"],
    "seller": ["sellers"],
    "earnest money": ["earnestMoney", "emd_amount", "earnest_money"],
    "closing date": ["closingDate", "closing_date", "settlement_date"],
    "addenda": ["addenda", "amendments", "counter_offers"],
    "credit score": ["credit_scores", "fico", "FicoScore", "creditScore"],
    "all borrowers": ["borrower_name", "borrowers", "applicants"],
    "tri-merge": ["bureaus", "experian", "transunion", "equifax"],
    "three bureaus": ["bureaus", "experian", "transunion", "equifax"],
    "tradeline": ["tradelines", "trade_lines"],
    "payment history": ["tradelines", "payment_history", "mortgage_history"],
    "public record": ["public_records", "publicRecords"],
    "inquiries": ["inquiries", "credit_inquiries"],
    "disputes": ["disputes", "disputed_accounts"],
    "collections": ["collections", "charge_offs"],
    "mortgage history": ["mortgage_history", "housing_history"],
    "social security": ["ssn", "socialSecurityNumber", "social_security"],
    "vested": ["vested_parties", "vesting", "grantee"],
    "legal description": ["legal_description", "legalDescription"],
    "effective date": ["effective_date", "effectiveDate"],
    "insurer": ["insurer", "title_company", "underwriter"],
    "title insurer": ["insurer", "title_company", "underwriter"],
    "liens": ["liens", "encumbrances", "exceptions"],
    "judgments": ["judgments", "liens", "tax_liens"],
    "chain of title": ["chain_of_title", "title_history"],
    "borrower signature": ["signed", "dateSigned", "borrower_signed"],
    "borrower name": ["borrower_name", "borrowers", "buyers"],
    "appraised value": ["appraised_value", "appraisedValue", "marketValue"],
    "property type": ["property_type", "propertyType"],
    "comparable": ["comparables", "comparable_sales"],
    "flood zone": ["flood_zone", "floodZone"],
    "employer": ["employer", "employer_name", "company"],
    "pay period": ["pay_period", "payPeriod"],
    "tax year": ["tax_year", "taxYear"],
    "account": ["account_number", "institution", "bank"],
    "account holder": ["account_holder", "accountHolder"],
    "deposit": ["deposits", "deposit_amount"],
}


# Documents whose name signals they're a supplement to a primary agreement
# rather than the agreement itself — e.g. "Addendum to Renew Lease
# Agreement" is a renewal rider, not the "Residential Lease Agreement" it
# renews. Both commonly get classified under the SAME generic name/doc_type
# ("Rental Agreement") by the upstream manifest classifier — the only place
# the distinction survives is metadata.specificDocumentType, which
# tools/shared/manifest_parser.py passes through into extracted_fields (see
# its NON_ENTITY_META_KEYS). So this check also inspects extracted_fields,
# not just name/doc_type. This keyword check lets _find_submitted_doc
# prefer the primary document when both are present in the manifest.
_ADDENDUM_LIKE_KEYWORDS = ("addendum", "amendment", "rider", "renewal", "extension")


def _is_addendum_like(*texts: str) -> bool:
    return any(kw in t.lower() for t in texts if t for kw in _ADDENDUM_LIKE_KEYWORDS)


def _find_submitted_doc(
    doc_type_canonical: str,
    submitted_docs: list[dict],
) -> dict | None:
    """Find a submitted doc matching the canonical document_type,
    including alias lookups from _DOCTYPE_ALIASES.

    When multiple submitted docs match (e.g. both the original
    "Residential Lease Agreement" and a later "Addendum to Renew Lease
    Agreement" are present), prefer the primary document — an addendum
    alone shouldn't be treated as satisfying specs describing the full
    agreement (parties, full property address, original term, etc.) when
    the original document is also available. Only fall back to an
    addendum/amendment/rider/renewal-like match if it's the only candidate.
    """
    all_names = _get_aliases(doc_type_canonical)

    fallback: dict | None = None
    for sdoc in submitted_docs:
        name = (sdoc.get("name") or "").strip().lower()
        dtype = (sdoc.get("doc_type") or "").strip().lower()
        sdoc_names = {name, dtype, name.replace(" ", "_"), dtype.replace("_", " ")}
        if not (all_names & sdoc_names):
            continue
        specific_type = str((sdoc.get("extracted_fields") or {}).get("specificDocumentType") or "")
        if _is_addendum_like(name, dtype, specific_type):
            if fallback is None:
                fallback = sdoc
            continue
        return sdoc

    return fallback


def _submitted_doc_ids(sdoc: dict) -> list[str]:
    """Return the physical document UUID(s) for a matched submitted doc.

    Prefers ``document_ids`` (a merged doc — e.g. combined paystubs — spans
    several files); falls back to a single ``document_id``/``id``.
    """
    ids = sdoc.get("document_ids")
    if isinstance(ids, list) and ids:
        return [str(i) for i in ids if i]
    one = sdoc.get("document_id") or sdoc.get("id")
    return [str(one)] if one else []


def _spec_text(spec: Any) -> str:
    """Extract the text content from a spec (string or dict)."""
    if isinstance(spec, str):
        return spec
    if isinstance(spec, dict):
        return spec.get("text") or spec.get("specification") or spec.get("description") or str(spec)
    return str(spec)


def _check_spec_satisfied(
    spec: Any,
    extracted_fields: dict,
) -> str | None:
    """If the spec is satisfied by extracted_fields, return a reason string.
    Returns None if not satisfied."""
    text_lower = _spec_text(spec).lower()
    ef_keys_lower = {k.lower() for k in extracted_fields}

    for keyword, field_names in _SPEC_FIELD_MAP.items():
        if keyword not in text_lower:
            continue
        for fname in field_names:
            if fname.lower() in ef_keys_lower:
                val = None
                for real_key in extracted_fields:
                    if real_key.lower() == fname.lower():
                        val = extracted_fields[real_key]
                        break
                if val is not None and val != "" and val != []:
                    return (
                        f"Dropped — submitted document contains {fname} "
                        f"field confirming this requirement is present"
                    )
    return None


# ---------------------------------------------------------------------------
# LLM-based spec satisfaction check
# ---------------------------------------------------------------------------

_SATISFACTION_PROMPT = """\
You are a mortgage document reviewer. A borrower has submitted a "{doc_type}" document.
Below are the extracted fields from that document, and a list of specifications that
the document must satisfy.

For each specification, determine if the extracted fields provide evidence that the
requirement is met. A spec is "satisfied" if the extracted data confirms the requirement
is present — even partially. Be reasonably lenient: if the document type matches and
relevant fields exist (even with limited data), the requirement to OBTAIN/PROVIDE
the document itself is satisfied.

However, do NOT mark a spec as satisfied if:
- The relevant extracted field is completely empty ([] or null or "")
- There is genuinely no evidence in the fields for that requirement

Specs about image/document QUALITY — "must be legible", "clear photo",
"readable", "identifiable information", "good quality scan", etc. — do not
have a dedicated extracted field, but they ARE satisfied by indirect
evidence: if the extracted fields contain real, specific identifying data
(e.g. a full name, ID/license number, date of birth, expiration date,
address) rather than being mostly empty or garbled, that successful
extraction is itself proof the source image was legible and clear enough
to read. Mark such a spec satisfied in that case, with a reason like
"Legible — extracted fields (name, DOB, license #, expiration) were
successfully read from the submitted image." Only leave it unsatisfied if
the extracted fields are sparse, empty, or clearly placeholder/garbled.

## Extracted Fields
{extracted_fields_json}
{reference_block}
## Specifications to Check
{specs_json}

## Response Format
Return a JSON array of objects for ONLY the satisfied specs:
[
  {{"specification": "<exact spec text>", "reason": "<brief reason why satisfied>"}}
]

If none are satisfied, return an empty array: []
Return ONLY valid JSON, no other text.
"""


_REFERENCE_BLOCK_TEMPLATE = """
## Reference Data (authoritative loan facts — from the loan application and the
## eligibility-locked loan file)
This block contains the authoritative borrower identity and loan facts for this
file (e.g. borrower names, SSN, DOB, loan amount, LTV, occupancy, loan purpose,
program, and subject property).

Use this ONLY to verify cross-document CONSISTENCY requirements — specs that ask
whether a value on the submitted document matches the loan (for example "name
matches the loan application", "property address matches the subject property",
"loan amount matches", "named insured matches the borrower").

Rules:
- Do NOT use this reference to satisfy a specification that requires the submitted
  document itself to CONTAIN data. The submitted document's own extracted fields
  are the only evidence that a value is present.
- Only apply a reference fact when a specification actually asks for that kind of
  cross-check. Ignore reference facts that are irrelevant to the spec.
- If the submitted document's value CONFLICTS with this reference (for example a
  different legal name, address, or loan amount), do NOT mark that specification
  as satisfied — leave it for reviewer reconciliation and note the discrepancy in
  your reason.
{reference_json}
"""


def _llm_check_specs(
    doc_type: str,
    specifications: list,
    extracted_fields: dict,
    reference_context: dict | None = None,
) -> list[dict]:
    """Use an LLM to determine which specs are satisfied by extracted fields.

    When ``reference_context`` is provided (authoritative borrower identity from
    the loan application), the model may use it to verify cross-document
    consistency specs — but not to satisfy specs on its own.
    """
    import json
    import logging
    import os

    from langchain_anthropic import ChatAnthropic

    specs_text = [_spec_text(s) for s in _as_list(specifications)]
    if not specs_text:
        return []

    ef_summary = {}
    for k, v in extracted_fields.items():
        if isinstance(v, list) and len(v) > 5:
            ef_summary[k] = v[:5] + [f"... ({len(v)} items total)"]
        elif isinstance(v, dict) and len(str(v)) > 500:
            ef_summary[k] = {dk: dv for i, (dk, dv) in enumerate(v.items()) if i < 10}
        else:
            ef_summary[k] = v

    reference_block = ""
    if reference_context:
        reference_block = _REFERENCE_BLOCK_TEMPLATE.format(
            reference_json=json.dumps(reference_context, indent=2, default=str),
        )

    prompt = _SATISFACTION_PROMPT.format(
        doc_type=doc_type,
        extracted_fields_json=json.dumps(ef_summary, indent=2, default=str),
        reference_block=reference_block,
        specs_json=json.dumps(specs_text, indent=2),
    )

    model = os.environ.get("SATISFACTION_CHECK_MODEL", "claude-haiku-4-5")
    logger = logging.getLogger(__name__)

    try:
        llm = ChatAnthropic(model=model, max_tokens=4096, max_retries=2)
        response = llm.invoke(prompt)
        content = response.content if hasattr(response, "content") else str(response)

        if "```json" in content:
            content = content.split("```json")[1].split("```")[0]
        elif "```" in content:
            content = content.split("```")[1].split("```")[0]

        result = json.loads(content.strip())
        if isinstance(result, list):
            valid = []
            for item in result:
                if isinstance(item, dict) and "specification" in item:
                    valid.append({
                        "specification": item["specification"],
                        "reason": item.get("reason", "Confirmed by submitted document"),
                    })
            return valid
    except Exception as e:
        logger.warning("LLM satisfaction check failed for %s: %s", doc_type, e)

    return []


def _extract_identity_reference(submitted_docs: list[dict]) -> dict:
    """Pull authoritative borrower identity from the submitted 1003, so
    identity/consistency specs on other documents (e.g. "name matches the loan
    application") can be truly cross-checked instead of confirmed in isolation.
    """
    doc = _find_submitted_doc("Loan Application (1003)", submitted_docs)
    if not doc:
        return {}

    ef = doc.get("extracted_fields", {}) or {}
    raw_borrowers = ef.get("new1003Borrowers") or ef.get("borrowers") or []

    borrowers: list[dict] = []
    for b in _as_list(raw_borrowers):
        if not isinstance(b, dict):
            continue
        nm = b.get("name") if isinstance(b.get("name"), dict) else b
        parts = [nm.get("firstName"), nm.get("middleName"), nm.get("lastName")]
        full = " ".join(str(p).strip() for p in parts if p)
        entry: dict = {}
        if full.strip():
            entry["name"] = full.strip()
        dob = b.get("DOB") or b.get("dob")
        if dob:
            entry["dob"] = dob
        ssn = b.get("last4SSN") or b.get("last4_ssn")
        if ssn:
            entry["last4_ssn"] = ssn
        if entry:
            borrowers.append(entry)

    return {"loan_application_borrowers": borrowers} if borrowers else {}


def _build_reference_context(scenario_summary: dict, submitted_docs: list[dict]) -> dict:
    """Assemble the authoritative cross-reference facts for satisfaction checks.

    Combines borrower identity (from the 1003) with eligibility-locked loan facts
    (loan amount, LTV, occupancy, purpose, program, subject property) so that
    consistency specs on ANY document — title, deed, hazard insurance, purchase
    contract, appraisal, etc. — can be verified against the loan file rather than
    confirmed from the submitted document alone.
    """
    ctx: dict = {}
    ctx.update(_extract_identity_reference(submitted_docs))

    ss = scenario_summary or {}
    loan_facts: dict = {}

    numbers = ss.get("numbers", {}) or {}
    for src_key, out_key in (
        ("loan_amount", "loan_amount"),
        ("purchase_price", "purchase_price"),
        ("appraised_value", "appraised_value"),
        ("LTV", "ltv"),
        ("CLTV", "cltv"),
    ):
        val = numbers.get(src_key)
        if val not in (None, "", [], "unknown"):
            loan_facts[out_key] = val

    for key in ("occupancy", "purpose", "program", "loan_number",
                "cash_out_amount", "borrower_type"):
        val = ss.get(key)
        if val not in (None, "", [], "unknown"):
            loan_facts[key] = val

    prop = ss.get("property", {}) or {}
    prop_clean = {
        k: v for k, v in prop.items()
        if v not in (None, "", [], "unknown")
    }
    if prop_clean:
        loan_facts["subject_property"] = prop_clean

    if loan_facts:
        ctx["loan_facts"] = loan_facts

    return ctx


# ---------------------------------------------------------------------------
# 1003 (URLA) completeness + consistency check
# ---------------------------------------------------------------------------
#
# The mandatory 1003 request carries the spec "All sections complete and
# consistent with the loan terms and program".  That is not a discrete
# attribute the generic checker can confirm (there is no single field for it,
# and the 1003 is deliberately not cross-referenced against itself), so it is
# evaluated directly here against the extracted URLA fields:
#
#   Completeness — the core URLA sections every application must populate
#     (borrower identity §1a, employment/income §1b–1e, assets §2a, subject
#     property + loan terms §4a, declarations §5a) contain extracted data.
#     Conditional sections (§1c/1d, §2c/2d, §3a–3c, §4b–4d) are only required
#     when their ``isSectionXX`` applicability flag is explicitly true.
#   Consistency  — loan-level values the 1003 exposes (loan amount, subject
#     property address, occupancy, purpose) do not conflict with the
#     eligibility-locked loan facts.
#
# The spec clears only when the application is actually complete AND nothing on
# it contradicts the loan; otherwise it stays open for reviewer follow-up.

_1003_BORROWER_KEYS = ("new1003Borrowers", "borrowers")


def _is_1003_completeness_spec(spec_text: str) -> bool:
    t = (spec_text or "").lower()
    return "complete and consistent" in t or "sections complete" in t


def _has_any_value(obj: Any) -> bool:
    """True if *obj* contains at least one non-empty leaf value."""
    if obj is None:
        return False
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, (int, float)):
        return True
    if isinstance(obj, str):
        return obj.strip() != ""
    if isinstance(obj, dict):
        return any(_has_any_value(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_has_any_value(v) for v in obj)
    return bool(obj)


def _flag_true(val: Any) -> bool:
    """Interpret an ``isSectionXX`` applicability flag as an explicit True."""
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in ("true", "yes", "y", "1")
    if isinstance(val, (int, float)):
        return val == 1
    return False


def _1003_borrowers(extracted_fields: dict) -> list[dict]:
    for k in _1003_BORROWER_KEYS:
        raw = extracted_fields.get(k)
        if raw:
            return [b for b in _as_list(raw) if isinstance(b, dict)]
    return []


def _1003_borrower_name(b: dict) -> str:
    nm = b.get("name") if isinstance(b.get("name"), dict) else b
    if not isinstance(nm, dict):
        return ""
    parts = [nm.get("firstName"), nm.get("middleName"), nm.get("lastName")]
    return " ".join(str(p).strip() for p in parts if p).strip()


def _to_number(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        digits = re.sub(r"[^0-9.]", "", v)
        try:
            return float(digits) if digits else None
        except ValueError:
            return None
    return None


def _first_present(d: dict, keys: tuple) -> Any:
    for k in keys:
        v = d.get(k)
        if v not in (None, "", [], {}):
            return v
    return None


def _norm_text(v: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(v or "").lower()).strip()


def _evaluate_1003_completeness_consistency(
    extracted_fields: dict,
    reference_context: dict | None,
) -> tuple[bool, str]:
    """Return (satisfied, reason) for the 1003 'complete and consistent' spec."""
    borrowers = _1003_borrowers(extracted_fields)
    if not borrowers:
        return False, "No borrower application data was extracted from the 1003."

    # --- Completeness: required-always sections must contain data ---
    # Conditional sections are required only when their isSectionXX flag is true.
    missing_all: list[str] = []
    for idx, b in enumerate(borrowers):
        label = _1003_borrower_name(b) or f"borrower #{idx + 1}"
        missing: list[str] = []
        if not _1003_borrower_name(b):
            missing.append("identity (§1a)")

        # Primary borrower must show income, assets, subject property/loan
        # terms, and declarations. Co-borrowers need only identity (they may
        # not carry separate income/assets on the application).
        if idx == 0:
            income = any(
                _has_any_value(b.get(s)) for s in ("section1b", "section1e")
            )
            if not income:
                missing.append("employment/income (§1b–1e)")

            if not _has_any_value(b.get("section2a")):
                missing.append("assets (§2a)")

            s4a = b.get("section4a") or {}
            if not _has_any_value(s4a.get("propertyAddress")):
                missing.append("subject property address (§4a)")
            if not _has_any_value(_first_present(
                s4a, ("loanAmount", "purposeOfLoan", "occupancy")
            )):
                missing.append("loan terms — amount/purpose/occupancy (§4a)")

            if not _has_any_value(b.get("section5a")):
                missing.append("declarations (§5a)")

        # Conditional sections — required only when flagged applicable.
        for flag, section, sec_label in (
            ("isSection1c", "section1c", "additional employment (§1c)"),
            ("isSection1d", "section1d", "previous employment (§1d)"),
            ("isSection2c", "section2c", "liabilities (§2c)"),
            ("isSection2d", "section2d", "other expenses (§2d)"),
            ("isSection3a", "section3a", "real estate owned (§3a)"),
            ("isSection4b", "section4b", "other new mortgages (§4b)"),
        ):
            if _flag_true(b.get(flag)) and not _has_any_value(b.get(section)):
                missing.append(sec_label)

        if missing:
            missing_all.append(f"{label}: {', '.join(missing)}")

    if missing_all:
        return False, (
            "Incomplete — the following 1003 sections have no extracted data: "
            + "; ".join(missing_all)
        )

    # --- Consistency: loan-level values on the 1003 must match loan facts ---
    loan_facts = (reference_context or {}).get("loan_facts", {}) or {}
    primary = borrowers[0]
    s4a = primary.get("section4a") or {}
    conflicts: list[str] = []
    checked: list[str] = []

    la_1003 = _to_number(s4a.get("loanAmount"))
    la_ref = _to_number(loan_facts.get("loan_amount"))
    if la_1003 and la_ref:
        checked.append("loan amount")
        if abs(la_1003 - la_ref) > max(1.0, 0.01 * la_ref):
            conflicts.append(f"loan amount ({la_1003:.0f} vs loan file {la_ref:.0f})")

    subj = loan_facts.get("subject_property", {}) or {}
    addr = s4a.get("propertyAddress") or {}
    if isinstance(addr, dict) and subj:
        z1 = str(addr.get("zipCode") or addr.get("zip") or "").strip()[:5]
        z2 = str(subj.get("zip") or subj.get("zipCode") or "").strip()[:5]
        if z1 and z2:
            checked.append("property ZIP")
            if z1 != z2:
                conflicts.append(f"property ZIP ({z1} vs loan file {z2})")

    occ_1003 = _norm_text(s4a.get("occupancy"))
    occ_ref = _norm_text(loan_facts.get("occupancy"))
    if occ_1003 and occ_ref:
        checked.append("occupancy")
        if occ_1003 not in occ_ref and occ_ref not in occ_1003:
            conflicts.append(f"occupancy ({s4a.get('occupancy')} vs loan file {loan_facts.get('occupancy')})")

    if conflicts:
        return False, "Inconsistent with the loan terms: " + "; ".join(conflicts)

    consistency_note = (
        f" and consistent with the loan file ({', '.join(checked)})"
        if checked else " with no detected conflicts against the loan file"
    )
    return True, (
        "All core 1003 sections are populated (borrower identity, "
        "employment/income, assets, subject property/loan terms, declarations)"
        + consistency_note + "."
    )


def _check_1003_specs(
    doc_type: str,
    specifications: list,
    extracted_fields: dict,
    reference_context: dict | None,
) -> list[dict]:
    """Satisfaction check for the 1003.

    The generic 'complete and consistent' spec is evaluated deterministically
    (completeness + consistency); any other specs (e.g. signed/dated) fall back
    to the standard extracted-fields LLM check, confirmed from the 1003's own
    fields only (no reference context, since the 1003 IS the reference).
    """
    completeness_specs: list = []
    other_specs: list = []
    for s in _as_list(specifications):
        (completeness_specs if _is_1003_completeness_spec(_spec_text(s)) else other_specs).append(s)

    satisfied = _llm_check_specs(doc_type, other_specs, extracted_fields, reference_context=None)

    for s in completeness_specs:
        ok, reason = _evaluate_1003_completeness_consistency(extracted_fields, reference_context)
        if ok:
            satisfied.append({"specification": _spec_text(s), "reason": reason})

    return satisfied


def run_satisfaction_pass(
    document_requests: list[dict],
    submitted_docs: list[dict],
    scenario_summary: dict,
) -> tuple[int, int]:
    """Cross-check *document_requests* against *submitted_docs*, moving
    satisfied specs from 'specifications' to 'satisfied_specifications'.

    Mutates each request in place. Returns (total_checked, total_satisfied).

    Reused by cross_check_satisfaction (borrower) and the co-borrower pass so
    both parties are evaluated with identical satisfaction semantics against
    their own manifest.
    """
    # Authoritative cross-reference facts (borrower identity from the 1003 +
    # eligibility-locked loan facts) used to cross-check consistency specs
    # (name, address, loan amount, occupancy, ...) on other documents.
    reference_context = _build_reference_context(scenario_summary, submitted_docs)

    total_checked = 0
    total_satisfied_specs = 0

    for dr in document_requests:
        doc_type = dr.get("document_type") or ""
        sdoc = _find_submitted_doc(doc_type, submitted_docs)
        if not sdoc:
            dr["satisfied_specifications"] = []
            continue

        total_checked += 1

        # Physical UUID(s) of the manifest document that matched this request,
        # so the condition points back to the exact submitted file(s) — the
        # ones a reviewer should open to confirm/close it. Stamped on match
        # (not only on a confirmed spec) so a "satisfied_but_review_required"
        # condition still carries the file the reviewer needs.
        matched_ids = _submitted_doc_ids(sdoc)
        if matched_ids:
            dr["document_ids"] = matched_ids

        # When the match came through a blanket alias (functionally
        # equivalent document), treat ALL specs as satisfied.
        sdoc_name = (sdoc.get("name") or "").strip().lower()
        if _is_blanket_alias(doc_type, sdoc_name):
            satisfied_specs = [
                {
                    "specification": _spec_text(spec),
                    "reason": (
                        f"Satisfied — equivalent document '{sdoc.get('name', '')}' "
                        f"is present in the manifest"
                    ),
                }
                for spec in _as_list(dr.get("specifications", []))
            ]
            dr["specifications"] = []
            dr["satisfied_specifications"] = satisfied_specs
            total_satisfied_specs += len(satisfied_specs)
            continue

        extracted = sdoc.get("extracted_fields", {})
        if not extracted:
            dr["satisfied_specifications"] = []
            continue

        # The 1003 is special: its own consistency/completeness spec is
        # evaluated deterministically against the extracted URLA sections and
        # the eligibility-locked loan facts (see _check_1003_specs). Every other
        # document passes the authoritative loan facts as reference context so
        # consistency specs (name/address/amount matching) are cross-checked
        # against the loan file rather than confirmed from the doc alone.
        if _canonical_doc_type(doc_type) == "loan application (1003)":
            satisfied_specs = _check_1003_specs(
                doc_type, dr.get("specifications", []), extracted, reference_context
            )
        else:
            satisfied_specs = _llm_check_specs(
                doc_type, dr.get("specifications", []), extracted,
                reference_context=reference_context,
            )
        satisfied_spec_texts = {s["specification"] for s in satisfied_specs}
        remaining_specs = [
            spec for spec in _as_list(dr.get("specifications", []))
            if _spec_text(spec) not in satisfied_spec_texts
        ]

        dr["specifications"] = remaining_specs
        dr["satisfied_specifications"] = satisfied_specs
        total_satisfied_specs += len(satisfied_specs)

    return total_checked, total_satisfied_specs


@tool
def cross_check_satisfaction(
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
    state: Annotated[dict, InjectedState] = None,
) -> Command:
    """
    Cross-check ranked document requests against submitted documents from
    the manifest.  For each request whose document exists in the manifest,
    compare specifications against the doc's extracted_fields.  Satisfied
    specs are moved from 'specifications' to 'satisfied_specifications'.
    """
    s = state or {}
    mo = s.get("module_outputs", {})
    ranked: list[dict] = _as_list(
        mo.get("08", {}).get("ranked_document_requests", [])
    )

    scenario_summary = s.get("scenario_summary", {})
    submitted_docs: list[dict] = _as_list(scenario_summary.get("_submitted_docs", []))

    total_checked, total_satisfied_specs = run_satisfaction_pass(
        ranked, submitted_docs, scenario_summary
    )

    msg = (
        f"Cross-checked {total_checked} document requests against submitted docs. "
        f"Moved {total_satisfied_specs} specifications to satisfied_specifications."
    )

    return Command(update={
        "module_outputs": {"08": {"ranked_document_requests": ranked}},
        "messages": [ToolMessage(msg, tool_call_id=tool_call_id)],
    })


@tool
def generate_final_output(
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
    state: Annotated[dict, InjectedState] = None,
) -> Command:
    """
    Assemble the final output JSON: scenario_summary, seen_conflicts,
    ranked document_requests, and aggregate stats.
    """
    s = state or {}
    mo = s.get("module_outputs", {})

    document_requests: list[dict] = _as_list(
        mo.get("08", {}).get("ranked_document_requests", [])
    )
    if not document_requests:
        document_requests = _as_list(
            mo.get("08", {}).get("merged_document_requests", [])
        )

    # Enforce consistent field schema on every document request
    document_requests = [normalize_document_structure(dr) for dr in document_requests]

    # Final label pass: rename canonical masterlist names to NQMF display
    # wording. Runs last (after matching/dedup/satisfaction) so only the
    # customer-facing label changes; STEP_09 forces the display heading to
    # equal this document_type, so headings follow automatically.
    for dr in document_requests:
        dr["document_type"] = apply_output_display_name(dr.get("document_type", ""))

    scenario_summary = s.get("scenario_summary", {})
    clean_summary: dict[str, Any] = {
        k: v for k, v in scenario_summary.items()
        if not k.startswith("_")
    }

    seen_conflicts = _as_list(s.get("seen_conflicts", []))

    # Stats
    total = len(document_requests)
    hard_stops = sum(
        1 for dr in document_requests if dr.get("severity") == "HARD-STOP"
    )
    by_category: dict[str, int] = {}
    by_priority: dict[str, int] = {}
    by_status: dict[str, int] = {}
    for dr in document_requests:
        cat = dr.get("document_category", "Other")
        pri = dr.get("priority", "P3")
        st = dr.get("status", "unknown")
        by_category[cat] = by_category.get(cat, 0) + 1
        by_priority[pri] = by_priority.get(pri, 0) + 1
        by_status[st] = by_status.get(st, 0) + 1

    stats = {
        "total_document_requests": total,
        "hard_stop_documents": hard_stops,
        "by_category": by_category,
        "by_priority": by_priority,
        "by_status": by_status,
    }

    final: dict[str, Any] = {
        "scenario_summary": clean_summary,
        "seen_conflicts": seen_conflicts,
        "document_requests": document_requests,
        "stats": stats,
    }

    msg = (
        f"Final output generated: {total} document requests, "
        f"{hard_stops} hard-stop(s). "
        f"By priority: {by_priority}. By status: {by_status}."
    )

    return Command(update={
        "final_output": final,
        "current_step": "STEP_08",
        "messages": [ToolMessage(msg, tool_call_id=tool_call_id)],
    })
