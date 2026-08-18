"""LangGraph Platform-compatible assistant endpoints."""

from __future__ import annotations

from fastapi import APIRouter

from api.platform.models import AssistantSearch
from api.registry import list_assistants

router = APIRouter(tags=["assistants"])


@router.post("/assistants/search")
async def assistants_search(body: AssistantSearch | None = None):
    assistants = list_assistants()
    if body:
        return assistants[body.offset : body.offset + body.limit]
    return assistants
