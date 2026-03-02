"""
guidelines.py — NQMF Underwriting Guidelines loader.

STUBBED: Returns placeholder text.
Implement a chunking/RAG/full-load strategy here when ready.
"""

from __future__ import annotations

from pathlib import Path

_GUIDELINES_PATH = Path(__file__).parent.parent.parent / "data" / "guidelines.md"

_KNOWN_SECTIONS: dict[str, str] = {
    "GENERAL UNDERWRITING REQUIREMENTS": "general",
    "OCCUPANCY TYPES": "general",
    "TRANSACTION TYPES": "general",
    "FULL DOCUMENTATION": "income",
    "EMPLOYMENT": "income",
    "RATIOS AND QUALIFYING – FULL AND ALT DOC": "income",
    "ALTERNATIVE DOCUMENTATION (ALT DOC)": "income",
    "DSCR RATIOS AND RENTAL INCOME REQUIREMENTS": "income",
    "DSCR PRODUCT TERMS": "income",
    "ITIN": "income",
    "ITIN – DOCUMENTATION REQUIREMENTS": "income",
    "ITIN - ELIGIBILITY": "income",
    "FOREIGN NATIONALS": "income",
    "SECOND LIEN": "income",
    "SECOND LIEN SELECT SENIOR LIEN QUALIFYING TERMS": "income",
    "TEXAS HOME EQUITY LOANS (CASH-OUT REFI TEXAS)": "title_closing",
    "ASSETS": "assets",
    "RESERVES": "assets",
    "CREDIT": "credit",
    "HOUSING HISTORY": "credit",
    "HOUSING EVENTS AND PRIOR BANKRUPTCY": "credit",
    "LIABILITIES": "credit",
    "APPRAISALS": "property_appraisal",
    "PROPERTY CONSIDERATIONS": "property_appraisal",
    "PROPERTY TYPES": "property_appraisal",
    "CONDOMINIUMS - GENERAL": "property_appraisal",
    "WARRANTABLE CONDOMINIUMS": "property_appraisal",
    "NON-WARRANTABLE CONDOMINIUMS": "property_appraisal",
    "COOPERATIVES (CO-OP)": "property_appraisal",
    "PROPERTY INSURANCE": "title_closing",
    "TITLE INSURANCE": "title_closing",
    "COMPLIANCE": "compliance",
    "BORROWER ELIGIBILITY": "compliance",
    "VESTING AND OWNERSHIP": "compliance",
}


def load_sections(section_names: list[str]) -> str:
    """
    Load guideline content for the requested section names.

    Currently a stub: returns a notice that the guidelines file is available
    but section extraction is not yet implemented. The full file path is noted
    so the LLM can reference it.

    TODO: Implement chunking, heading-based extraction, or RAG retrieval.
    """
    if not section_names:
        return ""

    requested = "\n".join(f"- {s}" for s in section_names)
    return (
        f"[GUIDELINES STUB]\n"
        f"Requested sections:\n{requested}\n\n"
        f"Guidelines file: {_GUIDELINES_PATH}\n"
        f"Full guideline content is available at the path above. "
        f"Section-level extraction is not yet implemented. "
        f"Use the full guidelines document as authoritative source."
    )


def load_full_guidelines() -> str:
    """Load the complete guidelines file. Use with caution — ~10K lines."""
    if _GUIDELINES_PATH.exists():
        return _GUIDELINES_PATH.read_text(encoding="utf-8")
    return "[GUIDELINES NOT FOUND]"
