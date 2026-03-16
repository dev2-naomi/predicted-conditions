"""
test_pipeline.py — Full end-to-end test with the real LLM.

Supports two input modes:
  1. XML-only: provide just the XML file path
  2. XML + JSON: provide XML + a sample_case.json for external override

Usage:
    python test_pipeline.py                           # SelectITIN (default)
    python test_pipeline.py data/input/case_scenario/SelectITIN
    python test_pipeline.py /path/to/some.xml         # XML-only mode

Submitted documents are loaded from a manifest JSON if available
(MANIFEST_PATH env var or manifest.json in the case directory),
otherwise falls back to individual JSON files in Pertinent Documents/.
"""

import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from langchain_core.messages import HumanMessage

from agent import agent, PredictiveConditionsState


def _find_xml(directory: Path) -> str:
    """Find and read the first .xml file in a directory."""
    for f in sorted(directory.iterdir()):
        if f.suffix.lower() == ".xml":
            return f.read_text()
    return ""


def _find_json(directory: Path, pattern: str = "sample_case") -> str:
    """Find and read a JSON file matching a pattern."""
    for f in sorted(directory.iterdir()):
        if f.suffix == ".json" and pattern in f.stem.lower():
            return f.read_text()
    return ""


def _find_submitted_docs(directory: Path) -> list:
    """
    Find submitted documents for a case directory.
    Checks for a manifest JSON first (via MANIFEST_PATH env var or
    manifest.json in the directory), then falls back to reading
    individual doc files from Pertinent Documents/.
    """
    from tools.shared.manifest_parser import parse_manifest

    manifest_path = Path(os.environ.get("MANIFEST_PATH", "")) or (directory / "manifest.json")
    if manifest_path.exists():
        docs = parse_manifest(manifest_path)
        print(f"  Loaded {len(docs)} documents from manifest: {manifest_path}")
        return docs

    docs_dir = directory / "Pertinent Documents"
    if not docs_dir.exists():
        return []
    submitted = []
    for fname in sorted(os.listdir(docs_dir)):
        if fname.endswith(".json"):
            with open(docs_dir / fname) as f:
                d = json.load(f)
                submitted.append({
                    "doc_id": str(d.get("id", "")),
                    "name": d.get("name", d.get("documentName", "unknown")),
                })
    return submitted


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else "data/input/case_scenario/SelectITIN"
    arg_path = Path(arg)

    xml_content = ""
    case_json = ""
    submitted_docs = []
    source_label = ""

    if arg_path.is_file() and arg_path.suffix.lower() == ".xml":
        xml_content = arg_path.read_text()
        source_label = arg_path.name
    elif arg_path.is_dir():
        xml_content = _find_xml(arg_path)
        case_json = _find_json(arg_path)
        submitted_docs = _find_submitted_docs(arg_path)
        source_label = arg_path.name
    else:
        print(f"Error: {arg} is not a valid file or directory.")
        sys.exit(1)

    if not xml_content:
        print(f"Error: No XML file found in {arg}")
        sys.exit(1)

    step_sequence = (
        "  STEP_00: parse_loan_file"
    )
    if case_json:
        step_sequence += ", parse_loan_profile"
    if submitted_docs:
        step_sequence += ", parse_submitted_documents"
    step_sequence += ", build_scenario_summary, detect_contradictions, route_to_facets"

    doc_info = ""
    if submitted_docs:
        # Summarize for the prompt — full data is in submitted_documents_json state
        substantive = [d for d in submitted_docs if d.get("doc_type", "other") != "other"]
        doc_type_counts: dict[str, int] = {}
        for d in submitted_docs:
            dt = d.get("doc_type", "other")
            doc_type_counts[dt] = doc_type_counts.get(dt, 0) + 1
        summary_lines = [f"  {dt}: {ct}" for dt, ct in sorted(doc_type_counts.items()) if dt != "other"]
        other_ct = doc_type_counts.get("other", 0)
        if other_ct:
            summary_lines.append(f"  other/admin: {other_ct}")
        doc_info = (
            f"\n\n{len(submitted_docs)} submitted documents ({len(substantive)} substantive):\n"
            + "\n".join(summary_lines)
        )

    initial_message = (
        "Execute the FULL predictive conditions workflow from STEP_00 through STEP_08.\n\n"
        "You MUST complete ALL steps in sequence. Do NOT stop after a single step.\n"
        "Do NOT output a summary between steps — just call the tools.\n\n"
        "Step sequence:\n"
        f"{step_sequence}\n"
        "  STEP_00b: check_submission_completeness\n"
        "  STEP_00c: load_program_matrix, then generate_matrix_conditions\n"
        "  STEP_01: check_overlay_conflicts, generate_crosscutting_conditions\n"
        "  STEP_02: load_guideline_sections (income sections), then generate_income_conditions\n"
        "  STEP_03: load_guideline_sections (asset sections), then generate_asset_conditions\n"
        "  STEP_04: load_guideline_sections (credit sections), then generate_credit_conditions\n"
        "  STEP_05: load_guideline_sections (property sections), then generate_property_conditions\n"
        "  STEP_06: load_guideline_sections (title sections), then generate_title_conditions\n"
        "  STEP_07: load_guideline_sections (compliance sections), then generate_compliance_conditions\n"
        "  STEP_08: merge_conditions, rank_conditions, generate_final_output\n\n"
        "For STEP_02 through STEP_07: first load the relevant guideline sections, then "
        "reason over the scenario_summary + guidelines to generate conditions.\n"
        f"{doc_info}"
    )

    # Allow env-var overrides for program and FICO (for testing)
    test_program = os.environ.get("TEST_PROGRAM", "")
    test_fico = os.environ.get("TEST_FICO", "")
    if (test_program or test_fico) and not case_json:
        override: dict = {"metadata": {}}
        if test_program:
            override["metadata"]["loan_program"] = {"name": test_program}
        if test_fico:
            override["metadata"]["fico"] = int(test_fico)
        case_json = json.dumps(override)

    initial_state: dict = {
        "loan_file_xml": xml_content,
        "env": "Test",
        "current_step": "STEP_00",
        "messages": [HumanMessage(content=initial_message)],
    }

    if case_json:
        initial_state["loan_profile_json"] = case_json

    if submitted_docs:
        initial_state["submitted_documents_json"] = json.dumps(submitted_docs)

    model_name = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-5")
    print("=" * 70)
    print("  Starting Full Pipeline Run (with LLM)")
    print("=" * 70)
    print(f"  Model: {model_name}")
    print(f"  Source: {source_label}")
    print(f"  External JSON: {'yes' if case_json else 'no (XML-derived profile)'}")
    print(f"  Submitted Documents: {len(submitted_docs)}")
    print("=" * 70)
    sys.stdout.flush()

    config = {"recursion_limit": 150}

    tool_call_count = 0
    start_time = time.time()
    accumulated_state: dict = {}

    for event in agent.stream(initial_state, config=config, stream_mode="values"):
        accumulated_state = event

        msgs = event.get("messages", [])
        if msgs:
            last_msg = msgs[-1]
            if hasattr(last_msg, "tool_calls") and last_msg.tool_calls:
                for tc in last_msg.tool_calls:
                    tool_call_count += 1
                    args_preview = json.dumps(tc.get("args", {}), default=str)
                    if len(args_preview) > 300:
                        args_preview = args_preview[:300] + "..."
                    print(f"\n  [{tool_call_count}] LLM -> {tc['name']}({args_preview})")
            elif hasattr(last_msg, "content") and last_msg.content and not hasattr(last_msg, "tool_call_id"):
                content = last_msg.content if isinstance(last_msg.content, str) else str(last_msg.content)
                if len(content) > 300:
                    content = content[:300] + "..."
                if content.strip():
                    print(f"\n  LLM says: {content}")

        sys.stdout.flush()

    elapsed = time.time() - start_time
    print(f"\n{'=' * 70}")
    print(f"  Pipeline Complete! ({elapsed:.1f}s, {tool_call_count} tool calls)")
    print("=" * 70)

    state_vals = accumulated_state
    final_output = state_vals.get("final_output")

    _KEEP_FIELDS = ("category", "severity", "title", "description", "required_documents", "required_data_elements")

    def _distill(conds: list) -> list:
        return [{k: c.get(k) for k in _KEEP_FIELDS} for c in conds]

    # Collect conditions: prefer final_output, fall back to module_outputs
    conditions = []
    conditions_full = []
    if final_output and final_output.get("conditions"):
        conditions = final_output["conditions"]
        conditions_full = final_output.get("conditions_full", conditions)
    else:
        mo = state_vals.get("module_outputs", {})
        ranked = mo.get("08_rank", {}).get("ranked_conditions", [])
        merged = mo.get("08_merge", {}).get("merged_conditions", [])
        conditions_full = ranked or merged
        if not conditions_full:
            for module_key in ["00c", "01", "02", "03", "04", "05", "06", "07"]:
                mod = mo.get(module_key, {})
                conditions_full.extend(mod.get("conditions", []))
        conditions = _distill(conditions_full)

    scenario_summary = {
        k: v for k, v in state_vals.get("scenario_summary", {}).items()
        if not k.startswith("_")
    }

    hard_stops = sum(1 for c in conditions_full if c.get("severity") == "HARD-STOP")
    by_category: dict[str, int] = {}
    by_priority: dict[str, int] = {}
    for c in conditions_full:
        cat = c.get("category", "Other")
        pri = c.get("priority", "P3")
        by_category[cat] = by_category.get(cat, 0) + 1
        by_priority[pri] = by_priority.get(pri, 0) + 1

    print(f"\n  Total conditions: {len(conditions)}")
    print(f"  Hard stops: {hard_stops}")
    print(f"  By priority: {by_priority}")
    print(f"  By category: {by_category}")

    print("\n  Conditions (distilled):")
    for c in conditions:
        print(f"    [{c.get('severity')}/{c.get('category')}] {c.get('title')}")

    output = {
        "scenario_summary": scenario_summary,
        "conditions": conditions,
        "conditions_full": conditions_full,
        "stats": {
            "total_conditions": len(conditions),
            "hard_stops": hard_stops,
            "by_category": by_category,
            "by_priority": by_priority,
        },
        "flags": state_vals.get("flags", []),
    }

    with open("test_output.json", "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\n  Full output saved to test_output.json")

    # Save the full final state (excluding messages and bulky raw data)
    def _serializable_state(sv: dict) -> dict:
        out = {}
        for k, v in sv.items():
            if k == "messages":
                out[k] = f"[{len(v)} messages]"
                continue
            if k == "loan_file_xml":
                out[k] = f"[XML string, {len(v)} chars]"
                continue
            if k == "submitted_documents_json":
                try:
                    docs = json.loads(v) if isinstance(v, str) else v
                    out[k] = f"[{len(docs)} documents]"
                except Exception:
                    out[k] = f"[string, {len(str(v))} chars]"
                continue
            if isinstance(v, dict):
                cleaned = {}
                for dk, dv in v.items():
                    if dk == "raw_sections":
                        cleaned[dk] = f"[{len(dv)} sections]"
                    elif dk == "_parsed_xml":
                        cleaned[dk] = "{...parsed xml dict...}"
                    else:
                        cleaned[dk] = dv
                out[k] = cleaned
            else:
                out[k] = v
        return out

    with open("test_final_state.json", "w") as f:
        json.dump(_serializable_state(state_vals), f, indent=2, default=str)
    print(f"  Full final state saved to test_final_state.json")


if __name__ == "__main__":
    main()
