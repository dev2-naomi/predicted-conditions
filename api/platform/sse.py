"""Server-Sent Events helpers (LangGraph Platform wire format).

Generic LangGraph Platform SSE framing, ported unchanged from the
monte-carlo-intelligence / LG-docsOrch AWS migration reference.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any


def format_sse_event(event: str, data: Any) -> str:
    """Format one SSE frame. Empty line terminates the event per the SSE spec."""
    payload = "null" if data is None else json.dumps(data, default=_json_default)
    return f"event: {event}\ndata: {payload}\n\n"


def format_metadata_event(run_id: str) -> str:
    return format_sse_event("metadata", {"run_id": run_id})


def format_end_event() -> str:
    return format_sse_event("end", None)


def format_error_event(message: str) -> str:
    return format_sse_event("error", {"error": message, "message": message})


def stream_event_name(mode: str) -> str:
    """Map LangGraph stream modes to LangGraph Platform SSE event names."""
    if mode == "messages":
        return "messages/partial"
    return mode


def serialize_stream_payload(mode: str, payload: Any) -> Any:
    """Serialize graph.stream payloads for SSE data fields."""
    if mode == "messages-tuple":
        if isinstance(payload, tuple) and len(payload) == 2:
            message, metadata = payload
            return [_serialize_message(message), _json_safe(metadata)]
        return _json_safe(payload)

    if mode == "messages":
        if isinstance(payload, tuple) and len(payload) == 2:
            message, metadata = payload
            return [_serialize_message(message), _json_safe(metadata)]
        return _serialize_message(payload)

    return _json_safe(payload)


def normalize_stream_modes(run_body: dict[str, Any]) -> list[str]:
    """Resolve requested stream modes from a run request body."""
    raw = run_body.get("stream_mode")
    if raw is None:
        return ["messages-tuple", "messages"]
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list) and raw:
        return [str(mode) for mode in raw]
    return ["messages-tuple", "messages"]


def _serialize_message(message: Any) -> Any:
    if hasattr(message, "model_dump"):
        return message.model_dump()
    if isinstance(message, dict):
        return message
    content = getattr(message, "content", str(message))
    role = getattr(message, "type", None) or getattr(message, "role", "assistant")
    message_id = getattr(message, "id", None)
    result: dict[str, Any] = {"role": role, "content": content}
    if message_id is not None:
        result["id"] = message_id
    return result


def _json_safe(value: Any) -> Any:
    # Covers LangChain messages (pydantic models) and any dataclass-shaped
    # LangGraph types that show up in graph state/results and aren't
    # natively DynamoDB/JSON serializable.
    if hasattr(value, "model_dump"):
        return _json_safe(value.model_dump())
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _json_safe(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _json_default(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    return str(value)
