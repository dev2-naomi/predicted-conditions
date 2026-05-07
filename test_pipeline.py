"""
test_pipeline.py — Full end-to-end test with the real LLM (v2 Document-Centric).

Inputs:
  - loan.xml (MISMO XML)
  - manifest.json (document inventory from extraction)
  - eligibility.json (eligibility engine output)

Usage:
    python test_pipeline.py compiled_inputs/nyarko         # single loan
    python test_pipeline.py --batch compiled_inputs         # all loans sequentially
    python test_pipeline.py --batch compiled_inputs --output-dir test_results
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from agent import agent


def _find_xml(directory: Path) -> str:
    for f in sorted(directory.iterdir()):
        if f.suffix.lower() == ".xml":
            return f.read_text()
    return ""


def _find_manifest_raw(directory: Path) -> str | None:
    env_path = os.environ.get("MANIFEST_PATH", "")
    if env_path:
        p = Path(env_path)
        if p.exists():
            return p.read_text(encoding="utf-8")

    for f in sorted(directory.iterdir()):
        if f.suffix == ".json" and "manifest" in f.name.lower():
            return f.read_text(encoding="utf-8")

    return None


def _find_eligibility_raw(directory: Path) -> str | None:
    env_path = os.environ.get("ELIGIBILITY_PATH", "")
    if env_path:
        p = Path(env_path)
        if p.exists():
            return p.read_text(encoding="utf-8")

    for f in sorted(directory.iterdir()):
        if f.suffix == ".json" and ("eligibility" in f.name.lower() or "sample_output" in f.name.lower()):
            return f.read_text(encoding="utf-8")

    return None


def _serializable_state(sv: dict) -> dict:
    out = {}
    for k, v in sv.items():
        if k == "messages":
            out[k] = f"[{len(v)} messages]"
            continue
        if k == "loan_file_xml":
            out[k] = f"[XML string, {len(v)} chars]"
            continue
        if k == "eligibility_json":
            out[k] = f"[eligibility JSON, {len(v)} chars]"
            continue
        if k == "manifest_json":
            out[k] = f"[manifest JSON, {len(v)} chars]"
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


def run_single(input_dir: Path, output_dir: Path | None = None) -> dict:
    """Run the pipeline for a single loan directory. Returns the result dict."""
    xml_content = _find_xml(input_dir)
    manifest_raw = _find_manifest_raw(input_dir)
    eligibility_raw = _find_eligibility_raw(input_dir)
    source_label = input_dir.name

    if not xml_content and not manifest_raw and not eligibility_raw:
        print(f"  SKIP: No XML, manifest, or eligibility file found in {input_dir}")
        return {}

    initial_state: dict = {
        "loan_file_xml": xml_content,
        "env": "Test",
        "current_step": "STEP_00",
    }
    if manifest_raw:
        initial_state["manifest_json"] = manifest_raw
    if eligibility_raw:
        initial_state["eligibility_json"] = eligibility_raw

    doc_count_label = "manifest" if manifest_raw else "no documents"
    model_name = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-5")
    print("=" * 70)
    print(f"  SBIQ Predictive Document Needs Pipeline (v2)")
    print("=" * 70)
    print(f"  Model: {model_name}")
    print(f"  Source: {source_label}")
    print(f"  Documents: {doc_count_label}")
    print(f"  Eligibility: {'yes' if eligibility_raw else 'no'}")
    print("=" * 70)
    sys.stdout.flush()

    config = {"recursion_limit": 250}
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

    document_requests = []
    if final_output and final_output.get("document_requests"):
        document_requests = final_output["document_requests"]
    else:
        mo = state_vals.get("module_outputs", {})
        ranked = mo.get("08", {}).get("ranked_document_requests", [])
        merged = mo.get("08", {}).get("merged_document_requests", [])
        document_requests = ranked or merged
        if not document_requests:
            for module_key in ["01", "02", "03", "04", "05", "06", "07"]:
                mod = mo.get(module_key, {})
                document_requests.extend(mod.get("document_requests", []))

    scenario_summary = {
        k: v for k, v in state_vals.get("scenario_summary", {}).items()
        if not k.startswith("_")
    }

    hard_stops = sum(1 for dr in document_requests if dr.get("severity") == "HARD-STOP")
    by_category: dict[str, int] = {}
    by_priority: dict[str, int] = {}
    by_status: dict[str, int] = {}
    for dr in document_requests:
        cat = dr.get("document_category", "Other")
        pri = dr.get("priority", "P3")
        status = dr.get("status", "unknown")
        by_category[cat] = by_category.get(cat, 0) + 1
        by_priority[pri] = by_priority.get(pri, 0) + 1
        by_status[status] = by_status.get(status, 0) + 1

    print(f"\n  Total document requests: {len(document_requests)}")
    print(f"  Hard stops: {hard_stops}")
    print(f"  By priority: {by_priority}")
    print(f"  By category: {by_category}")
    print(f"  By status: {by_status}")

    print("\n  Document Requests:")
    for dr in document_requests:
        spec_count = len(dr.get("specifications", []))
        reason_count = len(dr.get("reasons_needed", []))
        print(
            f"    [{dr.get('severity')}/{dr.get('priority')}] "
            f"{dr.get('document_type', '?')} "
            f"({dr.get('document_category', '?')}) "
            f"— {spec_count} specs, {reason_count} reasons "
            f"[{dr.get('status', '?')}]"
        )

    result = {
        "scenario_summary": scenario_summary,
        "seen_conflicts": state_vals.get("seen_conflicts", []),
        "document_requests": document_requests,
        "stats": {
            "total_document_requests": len(document_requests),
            "hard_stop_documents": hard_stops,
            "by_category": by_category,
            "by_priority": by_priority,
            "by_status": by_status,
            "elapsed_seconds": round(elapsed, 1),
            "tool_calls": tool_call_count,
        },
    }

    dest = output_dir or Path(".")
    dest.mkdir(parents=True, exist_ok=True)

    output_file = dest / f"{source_label}_output.json"
    state_file = dest / f"{source_label}_final_state.json"

    with open(output_file, "w") as f:
        json.dump(result, f, indent=2, default=str)
    print(f"\n  Output saved to {output_file}")

    with open(state_file, "w") as f:
        json.dump(_serializable_state(state_vals), f, indent=2, default=str)
    print(f"  State saved to {state_file}")

    return result


def run_batch(parent_dir: Path, output_root: Path):
    """Run all loan subdirectories sequentially and save results."""
    loan_dirs = sorted(
        d for d in parent_dir.iterdir()
        if d.is_dir() and not d.name.startswith(".")
    )
    if not loan_dirs:
        print(f"Error: No subdirectories found in {parent_dir}")
        sys.exit(1)

    print("=" * 70)
    print(f"  BATCH RUN — {len(loan_dirs)} loans")
    print(f"  Loans: {', '.join(d.name for d in loan_dirs)}")
    print(f"  Output: {output_root}/")
    print("=" * 70)
    sys.stdout.flush()

    batch_start = time.time()
    summary_rows: list[dict] = []

    for i, loan_dir in enumerate(loan_dirs, 1):
        print(f"\n{'#' * 70}")
        print(f"  [{i}/{len(loan_dirs)}] Running: {loan_dir.name}")
        print(f"{'#' * 70}\n")
        sys.stdout.flush()

        out_dir = output_root / loan_dir.name
        try:
            result = run_single(loan_dir, output_dir=out_dir)
            stats = result.get("stats", {})
            summary_rows.append({
                "loan": loan_dir.name,
                "status": "OK",
                "documents": stats.get("total_document_requests", 0),
                "hard_stops": stats.get("hard_stop_documents", 0),
                "by_category": stats.get("by_category", {}),
                "by_priority": stats.get("by_priority", {}),
                "elapsed_s": stats.get("elapsed_seconds", 0),
                "tool_calls": stats.get("tool_calls", 0),
            })
        except Exception as e:
            print(f"\n  ERROR running {loan_dir.name}: {e}")
            summary_rows.append({
                "loan": loan_dir.name,
                "status": f"ERROR: {e}",
                "documents": 0,
                "hard_stops": 0,
                "by_category": {},
                "by_priority": {},
                "elapsed_s": 0,
                "tool_calls": 0,
            })

    batch_elapsed = time.time() - batch_start

    print(f"\n\n{'=' * 70}")
    print(f"  BATCH COMPLETE — {len(loan_dirs)} loans in {batch_elapsed:.0f}s")
    print("=" * 70)
    print(f"\n  {'Loan':<15} {'Status':<8} {'Docs':>5} {'Hard':>5} {'Time':>7} {'Calls':>6}")
    print(f"  {'-'*15} {'-'*8} {'-'*5} {'-'*5} {'-'*7} {'-'*6}")
    for row in summary_rows:
        print(
            f"  {row['loan']:<15} {row['status']:<8} "
            f"{row['documents']:>5} {row['hard_stops']:>5} "
            f"{row['elapsed_s']:>6.0f}s {row['tool_calls']:>6}"
        )
    print()

    with open(output_root / "batch_summary.json", "w") as f:
        json.dump({
            "total_loans": len(loan_dirs),
            "total_elapsed_seconds": round(batch_elapsed, 1),
            "results": summary_rows,
        }, f, indent=2)
    print(f"  Batch summary saved to {output_root / 'batch_summary.json'}")


def main():
    args = sys.argv[1:]

    is_batch = "--batch" in args
    if is_batch:
        args.remove("--batch")

    output_dir_str = None
    if "--output-dir" in args:
        idx = args.index("--output-dir")
        if idx + 1 < len(args):
            output_dir_str = args[idx + 1]
            args = args[:idx] + args[idx + 2:]
        else:
            print("Error: --output-dir requires a value")
            sys.exit(1)

    target = args[0] if args else "compiled_inputs"
    target_path = Path(target)

    if is_batch:
        if not target_path.is_dir():
            print(f"Error: {target} is not a directory for batch mode")
            sys.exit(1)
        output_root = Path(output_dir_str) if output_dir_str else Path("test_results")
        run_batch(target_path, output_root)
    else:
        if not target_path.exists():
            print(f"Error: {target} does not exist")
            sys.exit(1)
        out = Path(output_dir_str) if output_dir_str else None
        run_single(target_path, output_dir=out)


if __name__ == "__main__":
    main()
