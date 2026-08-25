"""
coborrower.py — Per-party document attribution from a single manifest.

Runs as a post-pipeline graph node (after STEP_09). There is no separate
co-borrower input: the parties are auto-detected from the eligibility JSON
(falling back to the loan XML's BORROWER_2), and each manifest document is
fuzzy-matched by its name metadata to a party. Each party then gets the FULL
condition set, satisfied ONLY by the documents assigned to that party:

  1. Resolve parties (primary + optional co-borrower) via ``resolve_parties``.
  2. Assign every submitted document to a party via ``assign_docs_to_parties``
     (unnamed/loan-level docs go to all parties; joint docs go to both).
  3. For each party, take the borrower's canonical required-doc set
     (module_outputs["08"]), restore the full specification list, and re-run the
     SAME satisfaction + status logic against that party's assigned documents.
  4. Rebuild the AUS display block, apply the output display-name rename, and
     tag ``party`` (borrower/coborrower) with ``applicable_parties=[name]``.

All parties' requests live in the single ``final_output.document_requests``
array (the chosen "single array tagged by party" output shape). A single-party
loan keeps today's behavior (everything tagged ``party="borrower"``).
"""

from __future__ import annotations

import copy
import json
import logging
import re
from difflib import SequenceMatcher
from typing import Any

from tools.merger_tools import assign_statuses, run_satisfaction_pass
from tools.presentation_tools import _aus_restyle_spec
from tools.shared.normalize import (
    apply_output_display_name,
    normalize_document_structure,
)
from tools.shared.party_scope import is_borrower_specific
from tools.shared.xml_parser import parse_mismo_xml

logger = logging.getLogger(__name__)


def _spec_text(spec: Any) -> str:
    if isinstance(spec, str):
        return spec
    if isinstance(spec, dict):
        return (
            spec.get("text") or spec.get("specification")
            or spec.get("description") or ""
        )
    return str(spec)


def _restore_full_specifications(dr: dict) -> None:
    """Fold satisfied_specifications back into specifications so the co-borrower
    is evaluated against the complete requirement list, not the borrower's
    already-reduced one. Mutates in place."""
    specs = list(dr.get("specifications") or [])
    seen = {_spec_text(s).strip().lower() for s in specs}
    for sat in dr.get("satisfied_specifications") or []:
        text = _spec_text(sat)
        if text and text.strip().lower() not in seen:
            specs.append(text)
            seen.add(text.strip().lower())
    dr["specifications"] = specs
    dr["satisfied_specifications"] = []


def _rebuild_display(dr: dict, borrower_display: dict | None) -> dict:
    """Rebuild the AUS display block for a co-borrower doc.

    Reuses the borrower's styled heading / reason / review notes (same document
    type ⇒ same purpose), but recomputes the satisfied-vs-needed requirement
    split from THIS party's specifications, mirroring presentation_tools.
    """
    display: dict[str, Any] = {}
    if isinstance(borrower_display, dict):
        display = {
            k: copy.deepcopy(v)
            for k, v in borrower_display.items()
            if k in ("reason_for_requirement", "review_notes")
        }

    display["document_heading"] = dr.get("document_type", "")

    styled_sats: list[str] = []
    for item in dr.get("satisfied_specifications") or []:
        if isinstance(item, dict):
            text = item.get("specification", "")
            reason = (item.get("reason") or "").strip()
        else:
            text, reason = str(item), ""
        if not text:
            continue
        base = _aus_restyle_spec(text).rstrip(".")
        styled_sats.append(f"{base} — {reason}" if reason
                            else f"{base} — confirmed by submitted document.")
    display["satisfied_requirements"] = styled_sats

    styled_reqs: list[str] = []
    for spec in dr.get("specifications") or []:
        text = _spec_text(spec)
        if text:
            styled_reqs.append(_aus_restyle_spec(text))
    display["documentation_requirements"] = styled_reqs

    # Point back to the co-borrower manifest file(s) that satisfied this condition.
    display["document_ids"] = dr.get("document_ids") or []

    return display


def build_party_document_requests(
    canonical_docs: list[dict],
    borrower_final_docs: list[dict],
    party_submitted_docs: list[dict],
    party_name: str,
    party_tag: str,
    scenario_summary: dict,
) -> list[dict]:
    """Produce one party's document_requests (tagged ``party=party_tag``).

    Args:
        canonical_docs: pre-rename ranked docs (module_outputs["08"]); canonical
            names so alias/satisfaction matching works.
        borrower_final_docs: the styled final docs (renamed labels + display),
            used only to reuse heading/reason/review-note styling.
        party_submitted_docs: the subset of parsed manifest documents assigned
            to this party (see ``assign_docs_to_parties``).
        party_name: the party's display name (goes into ``applicable_parties``).
        party_tag: "borrower" or "coborrower".
        scenario_summary: shared scenario (loan facts + eligibility).
    """
    docs = copy.deepcopy(canonical_docs)
    for dr in docs:
        _restore_full_specifications(dr)
        # Clear any satisfaction stamped against the full manifest so each party
        # is evaluated purely against its own assigned documents.
        dr["document_ids"] = []

    # Re-evaluate satisfaction + status against this party's assigned documents.
    run_satisfaction_pass(docs, party_submitted_docs, scenario_summary)
    assign_statuses(docs, party_submitted_docs)

    # Map borrower final display blocks by their (renamed) document_type so we
    # can reuse styling.
    final_display_by_type: dict[str, dict] = {}
    for fd in borrower_final_docs:
        dt = (fd.get("document_type") or "").strip().lower()
        if dt and isinstance(fd.get("display"), dict):
            final_display_by_type[dt] = fd["display"]

    out: list[dict] = []
    for dr in docs:
        dr = normalize_document_structure(dr)
        dr["document_type"] = apply_output_display_name(dr.get("document_type", ""))
        dr["party"] = party_tag
        dr["applicable_parties"] = [party_name] if party_name else []
        borrower_disp = final_display_by_type.get(
            (dr.get("document_type") or "").strip().lower()
        )
        dr["display"] = _rebuild_display(dr, borrower_disp)
        out.append(dr)

    return out


def _fold_party_into_display(dr: dict) -> None:
    """Surface the ``party`` tag inside ``display`` so the frontend — which
    renders only ``display`` — knows whether a document belongs to the borrower
    or the co-borrower. Mutates in place."""
    display = dr.get("display")
    if not isinstance(display, dict):
        display = {}
        dr["display"] = display
    display["party"] = dr.get("party", "borrower")


# ---------------------------------------------------------------------------
# Party resolution (eligibility -> loan XML BORROWER_2)
# ---------------------------------------------------------------------------


def _norm_name(name: str) -> str:
    """Normalize a person name for fuzzy comparison: lowercase, drop commas and
    punctuation (so "Matthew,O'Malley" and "MATTHEW OMALLEY" collapse to the
    same token string), collapse whitespace."""
    s = (name or "").lower().replace(",", " ")
    s = re.sub(r"[^a-z0-9 ]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _name_matches(a: str, b: str, threshold: float = 0.82) -> bool:
    """True when two names plausibly refer to the same person.

    Handles case/punctuation differences (via normalization), whole-string
    similarity (SequenceMatcher ratio), and full first+last token overlap.
    A single shared token (e.g. only a shared last name) is NOT enough.
    """
    na, nb = _norm_name(a), _norm_name(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    if SequenceMatcher(None, na, nb).ratio() >= threshold:
        return True
    shared = set(na.split()) & set(nb.split())
    return len(shared) >= 2


def _load_eligibility(state: dict) -> dict:
    raw = state.get("eligibility_json")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def _find_key(obj: Any, key: str) -> Any:
    """Depth-first search for the first scalar value under *key* anywhere in a
    nested dict/list structure."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key and not isinstance(v, (dict, list)):
                return v
        for v in obj.values():
            found = _find_key(v, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_key(item, key)
            if found is not None:
                return found
    return None


def _elig_name(elig: dict, first_key: str, last_key: str) -> str | None:
    first = _find_key(elig, first_key)
    last = _find_key(elig, last_key)
    name = " ".join(str(x).strip() for x in (first, last) if x and str(x).strip())
    return name or None


def _xml_borrower_names(state: dict) -> list[str]:
    raw = state.get("loan_file_xml") or ""
    if not isinstance(raw, str) or not raw.strip():
        return []
    try:
        profile = parse_mismo_xml(raw)
        return [n for n in (profile.get("borrower_names") or []) if n]
    except Exception as e:  # noqa: BLE001 — never break the pass on XML issues
        logger.warning("Loan XML borrower-name parse failed: %s", e)
        return []


def resolve_parties(state: dict) -> list[dict]:
    """Resolve the loan's parties as [{"name","role","tag"}].

    Identity order (elig-then-xml):
      - primary: scenario_summary.borrowers[primary] -> eligibility
        borrower_first/last_name -> loan XML BORROWER_1.
      - co-borrower: scenario_summary.borrowers[co-borrower] -> eligibility
        CoBorrowerFirstName/LastName -> loan XML BORROWER_2.

    Returns a single primary when no distinct co-borrower is found.
    """
    ss = state.get("scenario_summary", {}) or {}
    primary: str | None = None
    co: str | None = None
    for b in ss.get("borrowers") or []:
        name = (b.get("name") or "").strip()
        if not name or name.lower() == "unknown":
            continue
        role = (b.get("role") or "").lower()
        if "co" in role:
            co = co or name
        elif not primary:
            primary = name

    elig = _load_eligibility(state)
    if not primary:
        primary = (
            _elig_name(elig, "borrower_first_name", "borrower_last_name")
            or _elig_name(elig, "BorrowerFirstName", "BorrowerLastName")
        )
    if not co:
        co = (
            _elig_name(elig, "CoBorrowerFirstName", "CoBorrowerLastName")
            or _elig_name(elig, "co_borrower_first_name", "co_borrower_last_name")
        )

    if not primary or not co:
        xml_names = _xml_borrower_names(state)
        if not primary and xml_names:
            primary = xml_names[0]
        if not co and len(xml_names) > 1:
            co = xml_names[1]

    parties: list[dict] = []
    if primary:
        parties.append({"name": primary, "role": "primary", "tag": "borrower"})
    if co and _norm_name(co) != _norm_name(primary or ""):
        parties.append({"name": co, "role": "co-borrower", "tag": "coborrower"})
    return parties


# ---------------------------------------------------------------------------
# Document -> party assignment (fuzzy name matching)
# ---------------------------------------------------------------------------


def _doc_party_names(doc: dict) -> list[str]:
    """Pull the person name(s) recorded on a parsed manifest document.

    Different document types nest the person's name under different keys
    depending on the extraction schema — e.g. ``customer`` for income docs,
    ``owner`` for IDs (driver's license), ``applicant1``/``applicant2`` for
    multi-borrower credit reports, plain ``borrowers``/entries for others.
    Rather than hardcode every schema (and silently miss the next one — this
    is exactly how a driver's license's ``owner`` name went undetected and
    the document fell back to "assign to ALL parties", satisfying the
    co-borrower's photo-ID requirement with the primary borrower's license),
    recursively scan extracted_fields for any dict carrying a real
    (non-placeholder) firstName+lastName pair, wherever it's nested.

    Placeholder entries (e.g. a credit report's ``alerts[].applicant`` stub
    with every field ``null``) are naturally skipped since firstName/lastName
    aren't non-empty strings there.
    """
    ef = doc.get("extracted_fields", {}) or {}
    names: list[str] = []

    def _walk(obj: Any) -> None:
        if isinstance(obj, dict):
            first = obj.get("firstName")
            last = obj.get("lastName")
            if isinstance(first, str) and isinstance(last, str) and first.strip() and last.strip():
                middle = obj.get("middleName") or ""
                parts = [first.strip(), str(middle).strip(), last.strip()]
                nm = " ".join(p for p in parts if p)
                if nm:
                    names.append(nm)
            for v in obj.values():
                _walk(v)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item)

    _walk(ef)

    # A couple of schemas store the name as a single already-joined field
    # rather than firstName/lastName parts.
    cust = ef.get("customer")
    if isinstance(cust, str) and cust.strip():
        names.append(cust.strip())
    for b in ef.get("borrowers") or []:
        if isinstance(b, str) and b.strip():
            names.append(b.strip())

    return names


def assign_docs_to_parties(
    submitted_docs: list[dict],
    parties: list[dict],
) -> dict[str, list[dict]]:
    """Assign each submitted document to one or more parties by fuzzy name match.

    Rules:
      - A document naming a party goes to that party (or both, if it names both).
      - A document with NO name (loan-level, e.g. appraisal/title) goes to ALL
        parties, so each party's loan-level conditions can be satisfied.
      - A document that names someone but matches no known party also goes to
        ALL parties (we cannot disambiguate, so we do not drop it).
    """
    names = [p["name"] for p in parties]
    result: dict[str, list[dict]] = {n: [] for n in names}

    for doc in submitted_docs:
        doc_names = _doc_party_names(doc)
        if not doc_names:
            for n in names:
                result[n].append(doc)
            continue

        matched = {
            pn for dn in doc_names for pn in names if _name_matches(dn, pn)
        }
        if not matched:
            matched = set(names)
        for pn in matched:
            result[pn].append(doc)

    return result


def apply_coborrower_pass(state: dict) -> dict:
    """Post-pipeline pass. Auto-detects the loan's parties and builds a full
    condition set per party, each satisfied only by that party's documents.

    Loan-level (non-borrower-specific) document types — appraisal, title,
    hazard insurance, wire transfer, etc., per data/document_party_scope.json
    — are NOT split per party: one shared document covers every party on the
    loan, so duplicating it under both ``party="borrower"`` and
    ``party="coborrower"`` would just be two identical copies. Those get
    ``party=None`` / ``applicable_parties=None`` instead, straight from the
    already-computed (whole-submitted-doc-set) borrower_final entry. Only
    borrower-specific types (IDs, tax docs, paystubs, etc.) get the per-party
    split below.

    Single-party loans keep today's behavior for borrower-specific docs (all
    tagged ``party="borrower"``); shared docs are still ``party=None`` even
    then, since "not tied to a specific person" doesn't depend on how many
    people are on the loan. Returns a state update dict ({} when there is
    nothing to do). Never raises — on any error it falls back to the
    borrower output.
    """
    final_output = dict(state.get("final_output") or {})
    borrower_final = list(final_output.get("document_requests") or [])
    if not borrower_final:
        return {}

    scenario_summary = state.get("scenario_summary", {}) or {}
    canonical = (
        state.get("module_outputs", {})
        .get("08", {})
        .get("ranked_document_requests", [])
    )
    submitted_docs = scenario_summary.get("_submitted_docs", []) or []

    # Loan-level docs: one shared copy, already satisfied against the full
    # submitted-doc set, not attributed to any one party.
    shared_docs: list[dict] = []
    borrower_specific_final: list[dict] = []
    for dr in borrower_final:
        dr = dict(dr)
        if is_borrower_specific(dr.get("document_type", "")):
            borrower_specific_final.append(dr)
        else:
            dr["party"] = None
            dr["applicable_parties"] = None
            shared_docs.append(dr)

    try:
        parties = resolve_parties(state)
    except Exception as e:  # noqa: BLE001 — never break borrower output
        logger.warning("Party resolution failed: %s", e)
        parties = []

    if len(parties) < 2 or not canonical:
        # Single-party loan (or no canonical set to rebuild from): tag the
        # existing borrower-specific set and, when known, stamp the
        # primary's name. Shared docs stay party=None regardless.
        primary_name = parties[0]["name"] if parties else ""
        for dr in borrower_specific_final:
            dr.setdefault("party", "borrower")
            if primary_name and not dr.get("applicable_parties"):
                dr["applicable_parties"] = [primary_name]
        combined = shared_docs + borrower_specific_final
    else:
        canonical_borrower_specific = [
            dr for dr in canonical
            if is_borrower_specific(dr.get("document_type", ""))
        ]
        assignments = assign_docs_to_parties(submitted_docs, parties)
        per_party: list[dict] = []
        for party in parties:
            party_docs = assignments.get(party["name"], [])
            try:
                per_party.extend(
                    build_party_document_requests(
                        canonical_borrower_specific, borrower_specific_final,
                        party_docs, party["name"], party["tag"], scenario_summary,
                    )
                )
            except Exception as e:  # noqa: BLE001 — never break borrower output
                logger.warning(
                    "Party document build failed for %s: %s", party.get("name"), e
                )
        if not per_party and borrower_specific_final:
            # Fall back to the untouched borrower-specific set on total failure.
            for dr in borrower_specific_final:
                dr.setdefault("party", "borrower")
            per_party = borrower_specific_final
        combined = shared_docs + per_party

    # Surface the party tag inside `display` (the frontend renders only display).
    for dr in combined:
        _fold_party_into_display(dr)

    final_output["document_requests"] = combined

    # Refresh stats, adding a per-party breakdown ("shared" for loan-level docs).
    stats = dict(final_output.get("stats") or {})
    by_party: dict[str, int] = {}
    for dr in combined:
        p = dr.get("party") or "shared"
        by_party[p] = by_party.get(p, 0) + 1
    stats["total_document_requests"] = len(combined)
    stats["by_party"] = by_party
    final_output["stats"] = stats

    return {"final_output": final_output}
