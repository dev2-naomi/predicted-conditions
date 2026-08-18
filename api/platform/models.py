"""Pydantic models for LangGraph Platform-compatible requests."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class RunCreate(BaseModel):
    assistant_id: str
    input: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any] | None = None
    stream_mode: list[str] | str | None = None


class ThreadCreate(BaseModel):
    metadata: dict[str, Any] | None = None


class AssistantSearch(BaseModel):
    metadata: dict[str, Any] | None = None
    limit: int = 10
    offset: int = 0
