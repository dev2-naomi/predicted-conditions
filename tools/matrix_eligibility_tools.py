"""
matrix_eligibility_tools.py — Tools for STEP_08: Program Matrix Eligibility Check.

Three tools (hybrid deterministic + LLM):
  1. check_matrix_eligibility: deterministic checks against the parsed
     LTV/FICO grid, reserves, DTI, loan amount, borrower eligibility,
     and FTHB limits. Runs instantly, no LLM needed.
  2. load_program_matrix: returns a *trimmed* version of the program
     matrix text (qualitative rules only) for the LLM to reason over.
  3. generate_matrix_conditions: the LLM stores any additional conditions
     it generated after reading the trimmed matrix text.
"""

from __future__ import annotations

from typing import Any, Dict, List

from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command
from typing_extensions import Annotated

from tools.shared.matrix_parser import get_program_matrix


@tool
def check_matrix_eligibility(
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
    state: Annotated[dict, InjectedState] = None,
) -> Command:
    """
    Run deterministic program matrix checks against the loan scenario.

    Automatically reads program, FICO, LTV, loan amount, DTI, occupancy,
    purpose, and borrower type from scenario_summary, then checks them
    against the parsed LTV/FICO grid, reserve schedule, DTI cap, loan
    amount range, borrower eligibility list, and FTHB loan cap.

    Stores any violations or warnings in module_outputs["08"] immediately.
    This should be called FIRST in STEP_08, before load_program_matrix.
    """
    s = state or {}
    ss = s.get("scenario_summary", {})
    numbers = ss.get("numbers", {})

    program = ss.get("program") or "unknown"

    fico_val = numbers.get("fico")
    fico = int(fico_val) if fico_val is not None else None
    ltv_val = numbers.get("ltv")
    ltv = float(ltv_val) if ltv_val is not None else None
    loan_val = numbers.get("loan_amount")
    loan_amount = float(loan_val) if loan_val is not None else None
    dti_val = numbers.get("dti")
    dti = float(dti_val) if dti_val is not None else None

    occupancy = ss.get("occupancy")
    purpose = ss.get("purpose")
    borrower_type = ss.get("borrower_type")
    is_fthb = bool(ss.get("is_fthb", False))

    pm = get_program_matrix()
    conditions = pm.run_deterministic_checks(
        program,
        fico=fico,
        ltv=ltv,
        loan_amount=loan_amount,
        dti=dti,
        occupancy=occupancy,
        purpose=purpose,
        borrower_type=borrower_type,
        is_fthb=is_fthb,
    )

    titles = [c.get("title", "?") for c in conditions]
    hard_stops = sum(1 for c in conditions if c.get("severity") == "HARD-STOP")

    summary = (
        f"Deterministic matrix check for '{program}': "
        f"{len(conditions)} condition(s) found ({hard_stops} HARD-STOP). "
        f"Inputs: FICO={fico}, LTV={ltv}, Loan={loan_amount}, "
        f"DTI={dti}, Occ={occupancy}, Purpose={purpose}."
    )
    if titles:
        summary += f" Conditions: {titles}"

    return Command(update={
        "module_outputs": {"08": {"conditions": conditions}},
        "current_step": "STEP_08",
        "messages": [ToolMessage(summary, tool_call_id=tool_call_id)],
    })


@tool
def load_program_matrix(
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
    state: Annotated[dict, InjectedState] = None,
) -> str:
    """
    Load the trimmed program matrix text for LLM review.

    Returns only qualitative rules that need interpretation (product type,
    cash-out seasoning, declining markets, property edge cases, income doc
    specifics, non-occupant co-borrower rules, credit/housing event
    seasoning). Numeric checks (LTV/FICO grid, DTI, reserves, loan
    amounts, borrower eligibility, FTHB) are handled by
    check_matrix_eligibility and are excluded from this text.

    If the program cannot be resolved, returns available program names.
    """
    s = state or {}
    ss = s.get("scenario_summary", {})
    program = ss.get("program") or "unknown"

    pm = get_program_matrix()
    canonical = pm.resolve_program_name(program)

    if not canonical:
        available = pm.program_names
        return (
            f"Could not find program matrix for '{program}'. "
            f"Available programs: {available}. "
            f"Please verify the program name from the loan file."
        )

    trimmed = pm.get_trimmed_text(program)
    if not trimmed.strip():
        return f"No additional qualitative rules found for '{canonical}'."

    return (
        f"## {canonical} — Qualitative Rules (for LLM review)\n\n"
        f"NOTE: LTV/FICO grid, DTI cap, reserves, loan amount range, "
        f"borrower eligibility, and FTHB limits have already been checked "
        f"deterministically by check_matrix_eligibility. Only review the "
        f"rules below for items that require interpretation.\n\n"
        f"{trimmed}"
    )


@tool
def generate_matrix_conditions(
    conditions: List[Dict],
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
    state: Annotated[dict, InjectedState] = None,
) -> Command:
    """
    Store additional program eligibility conditions generated by the LLM
    after reviewing the trimmed qualitative matrix rules.

    These complement the deterministic conditions from check_matrix_eligibility.
    Only generate conditions for qualitative rules: product type eligibility,
    cash-out seasoning, declining markets, property edge cases, income doc
    specifics, non-occupant co-borrower, credit/housing event seasoning.

    Do NOT duplicate checks already performed deterministically (LTV/FICO
    grid, DTI, reserves, loan amounts, borrower eligibility, FTHB).

    Args:
        conditions: List of condition dicts conforming to the standard schema.
    """
    for c in conditions:
        c.setdefault("tags", [])
        if "matrix_eligibility" not in c["tags"]:
            c["tags"].append("matrix_eligibility")

    # Merge with any deterministic conditions already stored in 08
    s = state or {}
    existing = s.get("module_outputs", {}).get("08", {}).get("conditions", [])
    merged = existing + conditions

    titles = [c.get("title", "?") for c in conditions]
    return Command(update={
        "module_outputs": {"08": {"conditions": merged}},
        "current_step": "STEP_08",
        "messages": [ToolMessage(
            f"Stored {len(conditions)} LLM-generated matrix condition(s): {titles}. "
            f"Total STEP_08 conditions (deterministic + LLM): {len(merged)}.",
            tool_call_id=tool_call_id,
        )],
    })
