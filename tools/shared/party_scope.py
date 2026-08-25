"""
party_scope.py — Classifies each canonical document type as borrower-specific
(each party needs their own copy — IDs, tax returns, paystubs, etc.) or
loan-level / non-borrower-specific (one shared document covers every party on
the loan — appraisal, title, hazard insurance, etc.).

Source of truth: data/document_party_scope.json — a business classification
of the same 172-document universe as data/nqm_document_types.json (see
docs/API_USAGE.md), kept as a separate lookup file since it encodes an
internal party-attribution decision rather than the externally-documented
accepted-document catalog.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_DATA_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "document_party_scope.json"

# A couple of output-facing labels don't appear verbatim in the
# classification data — tools/shared/normalize.py renames a few canonical
# masterlist names at the final-output boundary. Map those back to the
# masterlist name the classification data uses.
_ALIAS_TO_CANONICAL: dict[str, str] = {
    "current lease agreement": "rental agreement",
    "initial loan application (1003)": "loan application (1003)",
    "borrower authorization": "borrowers authorization",
}

# Names used directly as canonical document_type throughout the pipeline
# (see tools/doc_rules.py) that either aren't in the 172-document masterlist
# universe, or are an umbrella over several masterlist types:
#   - "Government-Issued Photo ID" covers Drivers License / Passport /
#     Non-Driver ID / ITIN / Permanent Resident Card / Social Security ID /
#     Travel VISA (see normalize.py's _MULTI_TYPE_DOCS) — all of them are
#     borrower-specific in the classification data.
#   - "Loan Application (1003)" is a Cross-Cutting requirement synthesized
#     by doc_rules.py, not a Tasktile category, so it has no masterlist
#     entry — but each borrower/co-borrower signs and is named on their own
#     1003, so it's explicitly borrower-specific rather than relying on the
#     default fallback.
_KNOWN_BORROWER_SPECIFIC = {"government-issued photo id", "loan application (1003)"}


@lru_cache(maxsize=1)
def _load_scope_map() -> dict[str, bool]:
    try:
        raw = json.loads(_DATA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {k.strip().lower(): bool(v) for k, v in raw.items()}


def is_borrower_specific(document_type: str) -> bool:
    """True when *document_type* needs its own copy per borrower/co-borrower
    (IDs, tax returns, paystubs, etc.); False when one shared document
    covers the whole loan (appraisal, title, hazard insurance, etc.).

    Works on either the pre-rename canonical masterlist name (e.g. "Rental
    Agreement") or the output-facing display name (e.g. "Current Lease
    Agreement") — see ``_ALIAS_TO_CANONICAL``.

    Falls back to True (borrower-specific — i.e. today's per-party split
    behavior) for any name not found in the classification data, so an
    unclassified/unexpected document_type never gets silently collapsed
    into a shared, unattributed entry.
    """
    name = (document_type or "").strip().lower()
    if not name:
        return True
    if name in _KNOWN_BORROWER_SPECIFIC:
        return True
    canonical = _ALIAS_TO_CANONICAL.get(name, name)
    return _load_scope_map().get(canonical, True)
