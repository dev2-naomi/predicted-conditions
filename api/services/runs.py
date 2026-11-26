"""Graph run execution helpers.

predicted-conditions runs typically take 8-11 minutes, comfortably inside
Lambda's 900s ceiling — but some inputs (more submitted documents -> more
satisfaction-check passes) legitimately run longer and hit that ceiling even
with no bugs/hangs involved (confirmed 2026-10-07: two real runs hard-killed
by AWS at exactly 900.00s per CloudWatch, with the run otherwise progressing
normally). Background runs now dispatch to an ECS Fargate task (no execution
time ceiling) when one is configured, falling back to the original Lambda
self-invoke (still 900s-capped) or a local thread when Fargate isn't set up
— see _dispatch_background_run's docstring for the exact priority order.
"""

from __future__ import annotations

import json
import logging
import os
import traceback
import uuid
from collections.abc import Iterator
from typing import Any

from api.checkpointer import get_checkpointer
from api.platform.sse import (
    _json_safe,
    format_end_event,
    format_error_event,
    format_metadata_event,
    format_sse_event,
    normalize_stream_modes,
    serialize_stream_payload,
    stream_event_name,
)
from api.registry import build_graph
from api.thread_store import get_thread_store

# Key used to tag a Lambda self-invoke payload as a background-run worker
# event rather than a normal Function URL / API Gateway HTTP event — checked
# in api/main.py's handler() before handing off to Mangum, since Mangum only
# understands HTTP-event shapes.
WORKER_EVENT_KEY = "predicted_conditions_worker"

# Margin under DynamoDB's hard 400KB item cap for a run record's `result`
# field. GET /threads/{thread_id}/state remains the source of truth for the
# full final state either way (it reads straight from the checkpointer,
# which supports S3 offload for oversized checkpoints).
_MAX_RUN_RESULT_BYTES = 300_000


def _capped_result(result: Any) -> Any:
    try:
        size = len(json.dumps(result, default=str))
    except Exception:  # noqa: BLE001 - if we can't even measure it, truncate
        size = _MAX_RUN_RESULT_BYTES + 1
    if size <= _MAX_RUN_RESULT_BYTES:
        return result
    return {
        "truncated": True,
        "reason": (
            f"Final result ({size} bytes) exceeds the run record's storage "
            f"limit ({_MAX_RUN_RESULT_BYTES} bytes) and was omitted. Fetch "
            "GET /threads/{thread_id}/state for the full final state instead "
            "— it has no size cap."
        ),
    }


def _merge_config(run_body: dict[str, Any], thread_id: str | None) -> dict[str, Any]:
    config: dict[str, Any] = dict(run_body.get("config") or {})
    configurable = dict(config.get("configurable") or {})
    if thread_id:
        configurable["thread_id"] = thread_id
    config["configurable"] = configurable
    return config


def _checkpointer_for_run(thread_id: str | None):
    if thread_id:
        return get_checkpointer()
    return None


def _record_thread_run(thread_id: str | None, assistant_id: str, run_body: dict[str, Any]) -> None:
    if not thread_id:
        return
    get_thread_store().record_run(thread_id, assistant_id=assistant_id)


def invoke_run(
    assistant_id: str,
    run_body: dict[str, Any],
    *,
    thread_id: str | None = None,
) -> dict[str, Any]:
    config = _merge_config(run_body, thread_id)
    _record_thread_run(thread_id, assistant_id, run_body)
    graph = build_graph(assistant_id, config, checkpointer=_checkpointer_for_run(thread_id))
    return graph.invoke(run_body.get("input") or {}, config=config)


def stream_run(
    assistant_id: str,
    run_body: dict[str, Any],
    *,
    thread_id: str | None = None,
) -> Iterator[str]:
    config = _merge_config(run_body, thread_id)
    _record_thread_run(thread_id, assistant_id, run_body)
    graph = build_graph(assistant_id, config, checkpointer=_checkpointer_for_run(thread_id))
    payload = run_body.get("input") or {}
    stream_modes = normalize_stream_modes(run_body)
    run_id = str(uuid.uuid4())

    yield format_metadata_event(run_id)

    try:
        for event in graph.stream(
            payload,
            config=config,
            stream_mode=stream_modes,
        ):
            if isinstance(event, tuple) and len(event) == 2:
                mode, event_payload = event
                yield format_sse_event(
                    stream_event_name(mode),
                    serialize_stream_payload(mode, event_payload),
                )
            else:
                yield format_sse_event("updates", serialize_stream_payload("updates", event))
    except Exception as exc:
        yield format_error_event(str(exc))
        raise
    finally:
        yield format_end_event()


# ── Background runs (Lambda self-invoke / local thread) ────────────────────
#
# The HTTP request that creates the run returns immediately with status
# "pending"; the client polls GET /threads/{thread_id}/runs/{run_id} for
# completion (test_cloud.py, run_manifest_cloud.py, etc. already do this
# against LangGraph Platform's identical wire format).


def load_persisted_run(run_id: str) -> tuple[str, str, dict[str, Any]]:
    """Look up a run's thread_id/assistant_id/input payload by run_id alone.

    Used by api/main.py's Lambda self-invoke handler — only run_id crosses
    the wire, so whichever invocation picks up the run looks everything else
    back up from the persisted record created by create_background_run.
    """
    run = get_thread_store().get_run(run_id)
    if not run:
        raise KeyError(f"Run not found: {run_id}")
    return run["thread_id"], run["assistant_id"], run.get("input_payload") or {}


def execute_background_run(
    thread_id: str,
    run_id: str,
    assistant_id: str,
    run_body: dict[str, Any],
) -> dict[str, Any]:
    """Actually execute a background run to completion (or failure).

    Deliberately never raises: whichever compute calls this (Lambda
    self-invoke or a local thread) needs the run record to reliably reach a
    terminal status so a polling client never sees a run stuck on "running"
    forever just because the worker process itself crashed calling this.
    """
    store = get_thread_store()
    store.update_run(run_id, status="running")
    config = _merge_config(run_body, thread_id)
    _record_thread_run(thread_id, assistant_id, run_body)
    try:
        graph = build_graph(assistant_id, config, checkpointer=_checkpointer_for_run(thread_id))
        result = graph.invoke(run_body.get("input") or {}, config=config)
    except Exception as exc:  # noqa: BLE001 - always record *a* terminal status
        # Full traceback, not just str(exc) — the bare message alone (e.g.
        # "cannot unpack non-iterable int object") gives no file/line/call
        # stack to debug from, and this exception is never otherwise logged
        # anywhere (no logger.exception call existed here before). Surfaced
        # on the run record's `error` field so it's visible via the normal
        # GET /threads/{id}/runs/{run_id} polling path without needing
        # CloudWatch access. Truncated defensively in case of a pathological
        # recursion-limit traceback.
        tb = traceback.format_exc()
        logging.getLogger(__name__).exception("Background run %s failed", run_id)
        store.update_run(run_id, status="error", error=tb[-4000:])
        return {"status": "error", "error": tb[-4000:]}
    # `result` is the raw graph.invoke() state — it holds LangChain message
    # objects (HumanMessage/AIMessage/...) that boto3's DynamoDB serializer
    # can't handle directly. _json_safe recursively converts pydantic models
    # to plain dicts via model_dump() first, matching the SSE streaming path.
    safe_result = _capped_result(_json_safe(result))
    try:
        store.update_run(run_id, status="success", result=safe_result)
    except Exception as exc:  # noqa: BLE001 - never strand the run on "running"
        try:
            store.update_run(
                run_id,
                status="success",
                result=str(safe_result),
                error=f"Result not fully serializable: {exc}",
            )
        except Exception as exc2:  # noqa: BLE001
            store.update_run(run_id, status="error", error=f"Failed to persist run result: {exc2}")
            return {"status": "error", "error": f"Failed to persist run result: {exc2}"}
        return {"status": "success", "result": str(safe_result)}
    return {"status": "success", "result": safe_result}


def create_background_run(
    thread_id: str,
    assistant_id: str,
    run_body: dict[str, Any],
) -> dict[str, Any]:
    """Create a run record and dispatch its execution asynchronously.

    Dispatch order (first configured mechanism wins):
      1. ECS Fargate (WORKER_TASK_DEFINITION_ARN set) — the normal path once
         the worker stack is deployed. No execution-time ceiling, so this is
         what actually fixes runs that legitimately exceed 900s.
      2. Lambda self-invoke (AWS_LAMBDA_FUNCTION_NAME set, but no Fargate
         config) — the original path, kept as a lighter-weight fallback.
         Still 900s-capped.
      3. A local Python thread — dev/test only, needs no AWS resources.
    """
    store = get_thread_store()
    run = store.create_run(thread_id, assistant_id=assistant_id, run_body=run_body)
    run_id = run["run_id"]

    try:
        _dispatch_background_run(run_id)
    except Exception as exc:
        store.update_run(run_id, status="error", error=f"dispatch failed: {exc}")
        raise

    return store.get_run(run_id) or run


def get_background_run(run_id: str) -> dict[str, Any] | None:
    return get_thread_store().get_run(run_id)


def _dispatch_background_run(run_id: str) -> None:
    task_definition_arn = os.environ.get("WORKER_TASK_DEFINITION_ARN", "").strip()
    if task_definition_arn:
        _dispatch_via_fargate(run_id, task_definition_arn)
        return

    function_name = os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "").strip()
    if function_name:
        _dispatch_via_lambda_self_invoke(run_id, function_name)
        return

    _dispatch_via_thread(run_id)


def _dispatch_via_fargate(run_id: str, task_definition_arn: str) -> None:
    """Launch an ECS Fargate task to run api/worker.py for this run_id.

    No execution-time ceiling (unlike Lambda's 900s), bounded instead by
    api/worker.py's own wall-clock safety-net timer. The task reads every
    other piece of the run (thread_id, assistant_id, input payload) back out
    of the persisted run record via load_persisted_run — only run_id crosses
    the wire here, via a per-task container environment override, matching
    the Lambda self-invoke path's existing pattern.
    """
    import boto3

    region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-2"))
    cluster_arn = os.environ["WORKER_CLUSTER_ARN"]
    subnet_ids = [s for s in os.environ.get("WORKER_SUBNET_IDS", "").split(",") if s]
    security_group_id = os.environ["WORKER_SECURITY_GROUP_ID"]
    container_name = os.environ["WORKER_CONTAINER_NAME"]

    ecs_client = boto3.client("ecs", region_name=region)
    ecs_client.run_task(
        cluster=cluster_arn,
        taskDefinition=task_definition_arn,
        launchType="FARGATE",
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": subnet_ids,
                "securityGroups": [security_group_id],
                # Public subnets, no NAT gateway (see the CDK stack) — the
                # task needs a public IP for outbound internet access
                # (DynamoDB/Secrets Manager/LLM provider APIs).
                "assignPublicIp": "ENABLED",
            }
        },
        overrides={
            "containerOverrides": [
                {
                    "name": container_name,
                    "environment": [{"name": "PC_RUN_ID", "value": run_id}],
                }
            ]
        },
    )


def _dispatch_via_lambda_self_invoke(run_id: str, function_name: str) -> None:
    """Async self-invoke. The invoked handler (api/main.py) looks up
    thread_id/assistant_id/input_payload from the run record itself via
    load_persisted_run — only run_id needs to cross the wire.
    """
    import boto3

    region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-2"))
    lambda_client = boto3.client("lambda", region_name=region)
    lambda_client.invoke(
        FunctionName=function_name,
        InvocationType="Event",
        Payload=json.dumps({WORKER_EVENT_KEY: {"run_id": run_id}}).encode("utf-8"),
    )


def _dispatch_via_thread(run_id: str) -> None:
    import threading

    thread_id, assistant_id, run_body = load_persisted_run(run_id)
    thread = threading.Thread(
        target=execute_background_run,
        args=(thread_id, run_id, assistant_id, run_body),
        daemon=True,
        name=f"predicted-conditions-run-{run_id[:8]}",
    )
    thread.start()
