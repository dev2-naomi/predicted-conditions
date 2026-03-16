"""
matrix_parser.py — Parses program_matrices.md into searchable sections
keyed by program name.

The markdown file uses level-1 headings (# **Program Name**) to delimit
each program's matrix.  Everything between two consecutive program headings
belongs to that program.  The first section (before any program heading)
contains shared/general requirements that apply to all programs.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

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
                # Skip the document title heading
                if "PROGRAM MATRICES" in heading.upper():
                    current_lines.append(line)
                    continue
                # Save previous section
                if current_program is None:
                    self._general_section = "".join(current_lines)
                else:
                    self._program_sections[current_program] = "".join(current_lines)
                current_program = heading
                current_lines = [line]
            else:
                current_lines.append(line)

        # Save the last section
        if current_program is not None:
            self._program_sections[current_program] = "".join(current_lines)
        else:
            self._general_section = "".join(current_lines)

    @property
    def program_names(self) -> list[str]:
        return list(self._program_sections.keys())

    @property
    def general_section(self) -> str:
        return self._general_section

    def resolve_program_name(self, raw_name: str) -> str | None:
        """Resolve a raw program name (from XML/JSON) to the canonical name."""
        if not raw_name:
            return None
        normalized = raw_name.strip().lower()
        # Direct match
        if normalized in _ALIAS_TO_CANONICAL:
            return _ALIAS_TO_CANONICAL[normalized]
        # Substring match
        for alias, canonical in _ALIAS_TO_CANONICAL.items():
            if alias in normalized or normalized in alias:
                return canonical
        return None

    def get_program_section(self, program_name: str) -> str | None:
        """Get the full matrix section for a program (exact canonical name)."""
        return self._program_sections.get(program_name)

    def get_program_matrix(self, raw_name: str) -> tuple[str | None, str]:
        """
        Resolve a raw program name and return (canonical_name, section_text).
        Includes both the program-specific section and the general section.
        Returns (None, "") if program not found.
        """
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


# ───────────────────────────────────────────────────────────────────────────
# Singleton
# ───────────────────────────────────────────────────────────────────────────

_instance: ProgramMatrix | None = None


def get_program_matrix(filepath: str | Path | None = None) -> ProgramMatrix:
    global _instance
    if _instance is None or (filepath and Path(filepath) != _instance.filepath):
        _instance = ProgramMatrix(filepath)
    return _instance
