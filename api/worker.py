"""ECS Fargate entrypoint for background runs that exceed Lambda's 900s cap.

Invoked as `python3 -m api.worker` (command override on the shared Docker
image — see infra/stacks/predicted_conditions_stack.py's WorkerTaskDefinition,
which overrides both entry_point and command: this image's base,
public.ecr.aws/lambda/python:3.12, normally ENTRYPOINTs into the Lambda
Runtime Interface Client via /lambda-entrypoint.sh, which would otherwise try
to poll the Lambda Runtime API — nonexistent outside Lambda — and hang
forever instead of running this script).

Dispatched by api/services/runs.py:_dispatch_via_fargate via ecs.run_task,
with the run_id passed as a per-task container environment override
(PC_RUN_ID) — everything else (thread_id, assistant_id, input payload) is
looked up from the persisted run record via load_persisted_run, same as the
existing Lambda-self-invoke path.
"""

from __future__ import annotations

import logging
import os
import sys
import threading

logger = logging.getLogger(__name__)


def _run_id() -> str:
    run_id = os.environ.get("PC_RUN_ID", "").strip()
    if not run_id:
        raise SystemExit("PC_RUN_ID must be set (container override from ecs.run_task)")
    return run_id


def _mark_timeout(run_id: str) -> None:
    # Best-effort — if this itself fails, os._exit still fires right after
    # and at least the ECS task stops (rather than running forever), even
    # though the run record would then be left stuck on "running" (same
    # class of orphaned-record issue the Lambda hard-timeout had, just with
    # a much larger ceiling here so it should be rare in practice).
    try:
        from api.thread_store import get_thread_store

        get_thread_store().update_run(
            run_id,
            status="error",
            error=(
                f"Fargate worker exceeded its own safety-net wall-clock limit "
                f"({os.environ.get('PC_WORKER_MAX_RUN_SECONDS', '?')}s) without "
                "completing. This is a safety net, not Lambda's 900s ceiling — "
                "if runs are routinely hitting this, raise "
                "PC_WORKER_MAX_RUN_SECONDS or investigate the run itself."
            ),
        )
    except Exception:  # noqa: BLE001 - we're about to os._exit regardless
        logger.exception("Failed to record timeout status for run %s", run_id)


def main() -> None:
    logging.basicConfig(level=logging.INFO)

    from api.secrets import load_secrets

    load_secrets()

    run_id = _run_id()
    logger.info("Fargate worker starting for run_id=%s", run_id)

    # Wall-clock safety net against a hung/runaway task — Fargate itself has
    # no execution-time ceiling (that's the whole point of this worker), so
    # without this a stuck run would run (and bill) forever. Deliberately
    # much larger than Lambda's 900s cap and than this agent's normal
    # 8-13 min runtime; this is a backstop for genuine pathological hangs,
    # not a tight budget. Uses os._exit (not sys.exit) since the main
    # thread will be blocked inside a synchronous graph.invoke() call with
    # no cooperative cancellation point.
    max_seconds = int(os.environ.get("PC_WORKER_MAX_RUN_SECONDS", str(30 * 60)))
    guard = threading.Timer(max_seconds, lambda: (_mark_timeout(run_id), os._exit(1)))
    guard.daemon = True
    guard.start()

    try:
        from api.services.runs import execute_background_run, load_persisted_run

        thread_id, assistant_id, run_body = load_persisted_run(run_id)
        result = execute_background_run(thread_id, run_id, assistant_id, run_body)
        logger.info("Fargate worker finished run_id=%s status=%s", run_id, result.get("status"))
    finally:
        guard.cancel()


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 - make sure the task exits non-zero and logs
        logger.exception("Fargate worker crashed")
        sys.exit(1)
