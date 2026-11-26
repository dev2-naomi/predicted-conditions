"""Thread metadata store (minimal LangGraph Platform compatibility).

Ported from the monte-carlo-intelligence / LG-docsOrch AWS migration
reference — generic, not agent-specific. Uses the same DynamoDB table as the
checkpointer (CHECKPOINT_TABLE_NAME) with a PK/SK layout that coexists with
checkpoint items (thread metadata uses SK="METADATA"; langgraph-checkpoint-aws
owns its own SK scheme for actual checkpoints).

Background-run records live in the same table under PK=f"RUN#{run_id}", so a
client polling GET /threads/{thread_id}/runs/{run_id} (see test_cloud.py,
run_manifest_cloud.py, etc.) gets an O(1) lookup.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

RUN_TERMINAL_STATUSES = frozenset({"success", "error", "timeout", "interrupted"})

# Leaves headroom under DynamoDB's 400KB hard item-size cap for every OTHER
# attribute that lives on the same RUN# record (run_id, thread_id,
# assistant_id, status, both timestamps, error, and -- the one that can
# independently grow after creation -- result). Mirrors the ~350KB threshold
# api/checkpointer.py's DynamoDBSaver already uses for its own s3_offload_config
# on the same underlying cap; a little more conservative here specifically
# because `result` is written later via a separate update_item call onto
# this same item and isn't accounted for at create_run() time.
_INPUT_PAYLOAD_S3_THRESHOLD_BYTES = 300_000


def _floats_to_decimal(value: Any) -> Any:
    """Recursively convert Python ``float`` values to ``Decimal``.

    DynamoDB's Number type has no native float, and boto3's high-level
    Table resource enforces this strictly: any native ``float`` anywhere in
    an item raises ``TypeError: Float types are not supported. Use Decimal
    types instead.`` on put_item/update_item.

    This matters a lot here because ``run_body``/``result`` are arbitrary,
    deeply-nested request/response payloads (loan_file_xml-derived numeric
    fields, loan_amount, interest rates, LTV percentages, etc.) that almost
    always contain at least one Python float once JSON-decoded — so without
    this conversion, DynamoDBThreadStore.create_run() fails outright (with
    exactly that "Float types are not supported" error) before a run_id
    ever exists, surfacing to callers as a generic 500 on
    POST /threads/{thread_id}/runs with no run record created at all
    (confirmed live 2026-10-14: a thread left permanently "idle" with no
    corresponding RUN# record, because create_run's put_item raised before
    any run was persisted).
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _floats_to_decimal(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_floats_to_decimal(v) for v in value]
    return value


def _decimals_to_native(value: Any) -> Any:
    """Reverse of ``_floats_to_decimal`` — convert ``Decimal`` back to a
    native ``int``/``float`` when reading an item back out of DynamoDB.

    Without this, every consumer that reads a persisted run back out (most
    importantly, every background-run dispatch path -- Fargate, Lambda
    self-invoke, and the local-thread fallback -- all retrieve run_body
    exclusively via load_persisted_run()/get_run(), never from the original
    in-process object) would hand the graph a Decimal-laden input_payload
    instead of plain numbers. Decimal refuses direct comparison against
    float (`TypeError: unsupported operand type(s) for >: 'decimal.Decimal'
    and 'float'`), which would quietly break arithmetic deep in the graph
    (e.g. tools/scenario_tools.py's loan_amount threshold checks) on every
    background run, not just ones that happen to trip it like the earlier,
    unrelated ad-hoc-script version of this same class of bug.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _decimals_to_native(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_decimals_to_native(v) for v in value]
    return value


class ThreadStore:
    def create(self, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        raise NotImplementedError

    def get(self, thread_id: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def record_run(self, thread_id: str, *, assistant_id: str, metadata: dict[str, Any] | None = None) -> None:
        raise NotImplementedError

    def create_run(
        self,
        thread_id: str,
        *,
        assistant_id: str,
        run_body: dict[str, Any],
    ) -> dict[str, Any]:
        raise NotImplementedError

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def update_run(
        self,
        run_id: str,
        *,
        status: str | None = None,
        result: Any = None,
        error: str | None = None,
    ) -> None:
        raise NotImplementedError


class InMemoryThreadStore(ThreadStore):
    def __init__(self) -> None:
        self._threads: dict[str, dict[str, Any]] = {}
        self._runs: dict[str, dict[str, Any]] = {}

    def create(self, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        thread_id = str(uuid.uuid4())
        record = {
            "thread_id": thread_id,
            "created_at": datetime.now(UTC).isoformat(),
            "metadata": metadata or {},
            "status": "idle",
        }
        self._threads[thread_id] = record
        return record

    def get(self, thread_id: str) -> dict[str, Any] | None:
        return self._threads.get(thread_id)

    def record_run(self, thread_id: str, *, assistant_id: str, metadata: dict[str, Any] | None = None) -> None:
        record = self._threads.get(thread_id)
        if not record:
            return
        merged = dict(record.get("metadata") or {})
        merged["assistant_id"] = assistant_id
        if metadata:
            merged.update(metadata)
        record["metadata"] = merged
        record["status"] = "busy"

    def create_run(
        self,
        thread_id: str,
        *,
        assistant_id: str,
        run_body: dict[str, Any],
    ) -> dict[str, Any]:
        run_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        record = {
            "run_id": run_id,
            "thread_id": thread_id,
            "assistant_id": assistant_id,
            "status": "pending",
            "input_payload": run_body,
            "created_at": now,
            "updated_at": now,
            "error": None,
            "result": None,
        }
        self._runs[run_id] = record
        return dict(record)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        record = self._runs.get(run_id)
        return dict(record) if record else None

    def update_run(
        self,
        run_id: str,
        *,
        status: str | None = None,
        result: Any = None,
        error: str | None = None,
    ) -> None:
        record = self._runs.get(run_id)
        if not record:
            return
        if status is not None:
            record["status"] = status
        if result is not None:
            record["result"] = result
        if error is not None:
            record["error"] = error
        record["updated_at"] = datetime.now(UTC).isoformat()


class DynamoDBThreadStore(ThreadStore):
    def __init__(self, table_name: str, *, region_name: str | None = None) -> None:
        import boto3

        self._table = boto3.resource("dynamodb", region_name=region_name).Table(table_name)
        self._region_name = region_name
        # Same bucket api/checkpointer.py's DynamoDBSaver already offloads
        # oversized checkpoints into (CHECKPOINT_S3_BUCKET, already
        # IAM-granted read/write to both the Lambda and the ECS worker task
        # role via infra/stacks/predicted_conditions_stack.py's
        # CheckpointOffloadBucket) — reused here rather than provisioning a
        # second bucket for the same underlying problem.
        self._s3_bucket = os.environ.get("CHECKPOINT_S3_BUCKET", "").strip()
        self._s3 = None  # lazy — only ever created if an oversized payload
        # actually needs offloading (or an offloaded one needs reading back)

    def _s3_client(self):
        if getattr(self, "_s3", None) is None:
            import boto3

            self._s3 = boto3.client("s3", region_name=getattr(self, "_region_name", None))
        return self._s3

    def _offload_input_payload_if_too_large(
        self, run_id: str, run_body: dict[str, Any]
    ) -> dict[str, str] | None:
        """Upload `run_body` to S3 instead of storing it inline, when it's too
        big to safely fit in the RUN# DynamoDB item alongside every other
        attribute. Returns the S3 pointer to store in DynamoDB, or None to
        store inline as before (either the payload is small enough, or no
        offload bucket is configured — in which case an oversized payload
        still fails loudly with DynamoDB's own ValidationException, exactly
        as it did before this fix, rather than silently losing data).

        See PC: compiled_inputs/montes (~455KB combined loan_file_xml +
        manifest_json + eligibility_json) hit exactly this cap and failed
        with a 500 before ever reaching the agent — confirmed via CloudWatch,
        documented in docs/pc-accuracy-uat-status.md.
        """
        # getattr defaults, not direct attribute access: tests (and any
        # other caller) that construct this class via
        # DynamoDBThreadStore.__new__(...) to stub out just `_table`,
        # bypassing __init__ entirely, must keep working exactly as before
        # this fix — no offload bucket configured, so skip straight to the
        # inline path below, same as always.
        s3_bucket = getattr(self, "_s3_bucket", "")
        try:
            serialized = json.dumps(run_body, default=str).encode("utf-8")
            size = len(serialized)
        except Exception:  # noqa: BLE001 - can't measure it -> treat as oversized
            serialized = None
            size = _INPUT_PAYLOAD_S3_THRESHOLD_BYTES + 1
        if size <= _INPUT_PAYLOAD_S3_THRESHOLD_BYTES or not s3_bucket:
            return None
        if serialized is None:
            serialized = json.dumps(run_body, default=str).encode("utf-8")
        key = f"runs/{run_id}/input.json"
        self._s3_client().put_object(
            Bucket=s3_bucket,
            Key=key,
            Body=serialized,
            ContentType="application/json",
        )
        return {"bucket": s3_bucket, "key": key}

    def _load_offloaded_input_payload(self, ref: dict[str, str]) -> dict[str, Any]:
        obj = self._s3_client().get_object(Bucket=ref["bucket"], Key=ref["key"])
        return json.loads(obj["Body"].read())

    def create(self, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        thread_id = str(uuid.uuid4())
        record = {
            "PK": f"THREAD#{thread_id}",
            "SK": "METADATA",
            "thread_id": thread_id,
            "created_at": datetime.now(UTC).isoformat(),
            "metadata": metadata or {},
            "status": "idle",
        }
        self._table.put_item(Item=record)
        return {
            "thread_id": thread_id,
            "created_at": record["created_at"],
            "metadata": record["metadata"],
            "status": record["status"],
        }

    def get(self, thread_id: str) -> dict[str, Any] | None:
        resp = self._table.get_item(Key={"PK": f"THREAD#{thread_id}", "SK": "METADATA"})
        item = resp.get("Item")
        if not item:
            return None
        return {
            "thread_id": item["thread_id"],
            "created_at": item.get("created_at", ""),
            "metadata": item.get("metadata", {}),
            "status": item.get("status", "idle"),
        }

    def record_run(self, thread_id: str, *, assistant_id: str, metadata: dict[str, Any] | None = None) -> None:
        record = self.get(thread_id)
        if not record:
            return
        merged = dict(record.get("metadata") or {})
        merged["assistant_id"] = assistant_id
        if metadata:
            merged.update(metadata)
        self._table.update_item(
            Key={"PK": f"THREAD#{thread_id}", "SK": "METADATA"},
            UpdateExpression="SET metadata = :metadata, #status = :status",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":metadata": merged,
                ":status": "busy",
            },
        )

    def create_run(
        self,
        thread_id: str,
        *,
        assistant_id: str,
        run_body: dict[str, Any],
    ) -> dict[str, Any]:
        run_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        item = {
            "PK": f"RUN#{run_id}",
            "SK": "METADATA",
            "run_id": run_id,
            "thread_id": thread_id,
            "assistant_id": assistant_id,
            "status": "pending",
            "created_at": now,
            "updated_at": now,
            "error": None,
            "result": None,
        }
        # Oversized run_body (loan_file_xml + manifest_json + eligibility_json
        # combined beyond DynamoDB's reach) goes to S3 with a pointer stored
        # instead of inline — see _offload_input_payload_if_too_large's
        # docstring. Everything else stores inline exactly as before.
        s3_ref = self._offload_input_payload_if_too_large(run_id, run_body)
        if s3_ref is not None:
            item["input_payload_ref"] = s3_ref
        else:
            item["input_payload"] = _floats_to_decimal(run_body)
        self._table.put_item(Item=item)
        # Convert Decimal back to native int/float on the way out — callers
        # further up the stack (e.g. execute_background_run's
        # graph.invoke(run_body.get("input"))) must get ordinary numbers,
        # not Decimals, for arithmetic to behave normally elsewhere in the
        # pipeline. Mirrors get_run()'s symmetric conversion below so every
        # read path (this immediate return, and any later
        # load_persisted_run()/get_run() re-fetch by a dispatch worker)
        # behaves identically. When offloaded, return the caller's own
        # run_body directly rather than round-tripping through S3 again
        # immediately after just writing it there.
        out = {k: v for k, v in item.items() if k not in ("PK", "SK", "input_payload_ref")}
        if s3_ref is not None:
            out["input_payload"] = run_body
        return _decimals_to_native(out)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        resp = self._table.get_item(Key={"PK": f"RUN#{run_id}", "SK": "METADATA"})
        item = resp.get("Item")
        if not item:
            return None
        s3_ref = item.pop("input_payload_ref", None)
        if s3_ref is not None:
            # Decimal-free by construction (round-tripped through plain
            # json.dumps/json.loads on the S3 write/read side, never through
            # DynamoDB's Decimal-only Number type), but _decimals_to_native
            # below is a harmless no-op on non-Decimal values either way, so
            # merging it in before that call keeps this single return path.
            item["input_payload"] = self._load_offloaded_input_payload(s3_ref)
        return _decimals_to_native({k: v for k, v in item.items() if k not in ("PK", "SK")})

    def update_run(
        self,
        run_id: str,
        *,
        status: str | None = None,
        result: Any = None,
        error: str | None = None,
    ) -> None:
        updates: dict[str, Any] = {"updated_at": datetime.now(UTC).isoformat()}
        if status is not None:
            updates["status"] = status
        if result is not None:
            # Same float->Decimal necessity as create_run's input_payload:
            # the final graph result nearly always carries at least one
            # native float (loan amounts, ratios, etc.) somewhere in its
            # (potentially deeply nested) document_requests/scenario_summary
            # structures, and boto3's Table resource rejects those outright.
            updates["result"] = _floats_to_decimal(result)
        if error is not None:
            updates["error"] = error

        expr_names = {f"#{k}": k for k in updates}
        expr_values = {f":{k}": v for k, v in updates.items()}
        set_clause = ", ".join(f"#{k} = :{k}" for k in updates)
        self._table.update_item(
            Key={"PK": f"RUN#{run_id}", "SK": "METADATA"},
            UpdateExpression=f"SET {set_clause}",
            ExpressionAttributeNames=expr_names,
            ExpressionAttributeValues=expr_values,
        )


_thread_store: ThreadStore | None = None


def get_thread_store() -> ThreadStore:
    global _thread_store
    if _thread_store is not None:
        return _thread_store

    table_name = os.environ.get("CHECKPOINT_TABLE_NAME", "").strip()
    if table_name:
        region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-2"))
        _thread_store = DynamoDBThreadStore(table_name, region_name=region)
    else:
        _thread_store = InMemoryThreadStore()
    return _thread_store
