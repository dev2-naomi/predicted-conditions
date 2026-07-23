"""
test_coborrower.py — Local end-to-end test of the co-borrower document set.

Pulls two existing cloud threads: one is used as the BORROWER (its loan file +
manifest + eligibility), and the OTHER thread's manifest is reused as the
co-borrower's manifest (it's just a test fixture). The updated LOCAL `agent`
graph is then run in-process so the new co-borrower node is exercised without
needing a cloud redeploy.

Inputs are cached to test_results/coborrower_test/inputs/ on first fetch, so
re-runs are fully offline (no cloud, no re-download of the large manifests).

Usage:
    python test_coborrower.py                         # use cached inputs, else auto-pick two threads
    python test_coborrower.py <borrower_tid> <cob_tid> # fetch these specific threads (and cache)
    python test_coborrower.py --refetch               # force re-fetch from cloud
    python test_coborrower.py --fetch-only            # fetch + cache inputs, skip the pipeline run

Requires LANGGRAPH_URL + LANGCHAIN_API_KEY (thread fetch) and ANTHROPIC_API_KEY
(local pipeline run) in .env. Tip: set ANTHROPIC_MODEL=claude-haiku-4-5 for a
faster/cheaper functional test.
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
HEADERS = {"x-api-key": API_KEY, "Content-Type": "application/json"}

OUT_DIR = Path("test_results/coborrower_test")
INPUT_DIR = OUT_DIR / "inputs"
_INPUT_FILES = {
    "loan_file_xml": "loan_file.xml",
    "manifest_json": "manifest.json",
    "eligibility_json": "eligibility.json",
    "coborrower_manifest_json": "coborrower_manifest.json",
}


def save_inputs(inputs: dict, meta: dict) -> None:
    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    for key, fname in _INPUT_FILES.items():
        (INPUT_DIR / fname).write_text(inputs.get(key, "") or "", encoding="utf-8")
    (INPUT_DIR / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"  Cached inputs -> {INPUT_DIR}/")


def load_cached_inputs() -> dict | None:
    if not all((INPUT_DIR / f).exists() for f in _INPUT_FILES.values()):
        return None
    data = {k: (INPUT_DIR / f).read_text(encoding="utf-8") for k, f in _INPUT_FILES.items()}
    # Require at least a borrower manifest + co-borrower manifest to be useful.
    if not data.get("manifest_json") or not data.get("coborrower_manifest_json"):
        return None
    return data


def fetch_state(thread_id: str) -> dict:
    r = requests.get(f"{BASE_URL}/threads/{thread_id}/state", headers=HEADERS, timeout=60)
    r.raise_for_status()
    return r.json().get("values", {}) or {}


def search_threads(limit: int = 20) -> list[dict]:
    r = requests.post(f"{BASE_URL}/threads/search", headers=HEADERS, json={"limit": limit}, timeout=60)
    r.raise_for_status()
    return r.json()


def _has_inputs(vals: dict, *, need_xml: bool) -> bool:
    ok = bool(vals.get("manifest_json")) and bool(vals.get("eligibility_json"))
    if need_xml:
        ok = ok and bool(vals.get("loan_file_xml"))
    return ok


def pick_two_threads() -> tuple[str, str]:
    """Auto-pick two distinct threads: borrower needs xml+manifest+elig, the
    co-borrower just needs a manifest."""
    threads = search_threads(limit=20)
    borrower_tid = coborrower_tid = None
    for t in threads:
        tid = t.get("thread_id")
        vals = t.get("values", {}) or {}
        if borrower_tid is None and _has_inputs(vals, need_xml=True):
            borrower_tid = tid
            continue
        if coborrower_tid is None and tid != borrower_tid and bool(vals.get("manifest_json")):
            coborrower_tid = tid
        if borrower_tid and coborrower_tid:
            break
    if not (borrower_tid and coborrower_tid):
        print("ERROR: could not find two suitable threads")
        sys.exit(1)
    return borrower_tid, coborrower_tid


def _print_set(label: str, docs: list[dict]) -> None:
    print(f"\n  === {label} ({len(docs)} docs) ===")
    for dr in docs:
        specs = len(dr.get("specifications", []))
        sat = len(dr.get("satisfied_specifications", []))
        print(f"    [{dr.get('severity')}/{dr.get('priority')}] "
              f"{dr.get('document_type', '?')} ({dr.get('document_category', '?')}) "
              f"[{dr.get('status', '?')}] — {specs} needed / {sat} satisfied")


def fetch_and_cache(argv: list[str]) -> dict:
    """Fetch borrower + co-borrower inputs from cloud threads and cache them."""
    if not BASE_URL or not API_KEY:
        print("ERROR: set LANGGRAPH_URL and LANGCHAIN_API_KEY in .env")
        sys.exit(1)

    tids = [a for a in argv if not a.startswith("--")]
    if len(tids) >= 2:
        borrower_tid, coborrower_tid = tids[0], tids[1]
    else:
        borrower_tid, coborrower_tid = pick_two_threads()

    print(f"  Borrower thread   : {borrower_tid}")
    print(f"  Co-borrower thread: {coborrower_tid}")

    bvals = fetch_state(borrower_tid)
    cvals = fetch_state(coborrower_tid)

    inputs = {
        "loan_file_xml": bvals.get("loan_file_xml", "") or "",
        "manifest_json": bvals.get("manifest_json", "") or "",
        "eligibility_json": bvals.get("eligibility_json", "") or "",
        "coborrower_manifest_json": cvals.get("manifest_json", "") or "",
    }
    save_inputs(inputs, {"borrower_thread": borrower_tid, "coborrower_thread": coborrower_tid})
    return inputs


def main():
    argv = sys.argv[1:]
    refetch = "--refetch" in argv
    fetch_only = "--fetch-only" in argv

    print("=" * 68)
    print("  Co-borrower local E2E test")
    print("=" * 68)

    inputs = None if refetch else load_cached_inputs()
    if inputs is None:
        print("  Fetching inputs from cloud threads...")
        inputs = fetch_and_cache(argv)
    else:
        print(f"  Using cached inputs from {INPUT_DIR}/")

    loan_xml = inputs.get("loan_file_xml", "") or ""
    manifest = inputs.get("manifest_json", "") or ""
    elig = inputs.get("eligibility_json", "") or ""
    cob_manifest = inputs.get("coborrower_manifest_json", "") or ""

    print(f"  Borrower   : xml={len(loan_xml)}c manifest={len(manifest)}c elig={len(elig)}c")
    print(f"  Co-borrower: manifest={len(cob_manifest)}c (reused as test fixture)")
    print("=" * 68)

    if fetch_only:
        print("\n--fetch-only: inputs cached, skipping pipeline run.")
        return

    run_inputs = {
        "loan_file_xml": loan_xml,
        "manifest_json": manifest,
        "eligibility_json": elig,
        "coborrower": {"manifest_json": cob_manifest},
    }

    # Import after env is loaded so ANTHROPIC_API_KEY is picked up.
    from agent import agent

    print(f"\nRunning local pipeline with model={os.getenv('ANTHROPIC_MODEL', 'default')} "
          "(this invokes the LLM; may take a few min)...")
    start = time.time()
    result = agent.invoke(run_inputs, config={"recursion_limit": 250})
    print(f"Done in {time.time() - start:.1f}s")

    final = result.get("final_output") or {}
    docs = final.get("document_requests", [])
    borrower_docs = [d for d in docs if d.get("party") == "borrower"]
    cob_docs = [d for d in docs if d.get("party") == "coborrower"]

    print(f"\n  Total: {len(docs)}  | by_party: {final.get('stats', {}).get('by_party')}")
    _print_set("BORROWER", borrower_docs)
    _print_set("CO-BORROWER", cob_docs)

    # Sanity: same required-doc set, potentially different satisfaction.
    b_types = {d.get("document_type") for d in borrower_docs}
    c_types = {d.get("document_type") for d in cob_docs}
    print("\n  Doc-type sets identical:", b_types == c_types)
    if b_types != c_types:
        print("    only borrower:", b_types - c_types)
        print("    only coborrower:", c_types - b_types)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / "coborrower_output.json"
    out_path.write_text(json.dumps(final, indent=2, default=str))
    print(f"\n  Saved output -> {out_path}")


if __name__ == "__main__":
    main()
