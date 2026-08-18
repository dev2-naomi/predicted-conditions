"""Thread state helpers for LangGraph Platform compatibility."""

from __future__ import annotations

import dataclasses
from typing import Any

from api.checkpointer import get_checkpointer
from api.registry import build_graph, get_factory
from api.thread_store import get_thread_store


def _jsonable(value: Any) -> Any:
    """Recursively convert LangGraph's NamedTuple/dataclass state types
    (StateSnapshot, PregelTask, ...) into plain dicts/lists.

    FastAPI's default encoder treats NamedTuples as bare tuples (serializing
    to arrays, dropping field names), which would silently corrupt the
    state snapshot shape clients expect from LangGraph Platform.
    """
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {k: _jsonable(v) for k, v in dataclasses.asdict(value).items()}
    if hasattr(value, "_asdict"):  # NamedTuple (StateSnapshot, PregelTask, ...)
        return {k: _jsonable(v) for k, v in value._asdict().items()}
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _serialize_snapshot(snapshot: Any) -> dict[str, Any]:
    return _jsonable(snapshot)


def _assistant_for_thread(thread_id: str, assistant_id: str | None) -> str:
    if assistant_id:
        get_factory(assistant_id)
        return assistant_id
    thread = get_thread_store().get(thread_id)
    if not thread:
        raise KeyError(f"Thread not found: {thread_id}")
    stored = (thread.get("metadata") or {}).get("assistant_id")
    if isinstance(stored, str) and stored.strip():
        get_factory(stored)
        return stored.strip()
    return "predicted-conditions"


def get_thread_state(thread_id: str, *, assistant_id: str | None = None) -> dict[str, Any]:
    aid = _assistant_for_thread(thread_id, assistant_id)
    config: dict[str, Any] = {"configurable": {"thread_id": thread_id}}
    checkpointer = get_checkpointer()
    if checkpointer is None:
        raise RuntimeError("Thread state requires a configured checkpointer")
    graph = build_graph(aid, config, checkpointer=checkpointer)
    snapshot = graph.get_state(config)
    return _serialize_snapshot(snapshot)
