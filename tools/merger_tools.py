"""
merger_tools.py — Tools for STEP_08: Merger, De-Duper, Ranker (v2 Document-Centric).

Four tools:
  1. merge_document_requests      — collect & merge doc requests from modules 01-07
  2. rank_document_requests       — sort by severity/priority/category and assign status
  3. cross_check_satisfaction     — compare specs against submitted doc contents
  4. generate_final_output        — assemble the final output JSON with stats
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command
from typing_extensions import Annotated

from tools.shared.normalize import normalize_all, normalize_document_structure


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

    # Build a lookup of existing documents from document_inventory.
    # Inventory rows use detected_document_type / category / doc_type / name;
    # canonical doc requests use "Credit Report" style names.  We normalise
    # both sides to lowercase-with-underscores so they can match.
    inventory: list[dict] = _as_list(s.get("document_inventory", []))
    inventory_types: set[str] = set()
    for doc in inventory:
        for key in ("detected_document_type", "document_type", "doc_type", "name", "label", "category"):
            raw = (doc.get(key) or "").strip().lower()
            if raw:
                inventory_types.add(raw)
                inventory_types.add(raw.replace(" ", "_"))
                inventory_types.add(raw.replace("_", " "))

    for dr in merged:
        doc_type_lower = (dr.get("document_type") or "").strip().lower()
        all_names = _get_aliases(doc_type_lower) if doc_type_lower else set()
        if all_names & inventory_types:
            dr["status"] = "satisfied_but_review_required"
        else:
            dr["status"] = "needed"

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


def _find_submitted_doc(
    doc_type_canonical: str,
    submitted_docs: list[dict],
) -> dict | None:
    """Find a submitted doc matching the canonical document_type,
    including alias lookups from _DOCTYPE_ALIASES."""
    all_names = _get_aliases(doc_type_canonical)

    for sdoc in submitted_docs:
        name = (sdoc.get("name") or "").strip().lower()
        dtype = (sdoc.get("doc_type") or "").strip().lower()
        sdoc_names = {name, dtype, name.replace(" ", "_"), dtype.replace("_", " ")}
        if all_names & sdoc_names:
            return sdoc
    return None


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

## Extracted Fields
{extracted_fields_json}

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


def _llm_check_specs(
    doc_type: str,
    specifications: list,
    extracted_fields: dict,
) -> list[dict]:
    """Use an LLM to determine which specs are satisfied by extracted fields."""
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

    prompt = _SATISFACTION_PROMPT.format(
        doc_type=doc_type,
        extracted_fields_json=json.dumps(ef_summary, indent=2, default=str),
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

    total_checked = 0
    total_satisfied_specs = 0

    for dr in ranked:
        doc_type = dr.get("document_type") or ""
        sdoc = _find_submitted_doc(doc_type, submitted_docs)
        if not sdoc:
            dr["satisfied_specifications"] = []
            continue

        total_checked += 1

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

        satisfied_specs = _llm_check_specs(
            doc_type, dr.get("specifications", []), extracted
        )
        satisfied_spec_texts = {s["specification"] for s in satisfied_specs}
        remaining_specs = [
            spec for spec in _as_list(dr.get("specifications", []))
            if _spec_text(spec) not in satisfied_spec_texts
        ]

        dr["specifications"] = remaining_specs
        dr["satisfied_specifications"] = satisfied_specs
        total_satisfied_specs += len(satisfied_specs)

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
