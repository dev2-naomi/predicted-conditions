"""Maps LangGraph Platform assistant_id values to graph factories.

predicted-conditions is a single-graph agent, so this registry has exactly
one entry. NOTE: this is deliberately a *different* module from the
top-level ``registry.py`` (the auto-generated STEP_CONFIG used by
step_loader.py) — Python resolves ``registry`` and ``api.registry`` as
distinct modules, so both coexist without collision.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver

from agent import build_agent

GraphFactory = Callable[..., Any]

ASSISTANTS: dict[str, dict[str, str]] = {
    "predicted-conditions": {
        "assistant_id": "predicted-conditions",
        "graph_id": "predicted-conditions",
        "name": "Predicted Conditions",
        "description": "Predictive underwriting conditions engine for non-QM mortgage loans.",
    },
}


def _predicted_conditions_factory(_run_config: RunnableConfig | dict[str, Any], *, checkpointer=None):
    return build_agent(checkpointer=checkpointer)


FACTORIES: dict[str, GraphFactory] = {
    "predicted-conditions": _predicted_conditions_factory,
}


def list_assistants() -> list[dict[str, str]]:
    return list(ASSISTANTS.values())


def get_factory(assistant_id: str) -> GraphFactory:
    if assistant_id not in FACTORIES:
        raise KeyError(f"Unknown assistant_id: {assistant_id}")
    return FACTORIES[assistant_id]


def build_graph(
    assistant_id: str,
    run_config: RunnableConfig | dict[str, Any],
    *,
    checkpointer: BaseCheckpointSaver | None = None,
):
    factory = get_factory(assistant_id)
    return factory(run_config, checkpointer=checkpointer)
