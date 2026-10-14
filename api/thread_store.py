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

import os
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

RUN_TERMINAL_STATUSES = frozenset({"success", "error", "timeout", "interrupted"})


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
            "input_payload": _floats_to_decimal(run_body),
            "created_at": now,
            "updated_at": now,
            "error": None,
            "result": None,
        }
        self._table.put_item(Item=item)
        # Convert Decimal back to native int/float on the way out — callers
        # further up the stack (e.g. execute_background_run's
        # graph.invoke(run_body.get("input"))) must get ordinary numbers,
        # not Decimals, for arithmetic to behave normally elsewhere in the
        # pipeline. Mirrors get_run()'s symmetric conversion below so every
        # read path (this immediate return, and any later
        # load_persisted_run()/get_run() re-fetch by a dispatch worker)
        # behaves identically.
        return _decimals_to_native({k: v for k, v in item.items() if k not in ("PK", "SK")})

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        resp = self._table.get_item(Key={"PK": f"RUN#{run_id}", "SK": "METADATA"})
        item = resp.get("Item")
        if not item:
            return None
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
