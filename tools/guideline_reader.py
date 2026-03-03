from __future__ import annotations

"""
Guideline Reader — Replaces Neo4j Knowledge Graph.

Reads the NQMF Underwriting Guidelines markdown file and provides
structured search, section extraction, and rule lookup capabilities.
All downstream tools reference this module instead of a graph database.
"""

import os
import re
from typing import Optional

# ── Default path to the guidelines file ──────────────────────────────────
_GUIDELINES_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "guidelines.md"
)


# ═══════════════════════════════════════════════════════════════════════════
# Data structures
# ═══════════════════════════════════════════════════════════════════════════


class GuidelineSection:
    """A single section parsed from the guidelines document."""

    def __init__(self, heading: str, level: int, line_start: int, line_end: int, body: str):
        self.heading = heading
        self.level = level
        self.line_start = line_start
        self.line_end = line_end
        self.body = body

    def __repr__(self):
        return f"<GuidelineSection '{self.heading}' lines {self.line_start}–{self.line_end}>"


# ═══════════════════════════════════════════════════════════════════════════
# Parser
# ═══════════════════════════════════════════════════════════════════════════

# The guidelines.md uses bold-uppercase headings like:
#   **SECTION NAME**              (top-level section)
#   SUBSECTION NAME ...           (sub-section, all-caps line)
# We detect both patterns.

_HEADING_BOLD = re.compile(r"^\*\*([A-Z][A-Z \-–/&',()0-9]+)\*\*")
_HEADING_CAPS = re.compile(r"^([A-Z][A-Z \-–/&',()0-9]{4,})\s*\.{0,100}\s*\d*\s*$")
_PAGE_LINE = re.compile(r"^Page\s+\*\*\d+\*\*\s+of\s+\*\*\d+\*\*")
_GUIDELINE_FOOTER = re.compile(r"^NQM Funding, LLC Underwriting Guidelines")


class GuidelinesDocument:
    """
    In-memory parsed representation of the NQMF guidelines markdown.
    Provides search and lookup by section heading.
    """

    def __init__(self, filepath: str | None = None):
        self.filepath = filepath or _GUIDELINES_PATH
        self._raw_lines: list[str] = []
        self._sections: list[GuidelineSection] = []
        self._heading_index: dict[str, list[GuidelineSection]] = {}
        self._load_and_parse()

    # ── Loading ───────────────────────────────────────────────────────────

    def _load_and_parse(self):
        with open(self.filepath, "r", encoding="utf-8") as f:
            self._raw_lines = f.readlines()

        # First pass — find all heading positions
        heading_positions: list[tuple[int, str, int]] = []  # (line_idx, heading, level)

        for idx, line in enumerate(self._raw_lines):
            stripped = line.strip()
            if not stripped:
                continue

            # Skip page markers and footers
            if _PAGE_LINE.match(stripped) or _GUIDELINE_FOOTER.match(stripped):
                continue

            # Skip TOC-style lines (contain long runs of dots)
            if "....." in stripped:
                continue

            # Bold heading: **HEADING TEXT**
            m = _HEADING_BOLD.match(stripped)
            if m:
                heading_text = m.group(1).strip().rstrip(".")
                # Determine level: if the line starts with # or ## use that,
                # otherwise treat bold as level 1
                level = 1
                heading_positions.append((idx, heading_text, level))
                continue

            # All-caps subheading
            m = _HEADING_CAPS.match(stripped)
            if m:
                heading_text = m.group(1).strip().rstrip(".")
                # Skip if it looks like a TOC entry (contains dots)
                if "..." in heading_text:
                    continue
                heading_positions.append((idx, heading_text, 2))

        # Second pass — slice body text between headings
        for i, (line_idx, heading, level) in enumerate(heading_positions):
            next_line = heading_positions[i + 1][0] if i + 1 < len(heading_positions) else len(self._raw_lines)
            body_lines = self._raw_lines[line_idx:next_line]
            body = "".join(body_lines)
            section = GuidelineSection(
                heading=heading,
                level=level,
                line_start=line_idx + 1,  # 1-based
                line_end=next_line,
                body=body,
            )
            self._sections.append(section)

            # Index by normalized heading
            key = self._normalize(heading)
            self._heading_index.setdefault(key, []).append(section)

    @staticmethod
    def _normalize(text: str) -> str:
        """Normalize heading for fuzzy matching."""
        text = text.upper().strip()
        text = re.sub(r"[^A-Z0-9 ]", " ", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    # ── Public API ────────────────────────────────────────────────────────

    @property
    def sections(self) -> list[GuidelineSection]:
        return list(self._sections)

    def list_headings(self) -> list[str]:
        """Return all unique section headings in document order."""
        seen = set()
        result = []
        for s in self._sections:
            if s.heading not in seen:
                seen.add(s.heading)
                result.append(s.heading)
        return result

    def get_section(self, heading: str) -> list[GuidelineSection]:
        """
        Return sections matching the given heading (exact or fuzzy).
        Returns a list because some headings repeat (e.g., 'DETERMINING LOAN TO VALUE').
        """
        key = self._normalize(heading)
        # Exact match first
        if key in self._heading_index:
            return self._heading_index[key]
        # Fuzzy: find headings that contain the search term
        results = []
        for k, secs in self._heading_index.items():
            if key in k or k in key:
                results.extend(secs)
        return results

    def get_sections_by_headings(self, headings: list[str]) -> dict[str, str]:
        """
        Given a list of heading names, return a dict mapping
        heading -> concatenated body text for all matching sections.
        """
        result: dict[str, str] = {}
        for h in headings:
            sections = self.get_section(h)
            if sections:
                result[h] = "\n".join(s.body for s in sections)
        return result

    def search(self, query: str, *, case_insensitive: bool = True) -> list[dict]:
        """
        Full-text search across all section bodies.
        Returns list of {heading, line_start, line_end, snippet} dicts.
        """
        flags = re.IGNORECASE if case_insensitive else 0
        pattern = re.compile(re.escape(query), flags)
        results = []
        for section in self._sections:
            matches = list(pattern.finditer(section.body))
            if matches:
                # Build a snippet around the first match
                start = max(0, matches[0].start() - 200)
                end = min(len(section.body), matches[0].end() + 200)
                snippet = section.body[start:end].strip()
                results.append({
                    "heading": section.heading,
                    "line_start": section.line_start,
                    "line_end": section.line_end,
                    "match_count": len(matches),
                    "snippet": snippet,
                })
        return results

    def search_regex(self, pattern: str, *, flags: int = re.IGNORECASE) -> list[dict]:
        """Regex search across all section bodies."""
        compiled = re.compile(pattern, flags)
        results = []
        for section in self._sections:
            matches = list(compiled.finditer(section.body))
            if matches:
                snippets = []
                for m in matches[:3]:  # cap at 3 snippets per section
                    start = max(0, m.start() - 150)
                    end = min(len(section.body), m.end() + 150)
                    snippets.append(section.body[start:end].strip())
                results.append({
                    "heading": section.heading,
                    "line_start": section.line_start,
                    "match_count": len(matches),
                    "snippets": snippets,
                })
        return results

    def get_rule_text(self, heading: str, keyword: str) -> Optional[str]:
        """
        Find a specific rule within a section body by keyword.
        Returns the paragraph or bullet containing the keyword.
        """
        sections = self.get_section(heading)
        if not sections:
            return None
        for section in sections:
            paragraphs = re.split(r"\n\s*\n", section.body)
            for para in paragraphs:
                if keyword.lower() in para.lower():
                    return para.strip()
        return None

    def get_full_text(self) -> str:
        """Return the raw guidelines text."""
        return "".join(self._raw_lines)


# ═══════════════════════════════════════════════════════════════════════════
# Singleton accessor
# ═══════════════════════════════════════════════════════════════════════════

_instance: Optional[GuidelinesDocument] = None


def get_guidelines(filepath: str | None = None) -> GuidelinesDocument:
    """Return (and cache) the parsed guidelines document."""
    global _instance
    if _instance is None or (filepath and filepath != _instance.filepath):
        _instance = GuidelinesDocument(filepath)
    return _instance
