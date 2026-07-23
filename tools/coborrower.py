"""
coborrower.py — Optional second document-needs set for a co-borrower.

Runs as a post-pipeline graph node (after STEP_09) when the caller supplies an
optional ``coborrower`` object. The co-borrower "just includes a manifest"
(loan file optional; eligibility shared with the borrower), so we do NOT re-run
the full LLM pipeline. Instead we:

  1. Take the borrower's canonical required-doc set (module_outputs["08"]).
  2. Restore the full specification list (satisfied specs folded back in) and
     re-run the SAME satisfaction logic against the co-borrower's manifest.
  3. Recompute status (needed vs already-submitted) against that manifest.
  4. Rebuild the AUS display block (reusing the borrower's styled headings /
     reasons, with a freshly computed satisfied-vs-needed split).
  5. Apply the same output display-name rename and tag ``party="coborrower"``.

The result is appended to the single ``final_output.document_requests`` array;
borrower requests are tagged ``party="borrower"``. This matches the chosen
"single array tagged by party" output shape.
"""

from __future__ import annotations

import copy
import json
import logging
from typing import Any

from tools.merger_tools import assign_statuses, run_satisfaction_pass
from tools.presentation_tools import _aus_restyle_spec
from tools.shared.manifest_parser import parse_manifest_from_string
from tools.shared.normalize import (
    apply_output_display_name,
    normalize_document_structure,
)

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

    return display


def build_coborrower_document_requests(
    borrower_canonical_docs: list[dict],
    borrower_final_docs: list[dict],
    coborrower_manifest_raw: str,
    scenario_summary: dict,
) -> list[dict]:
    """Produce the co-borrower's document_requests (tagged party='coborrower').

    Args:
        borrower_canonical_docs: pre-rename ranked docs (module_outputs["08"]);
            canonical names so alias/satisfaction matching works.
        borrower_final_docs: the styled final docs (renamed labels + display),
            used only to reuse heading/reason/review-note styling.
        coborrower_manifest_raw: the co-borrower's raw manifest JSON string.
        scenario_summary: shared scenario (loan facts + eligibility).
    """
    cb_submitted = parse_manifest_from_string(coborrower_manifest_raw)

    docs = copy.deepcopy(borrower_canonical_docs)
    for dr in docs:
        _restore_full_specifications(dr)

    # Re-evaluate satisfaction + status against the co-borrower's own manifest.
    run_satisfaction_pass(docs, cb_submitted, scenario_summary)
    assign_statuses(docs, cb_submitted)

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
        dr["party"] = "coborrower"
        borrower_disp = final_display_by_type.get(
            (dr.get("document_type") or "").strip().lower()
        )
        dr["display"] = _rebuild_display(dr, borrower_disp)
        out.append(dr)

    return out


def _coborrower_manifest(coborrower: Any) -> str:
    """Extract the co-borrower manifest JSON string from the input object.

    Accepts the co-borrower object as a dict or as a JSON-encoded string, and
    the manifest itself either as a raw JSON string or an inline object.
    """
    if isinstance(coborrower, str):
        try:
            coborrower = json.loads(coborrower)
        except (json.JSONDecodeError, TypeError):
            return ""
    if not isinstance(coborrower, dict):
        return ""
    m = coborrower.get("manifest_json") or coborrower.get("manifest") or ""
    if isinstance(m, dict):
        return json.dumps(m)
    return m if isinstance(m, str) else ""


def apply_coborrower_pass(state: dict) -> dict:
    """Post-pipeline pass. Tags borrower docs and, when a co-borrower manifest
    is supplied, appends the co-borrower's document set.

    Returns a state update dict ({} when there is nothing to do). Never raises —
    on any error it falls back to leaving the borrower output untouched.
    """
    final_output = dict(state.get("final_output") or {})
    borrower_final = list(final_output.get("document_requests") or [])
    if not borrower_final:
        return {}

    # Always tag the primary set.
    for dr in borrower_final:
        dr.setdefault("party", "borrower")

    coborrower = state.get("coborrower")
    cb_manifest = _coborrower_manifest(coborrower)

    cb_docs: list[dict] = []
    if cb_manifest:
        try:
            canonical = (
                state.get("module_outputs", {})
                .get("08", {})
                .get("ranked_document_requests", [])
            )
            if canonical:
                cb_docs = build_coborrower_document_requests(
                    canonical, borrower_final, cb_manifest,
                    state.get("scenario_summary", {}),
                )
        except Exception as e:  # noqa: BLE001 — never break borrower output
            logger.warning("Co-borrower pass failed: %s", e)
            cb_docs = []

    combined = borrower_final + cb_docs
    final_output["document_requests"] = combined

    # Refresh stats, adding a per-party breakdown.
    stats = dict(final_output.get("stats") or {})
    by_party: dict[str, int] = {}
    for dr in combined:
        p = dr.get("party", "borrower")
        by_party[p] = by_party.get(p, 0) + 1
    stats["total_document_requests"] = len(combined)
    stats["by_party"] = by_party
    final_output["stats"] = stats

    return {"final_output": final_output}
