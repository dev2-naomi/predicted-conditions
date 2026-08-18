"""
cloud_consistency.py — Run N scenarios M times each on the DEPLOYED cloud and
measure per-scenario document-set stability.

Usage:
    python cloud_consistency.py [runs_per_scenario]
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

from tools.merger_tools import _canonical_doc_type

BASE = os.getenv("LANGGRAPH_URL", "")
KEY = os.getenv("API_KEY") or os.getenv("LANGCHAIN_API_KEY", "")
ASSISTANT_ID = "predicted-conditions"
H = {"x-api-key": KEY, "Content-Type": "application/json"}

SCENARIOS = ["alaska", "bibby", "niccum", "nyarko", "weingarten"]


def _load(scenario: str) -> dict:
    d = Path("compiled_inputs") / scenario
    inp = {}
    for f in d.iterdir():
        if f.suffix.lower() == ".xml":
            inp["loan_file_xml"] = f.read_text(encoding="utf-8", errors="replace")
    man = d / "manifest.json"
    elig = d / "eligibility.json"
    if man.exists():
        inp["manifest_json"] = man.read_text(encoding="utf-8")
    if elig.exists():
        inp["eligibility_json"] = elig.read_text(encoding="utf-8")
    return inp


def run_once(scenario: str, run_idx: int, inputs: dict) -> dict:
    t0 = time.time()
    try:
        tid = requests.post(f"{BASE}/threads", headers=H, json={}).json()["thread_id"]
        payload = {"assistant_id": ASSISTANT_ID,
                   "input": {**inputs, "current_step": "STEP_00"},
                   "config": {"recursion_limit": 250}}
        rid = requests.post(f"{BASE}/threads/{tid}/runs", headers=H, json=payload).json()["run_id"]
        terminal = {"success", "error", "timeout", "interrupted"}
        status = "pending"
        while time.time() - t0 < 1200:
            time.sleep(15)
            status = requests.get(f"{BASE}/threads/{tid}/runs/{rid}", headers=H).json().get("status", "?")
            if status in terminal:
                break
        if status != "success":
            return {"scenario": scenario, "run": run_idx, "ok": False,
                    "status": status, "thread": tid, "elapsed": int(time.time() - t0)}
        vals = requests.get(f"{BASE}/threads/{tid}/state", headers=H).json().get("values", {})
        drs = (vals.get("final_output", {}) or {}).get("document_requests", [])
        canon = sorted({_canonical_doc_type(d.get("document_type") or "?") for d in drs})
        return {"scenario": scenario, "run": run_idx, "ok": True, "thread": tid,
                "n": len(canon), "types": canon, "elapsed": int(time.time() - t0)}
    except Exception as e:
        return {"scenario": scenario, "run": run_idx, "ok": False,
                "status": f"exc:{e}"[:200], "elapsed": int(time.time() - t0)}


def main():
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    loaded = {sc: _load(sc) for sc in SCENARIOS}
    tasks = [(sc, i) for sc in SCENARIOS for i in range(runs)]
    print(f"Cloud: {len(SCENARIOS)} scenarios x {runs} runs = {len(tasks)} runs\n")

    results = []
    with ThreadPoolExecutor(max_workers=len(tasks)) as ex:
        futs = [ex.submit(run_once, sc, i, loaded[sc]) for sc, i in tasks]
        done = 0
        for f in as_completed(futs):
            r = f.result()
            results.append(r)
            done += 1
            info = f"n={r['n']}" if r["ok"] else f"FAIL {r.get('status','')}"
            print(f"  [{done}/{len(tasks)}] {r['scenario']}#{r['run']} {info} ({r['elapsed']}s)")
            Path("cloud_consistency_results.json").write_text(json.dumps(results, indent=2))

    print("\n" + "=" * 72)
    print("CLOUD CONSISTENCY REPORT (deployed deterministic build)")
    print("=" * 72)
    for sc in SCENARIOS:
        rs = [r for r in results if r["scenario"] == sc and r["ok"]]
        if not rs:
            fails = [r for r in results if r["scenario"] == sc]
            print(f"\n{sc}: 0 successful ({[r.get('status') for r in fails]})")
            continue
        counts = [r["n"] for r in rs]
        sets = Counter(tuple(r["types"]) for r in rs)
        dom, dfreq = sets.most_common(1)[0]
        verdict = "STABLE" if len(sets) == 1 else f"{len(sets)} sets"
        print(f"\n{sc}: {len(rs)} runs | counts={sorted(counts)} | unique_sets={len(sets)} "
              f"[{verdict}] | dominant={dfreq}/{len(rs)} ({len(dom)} docs)")
        for st, freq in sets.items():
            if st == dom:
                continue
            print(f"    [{freq}x] +{sorted(set(st)-set(dom))} -{sorted(set(dom)-set(st))}")

    print("\nSaved to cloud_consistency_results.json")


if __name__ == "__main__":
    main()
