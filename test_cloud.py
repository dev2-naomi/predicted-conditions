"""
test_cloud.py — Smoke-test the LangGraph Cloud deployment (v2 Document-Centric).

Sends the three raw inputs (XML, manifest, eligibility) as strings
to the cloud API and polls until the run completes. Prints the final
document_requests output or the last error.

Usage:
    python test_cloud.py [input_directory]
    python test_cloud.py compiled_inputs/nyarko

Defaults to compiled_inputs/nyarko if no directory is given.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

import requests

BASE_URL = os.getenv("LANGGRAPH_URL", "")
API_KEY = os.getenv("LANGCHAIN_API_KEY", "")
ASSISTANT_ID = "predicted-conditions"

HEADERS = {
    "x-api-key": API_KEY,
    "Content-Type": "application/json",
}


def _load_raw(directory: Path) -> dict:
    """Auto-detect XML, manifest, and eligibility files and return raw strings."""
    inputs: dict[str, str] = {}

    for f in sorted(directory.iterdir()):
        name_lower = f.name.lower()
        if f.suffix.lower() == ".xml" and "loan_file_xml" not in inputs:
            inputs["loan_file_xml"] = f.read_text(encoding="utf-8", errors="replace")
        elif "manifest" in name_lower and f.suffix.lower() == ".json":
            inputs["manifest_json"] = f.read_text(encoding="utf-8")
        elif ("eligibility" in name_lower or "sample_output" in name_lower) and f.suffix.lower() == ".json":
            inputs["eligibility_json"] = f.read_text(encoding="utf-8")

    return inputs


def create_thread() -> str:
    resp = requests.post(f"{BASE_URL}/threads", headers=HEADERS, json={})
    resp.raise_for_status()
    thread_id = resp.json()["thread_id"]
    print(f"Thread created: {thread_id}")
    return thread_id


def start_run(thread_id: str, inputs: dict) -> str:
    payload = {
        "assistant_id": ASSISTANT_ID,
        "input": {**inputs, "current_step": "STEP_00"},
        "config": {
            "recursion_limit": 250,
        },
    }
    resp = requests.post(
        f"{BASE_URL}/threads/{thread_id}/runs",
        headers=HEADERS,
        json=payload,
    )
    resp.raise_for_status()
    run = resp.json()
    run_id = run["run_id"]
    print(f"Run started:   {run_id}")
    return run_id


def poll_run(thread_id: str, run_id: str, poll_interval: int = 15, timeout: int = 900) -> dict:
    """Poll until run reaches a terminal status. Returns the run object."""
    start = time.time()
    terminal = {"success", "error", "timeout", "interrupted"}

    while time.time() - start < timeout:
        resp = requests.get(
            f"{BASE_URL}/threads/{thread_id}/runs/{run_id}",
            headers=HEADERS,
        )
        resp.raise_for_status()
        run = resp.json()
        status = run.get("status", "unknown")
        elapsed = int(time.time() - start)
        print(f"  [{elapsed:>4}s] status={status}")

        if status in terminal:
            return run

        time.sleep(poll_interval)

    raise TimeoutError(f"Run did not complete within {timeout}s")


def get_thread_state(thread_id: str) -> dict:
    resp = requests.get(
        f"{BASE_URL}/threads/{thread_id}/state",
        headers=HEADERS,
    )
    resp.raise_for_status()
    return resp.json()


def main():
    if not BASE_URL or not API_KEY:
        print("ERROR: Set LANGGRAPH_URL and LANGCHAIN_API_KEY environment variables")
        sys.exit(1)

    input_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("compiled_inputs/nyarko")
    if not input_dir.is_dir():
        print(f"ERROR: {input_dir} is not a directory")
        sys.exit(1)

    inputs = _load_raw(input_dir)
    print("=" * 60)
    print("LangGraph Cloud Smoke Test")
    print("=" * 60)
    print(f"  Directory:   {input_dir}")
    print(f"  XML:         {'yes' if 'loan_file_xml' in inputs else 'NO'} ({len(inputs.get('loan_file_xml',''))} chars)")
    print(f"  Manifest:    {'yes' if 'manifest_json' in inputs else 'NO'} ({len(inputs.get('manifest_json',''))} chars)")
    print(f"  Eligibility: {'yes' if 'eligibility_json' in inputs else 'NO'} ({len(inputs.get('eligibility_json',''))} chars)")
    print(f"  Base URL:    {BASE_URL}")
    print("=" * 60)

    thread_id = create_thread()
    run_id = start_run(thread_id, inputs)

    print("\nPolling for completion...")
    run = poll_run(thread_id, run_id)

    status = run.get("status")
    print(f"\nRun finished with status: {status}")

    if status == "error":
        join_resp = requests.get(
            f"{BASE_URL}/threads/{thread_id}/runs/{run_id}/join",
            headers=HEADERS,
        )
        if join_resp.ok:
            err_data = join_resp.json()
            print(f"\nError details:\n{json.dumps(err_data, indent=2)}")
        else:
            print(f"\nRun error (no details from join endpoint)")
        sys.exit(1)

    print("\nFetching final thread state...")
    state = get_thread_state(thread_id)
    values = state.get("values", {})

    final_output = values.get("final_output")
    if final_output:
        out_path = Path("cloud_test_output.json")
        out_path.write_text(json.dumps(final_output, indent=2, default=str))
        print(f"\nFinal output saved to {out_path}")

        document_requests = final_output.get("document_requests", [])
        hard_stops = sum(1 for dr in document_requests if dr.get("severity") == "HARD-STOP")

        by_category: dict[str, int] = {}
        by_priority: dict[str, int] = {}
        for dr in document_requests:
            cat = dr.get("document_category", "Other")
            pri = dr.get("priority", "P3")
            by_category[cat] = by_category.get(cat, 0) + 1
            by_priority[pri] = by_priority.get(pri, 0) + 1

        print(f"\n  Total document requests: {len(document_requests)}")
        print(f"  Hard stops: {hard_stops}")
        print(f"  By priority: {by_priority}")
        print(f"  By category: {by_category}")

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

        stats = final_output.get("stats", {})
        if stats:
            print(f"\n  Stats: {json.dumps(stats, indent=4)}")
    else:
        print("\nNo final_output found in state. Dumping available keys:")
        for k in sorted(values.keys()):
            v = values[k]
            if isinstance(v, str) and len(v) > 200:
                print(f"  {k}: <string, {len(v)} chars>")
            elif isinstance(v, list):
                print(f"  {k}: <list, {len(v)} items>")
            elif isinstance(v, dict):
                print(f"  {k}: <dict, {len(v)} keys>")
            else:
                print(f"  {k}: {v}")

    full_state_path = Path("cloud_test_state.json")
    serializable = {}
    for k, v in values.items():
        if k in ("loan_file_xml", "manifest_json", "eligibility_json") and isinstance(v, str):
            serializable[k] = v[:200] + f"... ({len(v)} chars total)"
        elif k == "messages":
            serializable[k] = f"<{len(v)} messages>"
        else:
            serializable[k] = v
    full_state_path.write_text(json.dumps(serializable, indent=2, default=str))
    print(f"State summary saved to {full_state_path}")


if __name__ == "__main__":
    main()
