"""Graph run execution helpers.

Lambda-only (no Fargate): predicted-conditions runs typically take 8-11
minutes, comfortably inside Lambda's 900s ceiling (see the "When to add
Fargate?" ADR in the AWS deployment playbook — this agent doesn't need it).
Background runs (create_background_run/execute_background_run) still exist
so /threads/{id}/runs matches LangGraph Platform's async semantics and lets
callers avoid holding an HTTP connection open for 8-11 minutes; execution is
just dispatched to a Lambda self-invoke (or a local thread when running
outside Lambda) instead of an ECS task.
"""

from __future__ import annotations

import json
import os
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
        store.update_run(run_id, status="error", error=str(exc))
        return {"status": "error", "error": str(exc)}
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
      1. Lambda self-invoke (AWS_LAMBDA_FUNCTION_NAME set) — the normal path
         on the deployed Lambda. Still 900s-capped, which is fine here since
         full runs take 8-11 minutes.
      2. A local Python thread — dev/test only, needs no AWS resources.
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
    function_name = os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "").strip()
    if function_name:
        _dispatch_via_lambda_self_invoke(run_id, function_name)
        return

    _dispatch_via_thread(run_id)


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
