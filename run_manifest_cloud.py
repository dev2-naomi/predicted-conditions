"""Run one combined manifest+eligibility file through the cloud and save the output.

Usage:
    python run_manifest_cloud.py <combined_json_path> <loan_number>
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
API_KEY = os.getenv("API_KEY") or os.getenv("LANGCHAIN_API_KEY", "")
ASSISTANT_ID = "predicted-conditions"
HEADERS = {"x-api-key": API_KEY, "Content-Type": "application/json"}


def main():
    combined_path = Path(sys.argv[1])
    loan_number = sys.argv[2]
    raw = combined_path.read_text()

    # Combined file carries both the doc manifest (documents/...) and the
    # loan profile + eligibility result (entity.metadata). The pipeline's
    # manifest parser reads `documents`; the eligibility parser reads `entity`.
    inputs = {
        "loan_file_xml": "",
        "manifest_json": raw,
        "eligibility_json": raw,
        "current_step": "STEP_00",
    }

    print(f"Combined file: {combined_path} ({len(raw)} chars)")

    r = requests.post(f"{BASE_URL}/threads", headers=HEADERS, json={})
    r.raise_for_status()
    thread_id = r.json()["thread_id"]
    print(f"Thread: {thread_id}")

    r = requests.post(
        f"{BASE_URL}/threads/{thread_id}/runs",
        headers=HEADERS,
        json={
            "assistant_id": ASSISTANT_ID,
            "input": inputs,
            "config": {"recursion_limit": 250},
        },
    )
    r.raise_for_status()
    run_id = r.json()["run_id"]
    print(f"Run: {run_id}")

    start = time.time()
    terminal = {"success", "error", "timeout", "interrupted"}
    status = "unknown"
    while time.time() - start < 1800:
        try:
            r = requests.get(
                f"{BASE_URL}/threads/{thread_id}/runs/{run_id}",
                headers=HEADERS, timeout=60,
            )
            r.raise_for_status()
            status = r.json().get("status", "unknown")
            print(f"  [{int(time.time()-start):>4}s] status={status}")
            if status in terminal:
                break
        except requests.exceptions.RequestException as e:
            # Transient network blip on the local machine — the cloud run
            # keeps going, so just log and retry rather than crashing.
            print(f"  [{int(time.time()-start):>4}s] poll error (retrying): {e}")
        time.sleep(15)

    if status == "error":
        jr = requests.get(f"{BASE_URL}/threads/{thread_id}/runs/{run_id}/join", headers=HEADERS)
        print("ERROR:", json.dumps(jr.json(), indent=2)[:2000] if jr.ok else "no details")
        sys.exit(1)

    values = {}
    for attempt in range(6):
        try:
            r = requests.get(
                f"{BASE_URL}/threads/{thread_id}/state", headers=HEADERS, timeout=60,
            )
            r.raise_for_status()
            values = r.json().get("values", {})
            break
        except requests.exceptions.RequestException as e:
            print(f"  state fetch error (retry {attempt+1}/6): {e}")
            time.sleep(15)
    final_output = values.get("final_output")

    out_path = Path(f"output-manifest-{loan_number}.json")
    out_path.write_text(json.dumps(final_output, indent=2, default=str))
    print(f"\nSaved -> {out_path}")

    if final_output:
        drs = final_output.get("document_requests", [])
        print(f"Total document requests: {len(drs)}")
        for dr in drs:
            print(f"  [{dr.get('severity')}/{dr.get('priority')}] {dr.get('document_type','?')} "
                  f"({dr.get('document_category','?')}) [{dr.get('status','?')}]")
    print(f"Thread ID: {thread_id}")


if __name__ == "__main__":
    main()
