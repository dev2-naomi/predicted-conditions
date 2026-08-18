"""
measure_variance.py — Quantify run-to-run document-count variance for a single loan.

Pulls the exact inputs from an existing cloud thread, then fires N fresh runs
of the SAME inputs in parallel and reports the distribution of document sets.

Usage:
    python measure_variance.py <source_thread_id> [N]
"""
from __future__ import annotations

import json
import os
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

BASE = os.getenv("LANGGRAPH_URL", "")
KEY = os.getenv("API_KEY") or os.getenv("LANGCHAIN_API_KEY", "")
ASSISTANT_ID = "predicted-conditions"
H = {"x-api-key": KEY, "Content-Type": "application/json"}


def get_state(thread_id: str) -> dict:
    r = requests.get(f"{BASE}/threads/{thread_id}/state", headers=H)
    r.raise_for_status()
    return r.json().get("values", {})


def pull_inputs(thread_id: str) -> dict:
    vals = get_state(thread_id)
    inputs = {}
    for k in ("loan_file_xml", "manifest_json", "eligibility_json"):
        v = vals.get(k)
        if v:
            inputs[k] = v
    return inputs


def run_once(inputs: dict, idx: int) -> dict:
    """Create a thread, start a run, poll to completion, return doc summary."""
    try:
        tr = requests.post(f"{BASE}/threads", headers=H, json={})
        tr.raise_for_status()
        tid = tr.json()["thread_id"]

        payload = {
            "assistant_id": ASSISTANT_ID,
            "input": {**inputs, "current_step": "STEP_00"},
            "config": {"recursion_limit": 250},
        }
        rr = requests.post(f"{BASE}/threads/{tid}/runs", headers=H, json=payload)
        rr.raise_for_status()
        rid = rr.json()["run_id"]

        start = time.time()
        terminal = {"success", "error", "timeout", "interrupted"}
        status = "pending"
        while time.time() - start < 900:
            time.sleep(15)
            sr = requests.get(f"{BASE}/threads/{tid}/runs/{rid}", headers=H)
            sr.raise_for_status()
            status = sr.json().get("status", "unknown")
            if status in terminal:
                break

        if status != "success":
            return {"idx": idx, "thread": tid, "status": status, "docs": None, "types": []}

        vals = get_state(tid)
        fo = vals.get("final_output", {}) or {}
        drs = fo.get("document_requests", [])
        types = sorted((dr.get("document_type") or "?") for dr in drs)
        return {
            "idx": idx,
            "thread": tid,
            "status": status,
            "docs": len(drs),
            "types": types,
            "elapsed": int(time.time() - start),
        }
    except Exception as e:
        return {"idx": idx, "thread": None, "status": f"exc:{e}", "docs": None, "types": []}


def main():
    if len(sys.argv) < 2:
        print("Usage: python measure_variance.py <source_thread_id> [N]")
        sys.exit(1)
    source = sys.argv[1]
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 8

    print(f"Pulling inputs from source thread {source} ...")
    inputs = pull_inputs(source)
    print(f"  loan_file_xml: {'yes' if 'loan_file_xml' in inputs else 'no'}")
    print(f"  manifest_json: {'yes' if 'manifest_json' in inputs else 'no'}")
    print(f"  eligibility_json: {'yes' if 'eligibility_json' in inputs else 'no'}")
    print(f"\nFiring {n} parallel runs...\n")

    results = []
    with ThreadPoolExecutor(max_workers=n) as ex:
        futs = [ex.submit(run_once, inputs, i) for i in range(n)]
        for f in as_completed(futs):
            res = f.result()
            results.append(res)
            print(f"  run {res['idx']}: status={res['status']} docs={res['docs']} ({res.get('elapsed','?')}s)")

    ok = [r for r in results if r["docs"] is not None]
    counts = [r["docs"] for r in ok]
    print("\n" + "=" * 70)
    print("VARIANCE REPORT")
    print("=" * 70)
    print(f"  Successful runs: {len(ok)}/{n}")
    if counts:
        print(f"  Doc count: min={min(counts)} max={max(counts)} spread={max(counts)-min(counts)}")
        print(f"  Distribution: {dict(sorted(Counter(counts).items()))}")

    # Unique document sets
    set_sigs = Counter(tuple(r["types"]) for r in ok)
    print(f"  Unique document SETS: {len(set_sigs)}")

    # Per-doc frequency: which docs are stable vs flaky
    all_docs = Counter()
    for r in ok:
        for t in set(r["types"]):
            all_docs[t] += 1
    print(f"\n  Per-document frequency (out of {len(ok)} runs):")
    stable, flaky = [], []
    for doc, freq in sorted(all_docs.items(), key=lambda x: (-x[1], x[0])):
        marker = "STABLE" if freq == len(ok) else "FLAKY "
        line = f"    [{marker}] {freq}/{len(ok)}  {doc}"
        (stable if freq == len(ok) else flaky).append(line)
    for l in stable:
        print(l)
    for l in flaky:
        print(l)

    out = Path("variance_report.json")
    out.write_text(json.dumps({"source": source, "n": n, "results": results}, indent=2))
    print(f"\nRaw results saved to {out}")


if __name__ == "__main__":
    main()
