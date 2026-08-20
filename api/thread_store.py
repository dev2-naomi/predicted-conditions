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
from typing import Any

RUN_TERMINAL_STATUSES = frozenset({"success", "error", "timeout", "interrupted"})


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
            "input_payload": run_body,
            "created_at": now,
            "updated_at": now,
            "error": None,
            "result": None,
        }
        self._table.put_item(Item=item)
        return {k: v for k, v in item.items() if k not in ("PK", "SK")}

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        resp = self._table.get_item(Key={"PK": f"RUN#{run_id}", "SK": "METADATA"})
        item = resp.get("Item")
        if not item:
            return None
        return {k: v for k, v in item.items() if k not in ("PK", "SK")}

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
            updates["result"] = result
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
