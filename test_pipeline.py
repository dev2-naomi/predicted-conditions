"""
test_pipeline.py — Full end-to-end test with the real LLM.

Invokes the LangGraph agent with the SelectITIN sample data and streams
the output step by step. Saves final output to test_output.json.
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


def main():
    base = Path("data/input/case_scenario/SelectITIN")

    with open(base / "sample_case.json") as f:
        case_json = f.read()

    with open(base / "Amezcua_Corona_impac -  05152024 3.xml") as f:
        xml_content = f.read()

    docs_dir = base / "Pertinent Documents"
    submitted_docs = []
    for fname in sorted(os.listdir(docs_dir)):
        if fname.endswith(".json"):
            with open(docs_dir / fname) as f:
                d = json.load(f)
                submitted_docs.append({
                    "doc_id": str(d.get("id", "")),
                    "name": d.get("name", d.get("documentName", "unknown")),
                })

    initial_message = (
        "Execute the FULL predictive conditions workflow from STEP_00 through STEP_08.\n\n"
        "You MUST complete ALL steps in sequence. Do NOT stop after a single step.\n"
        "Do NOT output a summary between steps — just call the tools.\n\n"
        "Step sequence:\n"
        "  STEP_00: parse_loan_file, parse_loan_profile, parse_submitted_documents, "
        "build_scenario_summary, detect_contradictions, route_to_facets\n"
        "  STEP_01: check_overlay_conflicts, generate_crosscutting_conditions\n"
        "  STEP_02: load_guideline_sections (income sections from guideline_section_refs.income), "
        "then generate_income_conditions\n"
        "  STEP_03: load_guideline_sections (asset sections from guideline_section_refs.assets), "
        "then generate_asset_conditions\n"
        "  STEP_04: load_guideline_sections (credit sections from guideline_section_refs.credit), "
        "then generate_credit_conditions\n"
        "  STEP_05: load_guideline_sections (property sections from guideline_section_refs.property_appraisal), "
        "then generate_property_conditions\n"
        "  STEP_06: load_guideline_sections (title sections from guideline_section_refs.title_closing), "
        "then generate_title_conditions\n"
        "  STEP_07: load_guideline_sections (compliance sections from guideline_section_refs.compliance), "
        "then generate_compliance_conditions\n"
        "  STEP_08: merge_conditions, rank_conditions, generate_final_output\n\n"
        "For STEP_02 through STEP_07: first load the relevant guideline sections, then "
        "reason over the scenario_summary + guidelines to generate conditions.\n\n"
        f"Submitted documents:\n{json.dumps(submitted_docs, indent=2)}"
    )

    initial_state: dict = {
        "loan_file_xml": xml_content,
        "loan_profile_json": case_json,
        "submitted_documents_json": json.dumps(submitted_docs),
        "env": "Test",
        "current_step": "STEP_00",
        "messages": [HumanMessage(content=initial_message)],
    }

    model_name = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-5")
    print("=" * 70)
    print("  Starting Full Pipeline Run (with LLM)")
    print("=" * 70)
    print(f"  Model: {model_name}")
    print(f"  Loan Profile: {base / 'sample_case.json'}")
    print(f"  XML File: {base / 'Amezcua_Corona_impac -  05152024 3.xml'}")
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
    if final_output:
        print(f"\n  Total conditions: {final_output.get('stats', {}).get('total_conditions', 0)}")
        print(f"  Hard stops: {final_output.get('stats', {}).get('hard_stops', 0)}")
        print(f"  By priority: {final_output.get('stats', {}).get('by_priority', {})}")
        print(f"  By category: {final_output.get('stats', {}).get('by_category', {})}")

        print("\n  Conditions:")
        for c in final_output.get("conditions", []):
            print(f"    [{c.get('severity')}/{c.get('priority')}] {c.get('title')}")

        with open("test_output.json", "w") as f:
            json.dump(final_output, f, indent=2, default=str)
        print(f"\n  Full output saved to test_output.json")
    else:
        print("\nNo final_output in state. Checking module_outputs...")
        mo = state_vals.get("module_outputs", {})
        print(f"  Module output keys: {list(mo.keys())}")
        for k, v in mo.items():
            if isinstance(v, dict):
                conditions = v.get("conditions", [])
                if conditions:
                    print(f"  {k}: {len(conditions)} conditions")
                    for c in conditions[:5]:
                        print(f"    - [{c.get('severity')}/{c.get('priority')}] {c.get('title')}")

        with open("test_output.json", "w") as f:
            json.dump({
                "module_outputs": {
                    k: v for k, v in mo.items()
                    if isinstance(v, dict) and v.get("conditions")
                },
                "scenario_summary": {
                    k: v for k, v in state_vals.get("scenario_summary", {}).items()
                    if not k.startswith("_")
                },
                "flags": state_vals.get("flags", []),
            }, f, indent=2, default=str)
        print(f"\n  Partial output saved to test_output.json")


if __name__ == "__main__":
    main()
