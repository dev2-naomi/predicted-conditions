"""
offline_verify.py — Replay captured runs through the deterministic merge.

Takes the per-run document-type lists from variance_report.json, fetches the
(shared) scenario_summary from the source thread, applies the new deterministic
rules to each run, and reports whether they collapse to a stable set.
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from tools.merger_tools import _canonical_doc_type
from tools.doc_rules import apply_deterministic_rules

BASE = os.getenv("LANGGRAPH_URL", "")
KEY = os.getenv("API_KEY") or os.getenv("LANGCHAIN_API_KEY", "")
H = {"x-api-key": KEY}


def fetch_scenario(thread_id: str) -> dict:
    r = requests.get(f"{BASE}/threads/{thread_id}/state", headers=H)
    r.raise_for_status()
    return r.json().get("values", {}).get("scenario_summary", {})


def canon_set(types: list[str]) -> tuple[str, ...]:
    return tuple(sorted({_canonical_doc_type(t) for t in types}))


def main():
    report = json.loads(Path("variance_report.json").read_text())
    source = report["source"]
    results = [r for r in report["results"] if r.get("types")]

    print(f"Fetching scenario_summary from {source} ...")
    ss = fetch_scenario(source)
    flags = None

    print("\n=== BEFORE (raw LLM output) ===")
    raw_counts = [len(r["types"]) for r in results]
    raw_sets = Counter(tuple(sorted(r["types"])) for r in results)
    print(f"  counts: {sorted(raw_counts)}  unique sets: {len(raw_sets)}")

    print("\n=== BEFORE (canonicalized only, no rules) ===")
    canon_counts = [len(canon_set(r["types"])) for r in results]
    canon_sets = Counter(canon_set(r["types"]) for r in results)
    print(f"  counts: {sorted(canon_counts)}  unique sets: {len(canon_sets)}")

    print("\n=== AFTER (canonicalize + deterministic rules) ===")
    final_sets = []
    for r in results:
        docs = [{"document_type": t} for t in r["types"]]
        final, stats = apply_deterministic_rules(docs, ss, canonical_fn=_canonical_doc_type)
        flags = stats["flags"]
        fset = tuple(sorted({_canonical_doc_type(d["document_type"]) for d in final}))
        final_sets.append(fset)
    final_counts = [len(f) for f in final_sets]
    final_set_counter = Counter(final_sets)
    print(f"  flags: {flags}")
    print(f"  counts: {sorted(final_counts)}  unique sets: {len(final_set_counter)}")

    # Show the dominant set and any deviations
    dominant, dom_freq = final_set_counter.most_common(1)[0]
    print(f"\n  Dominant set ({dom_freq}/{len(results)} runs, {len(dominant)} docs):")
    for d in dominant:
        print(f"    - {d}")

    if len(final_set_counter) > 1:
        print("\n  Deviations from dominant set:")
        for fset, freq in final_set_counter.items():
            if fset == dominant:
                continue
            extra = set(fset) - set(dominant)
            missing = set(dominant) - set(fset)
            print(f"    [{freq} run(s)] +{sorted(extra)} -{sorted(missing)}")
    else:
        print("\n  ALL RUNS COLLAPSED TO ONE IDENTICAL SET ✓")


if __name__ == "__main__":
    main()
