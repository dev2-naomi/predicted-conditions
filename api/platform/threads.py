"""LangGraph Platform-compatible thread endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from api.platform.models import ThreadCreate
from api.services.state import get_thread_state
from api.thread_store import get_thread_store

router = APIRouter(tags=["threads"])


@router.post("/threads")
async def create_thread(body: ThreadCreate | None = None):
    metadata = body.metadata if body else None
    return get_thread_store().create(metadata)


@router.get("/threads/{thread_id}")
async def get_thread(thread_id: str):
    record = get_thread_store().get(thread_id)
    if not record:
        raise HTTPException(status_code=404, detail="Thread not found")
    return record


@router.get("/threads/{thread_id}/state")
async def get_thread_state_endpoint(
    thread_id: str,
    assistant_id: str | None = Query(default=None),
):
    try:
        return get_thread_state(thread_id, assistant_id=assistant_id)
    except KeyError as exc:
        detail = exc.args[0] if exc.args else str(exc)
        if str(detail).startswith("Unknown assistant_id") or str(detail).startswith("Thread not found"):
            raise HTTPException(status_code=404, detail=str(detail)) from exc
        raise HTTPException(status_code=500, detail=str(detail)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
