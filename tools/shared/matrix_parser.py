"""
matrix_parser.py — Parses program_matrices.md into searchable sections
keyed by program name.

The markdown file uses level-1 headings (# **Program Name**) to delimit
each program's matrix.  Everything between two consecutive program headings
belongs to that program.  The first section (before any program heading)
contains shared/general requirements that apply to all programs.

Also provides deterministic parsers that extract structured data from the
markdown tables — LTV/FICO grids, reserve brackets, DTI caps, loan amount
ranges, borrower eligibility, and FTHB limits — so that numeric checks
can run without an LLM.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

_MATRICES_PATH = Path(__file__).parent.parent.parent / "data" / "program_matrices.md"

_PROGRAM_HEADING = re.compile(r"^#\s+\*\*(.+?)\*\*\s*$")

# Canonical program name → list of aliases/variants we might see in XML/JSON
_PROGRAM_ALIASES: dict[str, list[str]] = {
    "Flex Supreme":           ["flex supreme", "flexsupreme", "flex_supreme"],
    "Flex Select":            ["flex select", "flexselect", "flex_select"],
    "Select ITIN":            ["select itin", "selectitin", "select_itin", "itin"],
    "Super Jumbo":            ["super jumbo", "superjumbo", "super_jumbo"],
    "Second Lien Select":     ["second lien select", "secondlienselect", "second_lien_select", "2nd lien select"],
    "DSCR Supreme":           ["dscr supreme", "dscrsupreme", "dscr_supreme"],
    "Investor DSCR":          ["investor dscr", "investordscr", "investor_dscr"],
    "Investor DSCR No Ratio": ["investor dscr no ratio", "investordscrnoratio", "investor_dscr_no_ratio", "dscr no ratio"],
    "DSCR Multi 5-8":         ["dscr multi 5-8", "dscr multi", "dscrmulti", "dscr_multi"],
    "Foreign National":       ["foreign national", "foreignnational", "foreign_national"],
}

_ALIAS_TO_CANONICAL: dict[str, str] = {}
for canonical, aliases in _PROGRAM_ALIASES.items():
    _ALIAS_TO_CANONICAL[canonical.lower()] = canonical
    for alias in aliases:
        _ALIAS_TO_CANONICAL[alias.lower()] = canonical


# ───────────────────────────────────────────────────────────────────────────
# Structured data types for deterministic checks
# ───────────────────────────────────────────────────────────────────────────

@dataclass
class GridRow:
    min_fico: int
    max_loan: int
    max_ltv_purchase: int | None
    max_ltv_cashout: int | None

@dataclass
class ReserveBracket:
    max_loan: int
    months: int

@dataclass
class ProgramLimits:
    min_fico: int | None = None
    min_loan: int | None = None
    max_loan: int | None = None
    max_dti: int | None = None
    fthb_max_loan: int | None = None
    ineligible_borrowers: list[str] = field(default_factory=list)
    eligible_borrowers: list[str] = field(default_factory=list)


# ───────────────────────────────────────────────────────────────────────────
# Helpers for cleaning markdown cell values
# ───────────────────────────────────────────────────────────────────────────

_STRIP_MD = re.compile(r"~~|<br\s*/?>|\*\*|\*")
_MONEY_RE = re.compile(r"\$?([\d,]+)")
_PCT_RE = re.compile(r"(\d+)\s*%")
_NUM_RE = re.compile(r"(\d[\d,]*)")

# Occupancy sub-table header markers
_OCCUPANCY_LABELS: dict[str, str] = {
    "primary residence": "primary_residence",
    "second home": "second_home",
    "investment property": "investment",
    "investment": "investment",
}

_PAGE_FOOTER_RE = re.compile(
    r"^\d+\s*\|\s*P\s*a\s*g\s*e|"
    r"^.+Matrix\s+\d+\s+\d+\s+\d{4}|"
    r"^\d{1,2}/\d{1,2}/\d{4}$",
    re.IGNORECASE,
)


def _clean_cell(cell: str) -> str:
    """Strip markdown formatting from a table cell."""
    return _STRIP_MD.sub("", cell).strip()


def _parse_int(text: str) -> int | None:
    """Extract the first integer from text, stripping $ and commas."""
    cleaned = _clean_cell(text)
    if not cleaned or cleaned.upper() == "N/A":
        return None
    m = _MONEY_RE.search(cleaned)
    if m:
        return int(m.group(1).replace(",", ""))
    m = _NUM_RE.search(cleaned)
    if m:
        return int(m.group(1).replace(",", ""))
    return None


def _parse_pct(text: str) -> int | None:
    cleaned = _clean_cell(text)
    if not cleaned or cleaned.upper() == "N/A":
        return None
    m = _PCT_RE.search(cleaned)
    if m:
        return int(m.group(1))
    m = _NUM_RE.search(cleaned)
    if m:
        val = int(m.group(1).replace(",", ""))
        if val <= 100:
            return val
    return None


def _is_separator_row(cells: list[str]) -> bool:
    """True for |---|---|---| rows."""
    return all(c.strip().replace("-", "") == "" for c in cells if c.strip())


def _split_table_row(line: str) -> list[str]:
    """Split a markdown table row into cells, stripping outer pipes."""
    parts = line.split("|")
    if parts and not parts[0].strip():
        parts = parts[1:]
    if parts and not parts[-1].strip():
        parts = parts[:-1]
    return parts


def _detect_occupancy(cells: list[str]) -> str | None:
    """If every non-empty cell in a row has the same occupancy label, return it."""
    labels = set()
    for c in cells:
        cleaned = _clean_cell(c).lower().strip()
        if not cleaned:
            continue
        for marker, key in _OCCUPANCY_LABELS.items():
            if marker in cleaned:
                labels.add(key)
                break
    if len(labels) == 1:
        return labels.pop()
    return None


# ───────────────────────────────────────────────────────────────────────────
# Main class
# ───────────────────────────────────────────────────────────────────────────

class ProgramMatrix:
    """Parsed representation of the program matrices document."""

    def __init__(self, filepath: str | Path | None = None):
        self.filepath = Path(filepath) if filepath else _MATRICES_PATH
        self._raw: str = ""
        self._general_section: str = ""
        self._program_sections: dict[str, str] = {}
        self._load()

    def _load(self):
        self._raw = self.filepath.read_text(encoding="utf-8")
        lines = self._raw.splitlines(keepends=True)

        current_program: str | None = None
        current_lines: list[str] = []

        for line in lines:
            m = _PROGRAM_HEADING.match(line.strip())
            if m:
                heading = m.group(1).strip()
                if "PROGRAM MATRICES" in heading.upper():
                    current_lines.append(line)
                    continue
                if current_program is None:
                    self._general_section = "".join(current_lines)
                else:
                    self._program_sections[current_program] = "".join(current_lines)
                current_program = heading
                current_lines = [line]
            else:
                current_lines.append(line)

        if current_program is not None:
            self._program_sections[current_program] = "".join(current_lines)
        else:
            self._general_section = "".join(current_lines)

    # ── basic accessors ───────────────────────────────────────────────────

    @property
    def program_names(self) -> list[str]:
        return list(self._program_sections.keys())

    @property
    def general_section(self) -> str:
        return self._general_section

    def resolve_program_name(self, raw_name: str) -> str | None:
        if not raw_name:
            return None
        normalized = raw_name.strip().lower()
        if normalized in _ALIAS_TO_CANONICAL:
            return _ALIAS_TO_CANONICAL[normalized]
        for alias, canonical in _ALIAS_TO_CANONICAL.items():
            if alias in normalized or normalized in alias:
                return canonical
        return None

    def get_program_section(self, program_name: str) -> str | None:
        return self._program_sections.get(program_name)

    def get_program_matrix(self, raw_name: str) -> tuple[str | None, str]:
        """Full combined text (general + program). Used by the LLM path."""
        canonical = self.resolve_program_name(raw_name)
        if not canonical:
            return None, ""
        section = self._program_sections.get(canonical, "")
        if not section:
            return canonical, ""
        combined = (
            f"## GENERAL REQUIREMENTS (All Programs)\n\n"
            f"{self._general_section}\n\n"
            f"---\n\n"
            f"## {canonical} — Program Matrix\n\n"
            f"{section}"
        )
        return canonical, combined

    # ── deterministic parsers ────────────────────────────────────────────

    def parse_ltv_grid(self, raw_name: str) -> dict[str, list[GridRow]]:
        """
        Parse the LTV/FICO grid tables for a resolved program.
        Returns {occupancy_key: [GridRow, ...]} where occupancy_key is
        'primary_residence', 'second_home', or 'investment'.
        """
        canonical = self.resolve_program_name(raw_name)
        if not canonical:
            return {}
        section = self._program_sections.get(canonical, "")
        if not section:
            return {}

        grids: dict[str, list[GridRow]] = {}
        current_occ: str | None = None
        in_grid = False
        last_fico: int | None = None

        for line in section.splitlines():
            if "|" not in line:
                # A run of blank lines between the grid and GENERAL REQUIREMENTS
                # means we've left the grid tables
                if in_grid and not line.strip():
                    continue
                if in_grid and line.strip() and "|" not in line:
                    in_grid = False
                continue

            cells = _split_table_row(line)
            if _is_separator_row(cells):
                continue

            # Detect occupancy header rows
            occ = _detect_occupancy(cells)
            if occ:
                current_occ = occ
                if current_occ not in grids:
                    grids[current_occ] = []
                in_grid = True
                last_fico = None
                continue

            # Skip column-header rows (contain "Min Credit Score" etc.)
            first_clean = _clean_cell(cells[0]).lower() if cells else ""
            if "credit score" in first_clean or "min" in first_clean and "max" in _clean_cell(cells[1]).lower() if len(cells) > 1 else False:
                continue

            if not current_occ or not in_grid:
                continue

            # Data row: |FICO|MaxLoan|MaxLTV_Purchase|MaxLTV_CashOut|...|
            if len(cells) < 4:
                continue

            fico = _parse_int(cells[0])
            if fico and 300 <= fico <= 900:
                last_fico = fico
            elif not fico or fico < 300 or fico > 900:
                fico = last_fico

            max_loan = _parse_int(cells[1])
            ltv_purchase = _parse_int(cells[2])
            ltv_cashout = _parse_int(cells[3])

            # Validate: max_loan must be plausible (>= $50K) and fico in range
            if fico and max_loan and max_loan >= 50000 and 300 <= fico <= 900:
                grids[current_occ].append(GridRow(
                    min_fico=fico,
                    max_loan=max_loan,
                    max_ltv_purchase=ltv_purchase,
                    max_ltv_cashout=ltv_cashout,
                ))

        return grids

    def parse_reserves(self, raw_name: str) -> list[ReserveBracket]:
        """
        Parse the reserves schedule from the program section.
        Returns sorted list of ReserveBracket(max_loan, months).
        """
        canonical = self.resolve_program_name(raw_name)
        if not canonical:
            return []
        section = self._program_sections.get(canonical, "")
        if not section:
            return []

        brackets: list[ReserveBracket] = []
        for line in section.splitlines():
            if "|" not in line:
                continue
            cells = _split_table_row(line)
            if len(cells) < 2:
                continue
            label = _clean_cell(cells[0]).lower()
            if "reserve" not in label:
                continue
            content = _clean_cell(cells[1])
            # Pattern: "<= $500,000 = 3 months" or "> $500,000 to $1,500,000 = 6 months"
            for m in re.finditer(
                r"(?:<=?\s*\$?([\d,]+)|>\s*\$?([\d,]+)\s*to\s*\$?([\d,]+))\s*=\s*(\d+)\s*months?",
                content, re.IGNORECASE,
            ):
                if m.group(1):
                    max_loan = int(m.group(1).replace(",", ""))
                else:
                    max_loan = int(m.group(3).replace(",", ""))
                months = int(m.group(4))
                brackets.append(ReserveBracket(max_loan=max_loan, months=months))

        brackets.sort(key=lambda b: b.max_loan)
        return brackets

    def parse_general_limits(self, raw_name: str) -> ProgramLimits:
        """
        Extract scalar limits: min/max loan, max DTI, min FICO,
        FTHB max loan, ineligible/eligible borrower types.
        """
        canonical = self.resolve_program_name(raw_name)
        if not canonical:
            return ProgramLimits()
        section = self._program_sections.get(canonical, "")
        if not section:
            return ProgramLimits()

        limits = ProgramLimits()

        for line in section.splitlines():
            if "|" not in line:
                continue
            cells = _split_table_row(line)
            if len(cells) < 2 or _is_separator_row(cells):
                continue

            label = _clean_cell(cells[0]).lower()
            content = _clean_cell(cells[1] if len(cells) > 1 else "")

            # Loan amount range
            if "loan amount" in label and "max" not in label:
                amounts = [int(x.replace(",", "")) for x in re.findall(r"\$?([\d,]+)", content)]
                amounts = [a for a in amounts if a >= 10000]
                if len(amounts) >= 2:
                    limits.min_loan = min(amounts)
                    limits.max_loan = max(amounts)
                elif len(amounts) == 1:
                    limits.max_loan = amounts[0]

            # Max DTI — check all cells since tables can have merged columns
            all_text = " ".join(_clean_cell(c) for c in cells).lower()
            if "max dti" in label or "max dti" in all_text:
                for c in cells:
                    pct = _parse_pct(c)
                    if pct and pct <= 100:
                        limits.max_dti = pct
                        break

            # Min credit score — scan all cells for "Minimum ... score ... NNN"
            if "credit score" in label or "credit score" in all_text:
                for c in cells:
                    m = re.search(r"[Mm]inimum\s+(?:credit\s+)?score\s+(?:of\s+)?(\d{3})", _clean_cell(c))
                    if m:
                        limits.min_fico = int(m.group(1))
                        break

            # FTHB
            if "homebuyer" in label or "fthb" in label:
                m = re.search(r"\$?([\d,]+)", content)
                if m:
                    val = int(m.group(1).replace(",", ""))
                    if val >= 100000:
                        limits.fthb_max_loan = val

            # Borrower eligibility — scan all cells
            if "borrower eligibility" in label:
                full_text = " ".join(_clean_cell(c) for c in cells)
                inelig = re.search(r"[Ii]neligible[:\s]+([^.]+)", full_text)
                if inelig:
                    raw = inelig.group(1).strip().rstrip(".")
                    # Remove trailing "See Guide..." clause
                    raw = re.sub(r"\s*See\s+Guide.*", "", raw, flags=re.IGNORECASE)
                    limits.ineligible_borrowers = [
                        b.strip() for b in re.split(r",\s*", raw) if b.strip()
                    ]
                elig = re.search(r"[Ee]ligible[:\s]+([^.]+)", full_text)
                if elig:
                    raw = elig.group(1).strip().rstrip(".")
                    raw = re.sub(r"\s*See\s+Guide.*", "", raw, flags=re.IGNORECASE)
                    # Split on comma but rejoin fragments that look like
                    # partial names (e.g. "U.S. Citizens" → keep whole)
                    limits.eligible_borrowers = [
                        b.strip() for b in re.split(r",\s+(?=[A-Z])", raw) if b.strip()
                    ]

        return limits

    # ── trimmed text for LLM ─────────────────────────────────────────────

    def get_trimmed_text(self, raw_name: str) -> str:
        """
        Return a compact version of the program matrix text suitable for
        LLM consumption. Strips:
          - LTV/FICO grid tables (handled deterministically)
          - Blank lines / page footers / date-only lines
          - Duplicate columns in requirement tables
          - Reserve and DTI rows (handled deterministically)
          - Loan amount rows (handled deterministically)
          - Borrower eligibility rows (handled deterministically)
          - FTHB rows (handled deterministically)
        """
        canonical = self.resolve_program_name(raw_name)
        if not canonical:
            return ""
        section = self._program_sections.get(canonical, "")
        if not section:
            return ""

        out_lines: list[str] = []
        in_ltv_grid = False
        skip_labels = {
            "reserve", "max dti", "loan amount",
            "borrower eligibility", "homebuyer", "fthb",
            "credit score",
        }

        for line in section.splitlines():
            stripped = line.strip()

            # Drop blank lines
            if not stripped:
                continue
            # Drop page footers and date-only lines
            if _PAGE_FOOTER_RE.match(stripped):
                continue
            # Drop the program heading (already known)
            if _PROGRAM_HEADING.match(stripped):
                continue

            # If we're inside the LTV/FICO grid, skip until we hit a
            # non-table row or a new section header
            if "|" in stripped:
                cells = _split_table_row(stripped)
                occ = _detect_occupancy(cells)
                if occ:
                    in_ltv_grid = True
                    continue
                if in_ltv_grid:
                    first = _clean_cell(cells[0]).lower() if cells else ""
                    # Still in grid if it looks like a data/header row
                    if ("credit score" in first or "max" in first
                            or _is_separator_row(cells)
                            or (_parse_int(cells[0]) is not None
                                and (_parse_int(cells[0]) or 0) >= 300)):
                        continue
                    # Section header that breaks out of the grid
                    in_ltv_grid = False

                # Skip rows whose labels we handle deterministically
                if cells and not _is_separator_row(cells):
                    label = _clean_cell(cells[0]).lower()
                    if any(sk in label for sk in skip_labels):
                        continue

                # Deduplicate columns: keep only first two non-empty columns
                if len(cells) > 2 and not _is_separator_row(cells):
                    deduped = [cells[0]]
                    for c in cells[1:]:
                        if _clean_cell(c) and _clean_cell(c) != _clean_cell(deduped[-1]):
                            deduped.append(c)
                            break
                    else:
                        deduped.append(cells[1] if len(cells) > 1 else "")
                    stripped = "|" + "|".join(deduped) + "|"

            else:
                if in_ltv_grid:
                    in_ltv_grid = False

            out_lines.append(stripped)

        return "\n".join(out_lines)

    # ── convenience: run all deterministic checks at once ────────────────

    def run_deterministic_checks(
        self,
        raw_name: str,
        *,
        fico: int | None = None,
        ltv: float | None = None,
        loan_amount: float | None = None,
        dti: float | None = None,
        occupancy: str | None = None,
        purpose: str | None = None,
        borrower_type: str | None = None,
        is_fthb: bool = False,
    ) -> list[dict[str, Any]]:
        """
        Run all deterministic matrix checks and return a list of
        condition dicts (same schema as the LLM output).
        """
        canonical = self.resolve_program_name(raw_name)
        if not canonical:
            return [{
                "condition_id": "program_not_found",
                "condition_family_id": "PROGRAM_NOT_FOUND",
                "category": "Program Eligibility",
                "title": f"Program '{raw_name}' Not Found in Matrix",
                "description": (
                    f"The loan program '{raw_name}' could not be matched to any "
                    f"known program matrix. Available: {self.program_names}"
                ),
                "severity": "HARD-STOP",
                "priority": "P0",
                "confidence": 0.95,
                "required_documents": [],
                "required_data_elements": ["loan_program"],
                "tags": ["matrix_eligibility", "deterministic"],
            }]

        conditions: list[dict[str, Any]] = []
        limits = self.parse_general_limits(raw_name)

        # ── Loan amount range ────────────────────────────────────────────
        if loan_amount is not None:
            if limits.min_loan and loan_amount < limits.min_loan:
                conditions.append(_build_condition(
                    "loan_below_minimum",
                    "LOAN_AMOUNT_RANGE",
                    f"Loan Amount ${loan_amount:,.0f} Below Program Minimum ${limits.min_loan:,}",
                    f"The loan amount ${loan_amount:,.0f} is below the {canonical} "
                    f"minimum of ${limits.min_loan:,}.",
                    "HARD-STOP", "P0", 0.95,
                    data_elements=["loan_amount"],
                ))
            if limits.max_loan and loan_amount > limits.max_loan:
                conditions.append(_build_condition(
                    "loan_above_maximum",
                    "LOAN_AMOUNT_RANGE",
                    f"Loan Amount ${loan_amount:,.0f} Exceeds Program Maximum ${limits.max_loan:,}",
                    f"The loan amount ${loan_amount:,.0f} exceeds the {canonical} "
                    f"maximum of ${limits.max_loan:,}.",
                    "HARD-STOP", "P0", 0.95,
                    data_elements=["loan_amount"],
                ))

        # ── Min FICO (global) ────────────────────────────────────────────
        if fico is not None and limits.min_fico and fico < limits.min_fico:
            conditions.append(_build_condition(
                "fico_below_program_minimum",
                "FICO_PROGRAM_MINIMUM",
                f"FICO {fico} Below {canonical} Minimum {limits.min_fico}",
                f"The borrower's FICO score of {fico} is below the {canonical} "
                f"program minimum of {limits.min_fico}.",
                "HARD-STOP", "P0", 0.95,
                data_elements=["fico"],
            ))

        # ── Max DTI ──────────────────────────────────────────────────────
        if dti is not None and limits.max_dti:
            if dti > limits.max_dti:
                conditions.append(_build_condition(
                    "dti_exceeds_maximum",
                    "DTI_PROGRAM_MAXIMUM",
                    f"DTI {dti:.1f}% Exceeds {canonical} Maximum {limits.max_dti}%",
                    f"The borrower's DTI of {dti:.1f}% exceeds the {canonical} "
                    f"program maximum of {limits.max_dti}%.",
                    "HARD-STOP", "P0", 0.95,
                    data_elements=["dti"],
                ))
        elif dti is None and limits.max_dti:
            conditions.append(_build_condition(
                "dti_unknown",
                "DTI_PROGRAM_MAXIMUM",
                f"{canonical} Has a {limits.max_dti}% DTI Cap — DTI Unknown",
                f"The {canonical} program caps DTI at {limits.max_dti}%. "
                f"DTI is not yet available; verify once calculated.",
                "SOFT-STOP", "P2", 0.70,
                data_elements=["dti"],
            ))

        # ── FTHB loan cap ────────────────────────────────────────────────
        if is_fthb and limits.fthb_max_loan and loan_amount is not None:
            if loan_amount > limits.fthb_max_loan:
                conditions.append(_build_condition(
                    "fthb_loan_exceeds_cap",
                    "FTHB_LOAN_CAP",
                    f"FTHB Loan ${loan_amount:,.0f} Exceeds Cap ${limits.fthb_max_loan:,}",
                    f"First-time homebuyer loan amount of ${loan_amount:,.0f} exceeds "
                    f"the {canonical} FTHB cap of ${limits.fthb_max_loan:,}.",
                    "HARD-STOP", "P0", 0.95,
                    data_elements=["loan_amount", "fthb_status"],
                ))

        # ── Borrower eligibility ─────────────────────────────────────────
        if borrower_type and limits.ineligible_borrowers:
            bt_lower = borrower_type.lower()
            for inelig in limits.ineligible_borrowers:
                if inelig.lower() in bt_lower or bt_lower in inelig.lower():
                    conditions.append(_build_condition(
                        "borrower_type_ineligible",
                        "BORROWER_ELIGIBILITY",
                        f"Borrower Type '{borrower_type}' Ineligible for {canonical}",
                        f"The borrower type '{borrower_type}' is listed as ineligible "
                        f"for the {canonical} program. Ineligible types: "
                        f"{', '.join(limits.ineligible_borrowers)}.",
                        "HARD-STOP", "P0", 0.95,
                        data_elements=["borrower_type"],
                    ))
                    break

        # ── LTV/FICO grid compliance ─────────────────────────────────────
        occ_key = _normalize_occupancy(occupancy)
        grids = self.parse_ltv_grid(raw_name)

        if fico is None:
            conditions.append(_build_condition(
                "fico_missing_for_grid",
                "FICO_REQUIRED_FOR_GRID",
                f"FICO Score Required for {canonical} LTV/FICO Grid Lookup",
                f"FICO is unknown. The {canonical} program has LTV limits that "
                f"vary by FICO tier. Cannot verify eligibility without FICO.",
                "SOFT-STOP", "P1", 0.70,
                data_elements=["fico"],
            ))
        elif occ_key and occ_key in grids and ltv is not None and loan_amount is not None:
            grid = grids[occ_key]
            is_cashout = purpose and "cash" in purpose.lower()
            best_match = _find_grid_match(grid, fico, loan_amount, is_cashout)
            if best_match is None:
                conditions.append(_build_condition(
                    "no_grid_tier_match",
                    "LTV_FICO_GRID_INELIGIBLE",
                    f"No {canonical} Grid Tier for FICO {fico} / ${loan_amount:,.0f}",
                    f"No row in the {canonical} {occ_key.replace('_', ' ')} "
                    f"LTV/FICO grid accommodates FICO {fico} with loan amount "
                    f"${loan_amount:,.0f}. The loan may be ineligible.",
                    "HARD-STOP", "P0", 0.95,
                    data_elements=["fico", "loan_amount", "ltv"],
                ))
            else:
                max_ltv = best_match.max_ltv_cashout if is_cashout else best_match.max_ltv_purchase
                if max_ltv is not None and ltv > max_ltv:
                    conditions.append(_build_condition(
                        "ltv_exceeds_grid_max",
                        "LTV_FICO_GRID_EXCEEDED",
                        f"LTV {ltv:.0f}% Exceeds {canonical} Max {max_ltv}% "
                        f"(FICO {fico}, ${loan_amount:,.0f})",
                        f"For {occ_key.replace('_', ' ')}, FICO {fico}, loan "
                        f"${loan_amount:,.0f}: max LTV is {max_ltv}% "
                        f"({'cash-out' if is_cashout else 'purchase/R&T'}), "
                        f"but the loan has {ltv:.0f}% LTV.",
                        "HARD-STOP", "P0", 0.95,
                        data_elements=["ltv", "fico", "loan_amount"],
                    ))
                elif max_ltv is not None and ltv >= max_ltv - 2:
                    conditions.append(_build_condition(
                        "ltv_borderline_grid",
                        "LTV_FICO_GRID_BORDERLINE",
                        f"LTV {ltv:.0f}% Near {canonical} Max {max_ltv}% "
                        f"(FICO {fico})",
                        f"LTV of {ltv:.0f}% is within 2% of the maximum "
                        f"{max_ltv}% for this FICO/loan tier. Verify accuracy.",
                        "SOFT-STOP", "P2", 0.85,
                        data_elements=["ltv", "fico"],
                    ))

        # ── Reserve requirements ─────────────────────────────────────────
        reserves = self.parse_reserves(raw_name)
        if reserves and loan_amount is not None:
            required = _lookup_reserves(reserves, loan_amount)
            if required:
                conditions.append(_build_condition(
                    "reserve_requirement",
                    "RESERVE_REQUIREMENT",
                    f"{canonical} Requires {required} Months PITIA Reserves",
                    f"For a loan amount of ${loan_amount:,.0f}, the {canonical} "
                    f"program requires {required} months of PITIA reserves.",
                    "SOFT-STOP", "P2", 0.85,
                    data_elements=["reserves_months"],
                    docs=["bank_statement", "investment_statement"],
                ))

        return conditions


# ───────────────────────────────────────────────────────────────────────────
# Module-level helpers
# ───────────────────────────────────────────────────────────────────────────

def _normalize_occupancy(occ: str | None) -> str | None:
    if not occ:
        return None
    lower = occ.strip().lower()
    for marker, key in _OCCUPANCY_LABELS.items():
        if marker in lower:
            return key
    if "primary" in lower or "owner" in lower:
        return "primary_residence"
    if "second" in lower:
        return "second_home"
    if "invest" in lower or "non-owner" in lower:
        return "investment"
    return None


def _find_grid_match(
    grid: list[GridRow], fico: int, loan_amount: float, is_cashout: bool,
) -> GridRow | None:
    """Find the most permissive grid row that matches FICO + loan amount."""
    candidates = [
        r for r in grid
        if fico >= r.min_fico and loan_amount <= r.max_loan
    ]
    if not candidates:
        return None
    # Prefer the row with the highest LTV allowance
    def _best_ltv(r: GridRow) -> int:
        v = r.max_ltv_cashout if is_cashout else r.max_ltv_purchase
        return v if v is not None else -1
    return max(candidates, key=_best_ltv)


def _lookup_reserves(brackets: list[ReserveBracket], loan_amount: float) -> int | None:
    for b in brackets:
        if loan_amount <= b.max_loan:
            return b.months
    if brackets:
        return brackets[-1].months
    return None


def _build_condition(
    cid: str,
    family: str,
    title: str,
    desc: str,
    severity: str,
    priority: str,
    confidence: float,
    *,
    data_elements: list[str] | None = None,
    docs: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "condition_id": cid,
        "condition_family_id": family,
        "category": "Program Eligibility",
        "title": title,
        "description": desc,
        "severity": severity,
        "priority": priority,
        "confidence": confidence,
        "required_documents": docs or [],
        "required_data_elements": data_elements or [],
        "owner": "matrix_eligibility",
        "triggers": [],
        "evidence_found": [],
        "guideline_trace": [],
        "overlay_trace": [],
        "resolution_criteria": "",
        "dependencies": [],
        "tags": ["matrix_eligibility", "deterministic"],
    }


# ───────────────────────────────────────────────────────────────────────────
# Singleton
# ───────────────────────────────────────────────────────────────────────────

_instance: ProgramMatrix | None = None


def get_program_matrix(filepath: str | Path | None = None) -> ProgramMatrix:
    global _instance
    if _instance is None or (filepath and Path(filepath) != _instance.filepath):
        _instance = ProgramMatrix(filepath)
    return _instance
