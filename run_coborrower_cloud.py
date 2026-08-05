"""
run_coborrower_cloud.py — Send the three loan inputs to the cloud deployment and
save the party-tagged output. Borrower/co-borrower are auto-detected from the
eligibility JSON (falling back to the loan XML); there is no co-borrower input.

By default it reads the cached fixtures created by test_coborrower.py:
    test_results/coborrower_test/inputs/{loan_file.xml, manifest.json,
                                         eligibility.json}

Usage:
    python run_coborrower_cloud.py
    python run_coborrower_cloud.py <inputs_dir>
    python run_coborrower_cloud.py --resume <thread_id>

Resume: LangGraph Platform checkpoints state after every node, so a run that
died mid-pipeline (e.g. on an Anthropic overload) can be continued from its
last checkpoint instead of restarting. Pass --resume <thread_id> to start a
fresh run on the existing thread with no new input; the graph replays from the
last completed step rather than re-running steps 0..N.

Auto-resume: a normal trigger self-heals. If a run fails with a transient error
(overload / rate limit / 5xx / timeout), the script automatically resumes the
thread from its last checkpoint (up to a few attempts, with backoff), so a
single Anthropic overload spike no longer surfaces as a crashed run. A
non-transient error stops immediately so real bugs aren't retried blindly.

Requires LANGGRAPH_URL + LANGCHAIN_API_KEY in .env.
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
HEADERS = {"x-api-key": API_KEY, "Content-Type": "application/json"}


def _start_run(thread_id: str, payload_input: dict | None) -> str:
    """Start a run on *thread_id*. When payload_input is None the run resumes
    from the thread's last checkpoint (LangGraph Platform replays the graph)."""
    r = requests.post(
        f"{BASE_URL}/threads/{thread_id}/runs",
        headers=HEADERS,
        json={
            "assistant_id": ASSISTANT_ID,
            "input": payload_input,  # null → resume from last checkpoint
            "config": {"recursion_limit": 250},
        },
    )
    r.raise_for_status()
    run_id = r.json()["run_id"]
    print(f"Run: {run_id}")
    return run_id


def _poll(thread_id: str, run_id: str) -> str:
    start = time.time()
    terminal = {"success", "error", "timeout", "interrupted"}
    status = "unknown"
    while time.time() - start < 1800:
        try:
            r = requests.get(f"{BASE_URL}/threads/{thread_id}/runs/{run_id}",
                             headers=HEADERS, timeout=60)
            r.raise_for_status()
            status = r.json().get("status", "unknown")
            print(f"  [{int(time.time()-start):>4}s] status={status}")
            if status in terminal:
                break
        except requests.exceptions.RequestException as e:
            print(f"  poll error (retrying): {e}")
        time.sleep(15)
    return status


# Error signatures that are transient (Anthropic capacity / rate limit) and so
# worth auto-resuming. A deterministic code bug will surface a different message
# and repeat, so we cap attempts to avoid a crash loop.
_TRANSIENT_MARKERS = (
    "overloaded", "rate_limit", "rate limit", "529", "503", "502", "500",
    "timeout", "timed out", "overloaded_error",
)


def _error_details(thread_id: str, run_id: str) -> str:
    """Fetch the failed run's error text (best effort)."""
    try:
        jr = requests.get(f"{BASE_URL}/threads/{thread_id}/runs/{run_id}/join",
                          headers=HEADERS, timeout=60)
        return json.dumps(jr.json(), default=str) if jr.ok else ""
    except requests.exceptions.RequestException:
        return ""


def _looks_transient(err_text: str) -> bool:
    low = err_text.lower()
    return any(m in low for m in _TRANSIENT_MARKERS)


def _run_with_auto_resume(
    thread_id: str, first_input: dict | None, max_resumes: int = 6,
) -> str:
    """Start a run and, if it fails transiently, resume from the checkpoint.

    LangGraph Platform checkpoints after each node, so resuming (input=None)
    continues from the last completed step rather than restarting. This lets a
    single trigger ride out Anthropic overloads that outlast one run's retry
    budget, only re-running the step that actually failed.
    """
    payload = first_input
    for attempt in range(max_resumes + 1):
        run_id = _start_run(thread_id, payload)
        status = _poll(thread_id, run_id)
        if status != "error":
            return status

        err = _error_details(thread_id, run_id)
        print("ERROR:", err[:2000] if err else "no details")
        if attempt >= max_resumes:
            print(f"\nGiving up after {max_resumes} resume attempt(s).")
            return status
        if err and not _looks_transient(err):
            print("\nError does not look transient (not an overload); not auto-resuming.")
            return status

        wait = min(15 * (2 ** attempt), 120)
        print(f"\nTransient failure — auto-resuming thread from last checkpoint "
              f"in {wait}s (attempt {attempt + 1}/{max_resumes})...")
        time.sleep(wait)
        payload = None  # subsequent runs resume from checkpoint
    return status


def main():
    if not BASE_URL or not API_KEY:
        print("ERROR: set LANGGRAPH_URL and LANGCHAIN_API_KEY in .env")
        sys.exit(1)

    args = sys.argv[1:]
    resume_thread_id: str | None = None
    if args and args[0] == "--resume":
        if len(args) < 2:
            print("ERROR: --resume requires a <thread_id>")
            sys.exit(1)
        resume_thread_id = args[1]
        args = args[2:]

    if resume_thread_id:
        # Resume: continue the existing thread from its last checkpoint. No new
        # input is sent, so steps already completed are not re-run.
        thread_id = resume_thread_id
        first_input: dict | None = None
        print("=" * 60)
        print("  Co-borrower cloud run (RESUME)")
        print("=" * 60)
        print(f"  Resuming thread: {thread_id}")
        print("=" * 60)
    else:
        inputs_dir = Path(args[0]) if args else Path("test_results/coborrower_test/inputs")

        def _read(name: str) -> str:
            p = inputs_dir / name
            return p.read_text(encoding="utf-8") if p.exists() else ""

        first_input = {
            "loan_file_xml": _read("loan_file.xml"),
            "manifest_json": _read("manifest.json"),
            "eligibility_json": _read("eligibility.json"),
        }

        print("=" * 60)
        print("  Per-party attribution cloud run")
        print("=" * 60)
        print(f"  Inputs dir : {inputs_dir}")
        print(f"  Inputs     : xml={len(first_input['loan_file_xml'])}c "
              f"manifest={len(first_input['manifest_json'])}c "
              f"elig={len(first_input['eligibility_json'])}c")
        print("  Parties are auto-detected from eligibility (fallback: loan XML).")
        print("=" * 60)

        r = requests.post(f"{BASE_URL}/threads", headers=HEADERS, json={})
        r.raise_for_status()
        thread_id = r.json()["thread_id"]
        print(f"Thread: {thread_id}")

    status = _run_with_auto_resume(thread_id, first_input)

    if status == "error":
        print(
            f"\nRun still failing after auto-resume. State is checkpointed — "
            f"retry later from the last completed step with:\n"
            f"    python run_coborrower_cloud.py --resume {thread_id}"
        )
        sys.exit(1)

    r = requests.get(f"{BASE_URL}/threads/{thread_id}/state", headers=HEADERS, timeout=60)
    r.raise_for_status()
    final_output = r.json().get("values", {}).get("final_output")

    out_path = Path("test_results/coborrower_test/coborrower_cloud_output.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(final_output, indent=2, default=str))
    print(f"\nSaved -> {out_path}")

    if final_output:
        drs = final_output.get("document_requests", [])
        print(f"Total document requests: {len(drs)}")
        print(f"by_party: {final_output.get('stats', {}).get('by_party')}")
        for party in ("borrower", "coborrower"):
            subset = [d for d in drs if d.get("party") == party]
            print(f"\n  === {party.upper()} ({len(subset)}) ===")
            for dr in subset:
                print(f"    [{dr.get('severity')}/{dr.get('priority')}] "
                      f"{dr.get('document_type','?')} [{dr.get('status','?')}]")
    print(f"Thread ID: {thread_id}")


if __name__ == "__main__":
    main()
