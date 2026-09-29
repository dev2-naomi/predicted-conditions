"""generate_conditions_excel.py

Generates an Excel workbook enumerating ALL possible conditions/specifications
for each document type, one tab per document type.

Two kinds of tabs:
  1. STANDARDIZED types (the 24 covered by data/canonical_doc_specs.json,
     the deterministic library used by apply_guideline_canonicalization()
     since the "standardize inner-items list per doc type" fix). For these,
     the sheet lists literally every spec+condition that library can ever
     produce for that doc type -- the authoritative "all possible
     conditions" list -- cross-checked against what actually showed up in
     the post-fix 10x-consistency-check runs (consistency_check/*.json) as
     a sanity check.
  2. NOT-YET-STANDARDIZED types (everything else the LLM has generated
     freeform across those same runs). For these there's no canonical
     library yet, so the sheet lists every unique spec text observed
     across the sampled runs, with an occurrence/consistency rate --
     useful for spotting which doc types are next in line for
     standardization (specs with <100% consistency across reruns of the
     same input are the ones drifting).

Plus an "Overview" tab summarizing every document type.

Usage:
    python3 generate_conditions_excel.py [output.xlsx]
"""
from __future__ import annotations

import glob
import json
import sys
from collections import defaultdict

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from tools.merger_tools import _canonical_doc_type

CANONICAL_PATH = "data/canonical_doc_specs.json"
RUNS_GLOB = "consistency_check/*.json"
DEFAULT_OUT = "all_conditions_by_document.xlsx"

HEADER_FILL = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
HEADER_FONT = Font(color="FFFFFF", bold=True)
MISSING_FILL = PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid")
EXTRA_FILL = PatternFill(start_color="F8CBAD", end_color="F8CBAD", fill_type="solid")
TITLE_FONT = Font(bold=True, size=13)
WRAP = Alignment(wrap_text=True, vertical="top")


def _norm(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def load_canonical() -> dict:
    with open(CANONICAL_PATH) as f:
        return json.load(f)


def load_run_data() -> tuple[dict, dict, dict]:
    """Scan consistency_check/*.json (post-fix 10x runs).

    Returns:
      spec_counts[canon_key][spec_text] = occurrence count (# doc-instances
          containing that exact spec text)
      instance_counts[canon_key] = total # doc-instances of that type seen
      raw_names[canon_key] = set of raw document_type strings observed
    """
    spec_counts: dict = defaultdict(lambda: defaultdict(int))
    instance_counts: dict = defaultdict(int)
    raw_names: dict = defaultdict(set)

    for path in sorted(glob.glob(RUNS_GLOB)):
        if path.endswith("_summary.json"):
            continue
        with open(path) as f:
            data = json.load(f)
        runs = data.get("runs", [])
        # kashana_postfix_verification.json nests differently; handle both.
        if not runs and isinstance(data, dict):
            runs = data.get("runs", [])
        for run in runs:
            for dr in run.get("document_requests", []) or []:
                raw_type = dr.get("document_type") or ""
                if not raw_type:
                    continue
                canon_key = _canonical_doc_type(raw_type)
                raw_names[canon_key].add(raw_type)
                instance_counts[canon_key] += 1
                seen_this_instance = set()
                for spec in dr.get("specifications", []) or []:
                    if not isinstance(spec, str) or not spec.strip():
                        continue
                    norm = _norm(spec)
                    if norm in seen_this_instance:
                        continue
                    seen_this_instance.add(norm)
                    spec_counts[canon_key][spec.strip()] += 1

    return spec_counts, instance_counts, raw_names


def _autosize(ws: Worksheet, widths: list[int]) -> None:
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w


def _write_header(ws: Worksheet, headers: list[str], row: int = 1) -> None:
    for i, h in enumerate(headers, start=1):
        c = ws.cell(row=row, column=i, value=h)
        c.fill = HEADER_FILL
        c.font = HEADER_FONT
        c.alignment = Alignment(vertical="center")
    ws.freeze_panes = f"A{row + 1}"


def _sheet_title(name: str, used: set) -> str:
    # Excel sheet names: max 31 chars, no []:*?/\\
    safe = "".join(ch for ch in name.title() if ch not in '[]:*?/\\')
    safe = safe[:31] or "Sheet"
    base = safe
    n = 2
    while safe.lower() in used:
        suffix = f" {n}"
        safe = (base[: 31 - len(suffix)] + suffix)
        n += 1
    used.add(safe.lower())
    return safe


def build_workbook(out_path: str) -> None:
    canonical = load_canonical()
    canonical_keys = set(canonical.keys())
    spec_counts, instance_counts, raw_names = load_run_data()

    all_canon_keys = set(canonical_keys) | set(spec_counts.keys())

    wb = Workbook()
    overview_ws = wb.active
    overview_ws.title = "Overview"

    used_titles = {"overview"}

    overview_rows = []

    # Sort: standardized types first (alpha), then not-yet-standardized (alpha)
    standardized = sorted(k for k in all_canon_keys if k in canonical_keys)
    not_standardized = sorted(k for k in all_canon_keys if k not in canonical_keys)

    for key in standardized:
        entry = canonical.get(key, {})
        items = entry.get("items", [])
        headings = entry.get("source_headings", [])
        observed = spec_counts.get(key, {})
        observed_norm = {_norm(t): (t, c) for t, c in observed.items()}
        total_instances = instance_counts.get(key, 0)
        raws = raw_names.get(key, set())

        sheet_name = _sheet_title(key, used_titles)
        ws = wb.create_sheet(sheet_name)
        ws.cell(row=1, column=1, value=f"{key.title()}  (STANDARDIZED — data/canonical_doc_specs.json)").font = TITLE_FONT
        ws.cell(row=2, column=1, value=f"Raw document_type label(s) seen: {', '.join(sorted(raws)) or '(none observed in sampled runs)'}")
        ws.cell(row=3, column=1, value=f"Source guideline heading(s): {', '.join(headings) or '—'}")
        ws.cell(row=4, column=1, value=(
            f"Instances observed across post-fix 10x runs: {total_instances}. "
            "Every row below is a possible spec+condition; 'condition' is a flag "
            "expression from the guideline library (blank = always applies)."
        ))
        header_row = 6
        _write_header(ws, ["#", "Condition / Specification Text", "Applies When (condition)", "Seen In Sampled Runs?", "Occurrence Count"], row=header_row)

        r = header_row + 1
        matched_norms = set()
        for idx, item in enumerate(items, start=1):
            text = item.get("text", "")
            cond = item.get("condition") or "(always)"
            norm = _norm(text)
            matched = norm in observed_norm
            if matched:
                matched_norms.add(norm)
            count = observed_norm.get(norm, (None, 0))[1]
            ws.cell(row=r, column=1, value=idx)
            tcell = ws.cell(row=r, column=2, value=text)
            tcell.alignment = WRAP
            ccell = ws.cell(row=r, column=3, value=cond)
            ccell.alignment = WRAP
            seen_cell = ws.cell(row=r, column=4, value="Yes" if matched else ("N/A (0 instances)" if total_instances == 0 else "Not seen"))
            ws.cell(row=r, column=5, value=count)
            if not matched and total_instances > 0:
                for col in range(1, 6):
                    ws.cell(row=r, column=col).fill = MISSING_FILL
            r += 1

        # Extras: specs observed in runs but NOT in the canonical library
        extras = [(t, c) for norm, (t, c) in observed_norm.items() if norm not in matched_norms]
        if extras:
            r += 1
            ws.cell(row=r, column=1, value="⚠ Observed in runs but NOT in canonical library:").font = Font(bold=True, italic=True)
            r += 1
            for t, c in sorted(extras, key=lambda x: -x[1]):
                tcell = ws.cell(row=r, column=2, value=t)
                tcell.alignment = WRAP
                ws.cell(row=r, column=5, value=c)
                for col in range(1, 6):
                    ws.cell(row=r, column=col).fill = EXTRA_FILL
                r += 1

        _autosize(ws, [4, 90, 40, 20, 16])

        overview_rows.append([
            key.title(), "Yes", len(items), total_instances, len(extras), sheet_name,
        ])

    for key in not_standardized:
        observed = spec_counts.get(key, {})
        total_instances = instance_counts.get(key, 0)
        raws = raw_names.get(key, set())
        display_name = sorted(raws)[0] if raws else key.title()

        sheet_name = _sheet_title(key, used_titles)
        ws = wb.create_sheet(sheet_name)
        ws.cell(row=1, column=1, value=f"{display_name}  (NOT YET STANDARDIZED — freeform LLM output)").font = TITLE_FONT
        ws.cell(row=2, column=1, value=f"Raw document_type label(s) seen: {', '.join(sorted(raws)) or '(none)'}")
        ws.cell(row=3, column=1, value=(
            f"Instances observed across post-fix 10x runs: {total_instances}. No canonical "
            "library entry exists yet for this doc type, so specs below are whatever the LLM "
            "generated (may vary run-to-run for the same input — lower consistency % = higher "
            "priority for standardization)."
        ))
        header_row = 5
        _write_header(ws, ["#", "Observed Specification / Condition Text", "Occurrence Count", "Consistency % (of instances)"], row=header_row)

        r = header_row + 1
        for idx, (text, count) in enumerate(sorted(observed.items(), key=lambda x: -x[1]), start=1):
            pct = (count / total_instances * 100) if total_instances else 0
            ws.cell(row=r, column=1, value=idx)
            tcell = ws.cell(row=r, column=2, value=text)
            tcell.alignment = WRAP
            ws.cell(row=r, column=3, value=count)
            pcell = ws.cell(row=r, column=4, value=round(pct, 1))
            if pct < 100:
                for col in range(1, 5):
                    ws.cell(row=r, column=col).fill = MISSING_FILL
            r += 1

        _autosize(ws, [4, 100, 16, 22])

        overview_rows.append([
            display_name, "No", 0, total_instances, len(observed), sheet_name,
        ])

    # ---- Overview sheet ----
    overview_ws.cell(row=1, column=1, value="All Document Types — Conditions Overview").font = TITLE_FONT
    overview_ws.cell(row=2, column=1, value=(
        f"Standardized types: {len(standardized)}  |  Not-yet-standardized types: {len(not_standardized)}  "
        f"|  Source: {CANONICAL_PATH} + {RUNS_GLOB}"
    ))
    _write_header(overview_ws, [
        "Document Type", "Standardized?", "# Canonical Conditions", "# Instances In Sampled Runs",
        "# Extra/Unmatched Conditions Observed", "Detail Tab",
    ], row=4)
    r = 5
    overview_rows.sort(key=lambda row: (row[1] != "Yes", row[0]))
    for row in overview_rows:
        for col, val in enumerate(row, start=1):
            cell = overview_ws.cell(row=r, column=col, value=val)
            if col == 2 and val == "No":
                cell.fill = MISSING_FILL
        r += 1
    _autosize(overview_ws, [45, 14, 20, 22, 28, 24])

    wb.save(out_path)
    print(f"Wrote {out_path}")
    print(f"  Standardized doc-type tabs: {len(standardized)}")
    print(f"  Not-yet-standardized doc-type tabs: {len(not_standardized)}")


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_OUT
    build_workbook(out)
