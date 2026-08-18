"""Shared LangGraph checkpointer for local API dev and AWS Lambda.

Selection is entirely env-driven so ``agent.py:build_agent`` works unchanged
across ``langgraph dev`` (which never imports this module — it manages its
own persistence), the local FastAPI shim, and Lambda.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver


@lru_cache(maxsize=1)
def get_checkpointer() -> "BaseCheckpointSaver | None":
    """Return a module-scoped checkpointer when persistence is configured.

    - ``CHECKPOINT_TABLE_NAME`` set -> DynamoDB (Lambda / AWS)
    - ``CHECKPOINT_IN_MEMORY=true`` -> InMemorySaver (local API dev/tests)
    - otherwise -> None (threadless runs only; /threads/{id}/state and
      resumption require a checkpointer)

    ``CHECKPOINT_S3_BUCKET``, when set alongside ``CHECKPOINT_TABLE_NAME``,
    enables DynamoDBSaver's built-in S3 offload for checkpoints/writes over
    350KB — guards against DynamoDB's 400KB item cap on long runs with large
    message histories.
    """
    table_name = os.environ.get("CHECKPOINT_TABLE_NAME", "").strip()
    if table_name:
        from langgraph_checkpoint_aws import DynamoDBSaver

        region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-2"))
        s3_bucket = os.environ.get("CHECKPOINT_S3_BUCKET", "").strip()
        s3_offload_config = {"bucket_name": s3_bucket} if s3_bucket else None
        return DynamoDBSaver(
            table_name=table_name,
            region_name=region,
            s3_offload_config=s3_offload_config,
            enable_checkpoint_compression=True,
        )

    if os.environ.get("CHECKPOINT_IN_MEMORY", "").strip().lower() in ("1", "true", "yes"):
        from langgraph.checkpoint.memory import InMemorySaver

        return InMemorySaver()

    return None
