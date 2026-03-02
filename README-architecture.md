# Agentic Workflow Orchestrator — Architecture Reference

A reference architecture for building multi-step, tool-heavy agentic workflows on LangGraph Cloud. The system uses a single ReAct-style orchestrator agent that progresses through a configurable sequence of phases and steps — dynamically scoping its tools and instructions at each stage, and delegating specialized work to remote subagents.

This document describes the patterns, not a specific domain. Adapt the steps, tools, subagents, and state fields to your use case.

---

## Table of Contents

- [Core Concepts](#core-concepts)
- [Architecture Diagram](#architecture-diagram)
- [Agent Design Patterns](#agent-design-patterns)
  - [1. ReAct Orchestrator (No Explicit Edges)](#1-react-orchestrator-no-explicit-edges)
  - [2. Dynamic Tool Binding](#2-dynamic-tool-binding)
  - [3. Dynamic Plan Injection](#3-dynamic-plan-injection)
  - [4. Remote Subagents via Task Tool](#4-remote-subagents-via-task-tool)
  - [5. State Schema with Custom Reducers](#5-state-schema-with-custom-reducers)
  - [6. Middleware Stack](#6-middleware-stack)
  - [7. Config-Driven Workflow Generation](#7-config-driven-workflow-generation)
  - [8. Development Mode](#8-development-mode)
- [Project Layout](#project-layout)
- [Data Flow](#data-flow)
- [Deployment Model](#deployment-model)
- [Applying This Architecture](#applying-this-architecture)

---

## Core Concepts

| Concept | Description |
|---------|-------------|
| **Orchestrator** | A single LangGraph ReAct agent that drives the entire workflow. No explicit graph edges — the LLM decides what to do based on the current step's plan and tools. |
| **Step** | A discrete unit of work (e.g., "Data Gathering", "Compliance Check"). Steps execute sequentially. Each step has its own plan file and scoped tool set. |
| **Phase** | A logical grouping of related steps (e.g., "Verification", "Preparation"). Phases are organizational — the agent operates at the step level. |
| **Substep** | A granular task within a step (e.g., "Verify address", "Extract fields"). Tracked via a `write_todo` tool for status reporting. |
| **Tool Resolver** | A function called before every LLM invocation that returns only the tools relevant to the current step. Reduces context window by 60–75%. |
| **Plan Resolver** | A function that injects the current step's instructions as a transient system message (not persisted in chat history). |
| **Remote Subagent** | A separate LangGraph Cloud deployment invoked via a `task` tool for work that requires a different modality (e.g., GUI automation, browser use, specialized model). |
| **Registry** | A configuration layer mapping step IDs to tool names, plan files, and metadata. The single source of truth for workflow structure. |

---

## Architecture Diagram

```
┌───────────────────────────────────────────────────────────────┐
│                      LangGraph Cloud                          │
│                                                               │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │             Main Orchestrator Agent                     │  │
│  │                                                         │  │
│  │  ┌───────────┐ ┌───────────┐ ┌───────────┐ ┌────────┐  │  │
│  │  │   Tool    │ │   Plan    │ │   State   │ │ Summa- │  │  │
│  │  │  Resolver │ │  Resolver │ │  Reducers │ │ rizer  │  │  │
│  │  └─────┬─────┘ └─────┬─────┘ └─────┬─────┘ └────────┘  │  │
│  │        │              │             │                    │  │
│  │  ┌─────▼──────────────▼─────────────▼─────────────────┐  │  │
│  │  │              ReAct Agent Loop (LLM)                │  │  │
│  │  │    Step 1 → Step 2 → ... → Step N-1 → Step N      │  │  │
│  │  └───────┬────────────┬─────────────┬─────────────────┘  │  │
│  └──────────┼────────────┼─────────────┼────────────────────┘  │
│             │            │             │                        │
│  ┌──────────▼──┐  ┌──────▼─────┐  ┌───▼──────────┐            │
│  │ Subagent A  │  │ Subagent B │  │ Subagent C   │            │
│  │ (e.g. GUI   │  │ (e.g. data │  │ (e.g. screen │            │
│  │  automation) │  │  pipeline) │  │  capture)    │            │
│  └─────────────┘  └────────────┘  └──────────────┘            │
│                                                               │
└───────────────────────────────────────────────────────────────┘
                    │
     ┌──────────────┼──────────────┐
┌────▼─────┐  ┌─────▼─────┐  ┌────▼─────┐
│ External │  │  Storage  │  │ External │
│  API #1  │  │  (S3/DB)  │  │  API #2  │
└──────────┘  └───────────┘  └──────────┘
```

---

## Agent Design Patterns

### 1. ReAct Orchestrator (No Explicit Edges)

The orchestrator is a **single ReAct agent** — not a multi-node graph with hardcoded conditional edges. The LLM drives step progression by reading the system prompt, the injected plan, and the available tools.

```python
agent = create_deep_agent(
    agent_type="My-Orchestrator",
    middleware=(WorkflowMiddleware(),),
    system_prompt=system_instructions,
    tools=ALL_TOOLS,
    tool_resolver=resolve_tools_for_step,
    plan_resolver=resolve_plan_for_step,
    subagents=[subagent_a, subagent_b],
    context_schema=WorkflowState,
)
```

**Why this pattern instead of explicit graph edges:**

- A tool-heavy workflow with 50–100+ tools would require dozens of nodes and complex routing logic if modeled as a graph.
- The LLM is better at deciding "what to do next within this step" than a static conditional edge.
- Adding or reordering steps requires zero graph rewiring — just update the config and plan files.
- Step progression is tracked in state (`step_reports`), so the tool resolver always knows where the agent is.

**Trade-off:** The system prompt must be clear about step ordering and completion criteria, since there are no graph-level guardrails forcing transitions.

---

### 2. Dynamic Tool Binding

A `tool_resolver` function is called **before every LLM invocation**. It reads the current step from state and returns only the tools registered for that step, plus a set of general-purpose tools that are always available.

```
Total tools: 80+
Tools per step: 8–15  (60–75% context reduction)
```

**How it works:**

```python
def resolve_tools_for_step(state: dict) -> list:
    current_step = get_current_step(state)

    if is_step_skipped(current_step):
        return [write_todo]  # minimal tools for skipped steps

    step_tools = registry.get_tools(current_step)
    return GENERAL_TOOLS + step_tools
```

**The registry** maps each step to its allowed tool names:

```python
STEP_CONFIG = {
    "STEP_00": {
        "name": "Data Gathering",
        "tools": ["find_record", "extract_fields", "fetch_documents"],
        "plan_file": "data_gathering.md",
    },
    "STEP_01": {
        "name": "Validation",
        "tools": ["validate_address", "check_blockers"],
        "plan_file": "validation.md",
    },
    # ...
}
```

**General tools** (always available regardless of step):
- `write_todo` — track substep status
- `get_workflow_status` — get overall progress
- `add_flag` — flag issues
- `save_step_report` — save step summary
- `generate_report` — generate final report
- `task` — invoke remote subagents

**Benefits:**
- Prevents the LLM from calling tools that belong to a different step
- Dramatically reduces token usage from tool descriptions
- Makes it easy to test individual steps by scoping tool availability

---

### 3. Dynamic Plan Injection

A `plan_resolver` function is called **before every LLM invocation** and returns a markdown string that gets injected as a **transient system message** — present during that call but not persisted in the message history.

```python
def resolve_plan_for_step(state: dict) -> str | None:
    current_step = get_current_step(state)

    if is_step_skipped(current_step):
        return None

    return load_plan_content(current_step)  # reads plans/<step>.md
```

Each plan file contains substep-by-substep instructions:

```markdown
# Step 3 — Compliance Check

## 3.1 Verify Licensing
Call `check_license(id=...)`. If expired, flag with remedy="Escalate".

## 3.2 Run Audit
Call `run_audit()`. Compare result against thresholds in state.

## 3.3 Generate Report
Call `save_step_report(step="STEP_03", ...)`.
```

**Why transient injection instead of persistent messages:**
- Plans are large (1–3KB each). Keeping all 15 plans in the message history would waste tokens.
- The agent only needs the current step's plan. Older plans are irrelevant noise.
- A rolling summarization middleware compresses completed step context, so old plans are captured in the summary.

---

### 4. Remote Subagents via Task Tool

Subagents are separate LangGraph Cloud deployments — each with their own graph, tools, and potentially different modalities (e.g., computer use / browser automation). The orchestrator invokes them through a `task` tool.

**The Collect / Invoke / Analyze pattern:**

```
collect_info()  →  task(subagent_type="...", inputs={...})  →  analyze_result()
```

1. **Collect:** A tool gathers the inputs the subagent needs from the orchestrator's state and stages them in `pending_subagent_inputs`.
2. **Invoke:** The `task` tool sends the request to the remote subagent and waits for the result.
3. **Analyze:** A tool interprets the subagent's response and updates the orchestrator's state (flags, reports, etc.).

**Defining a remote subagent:**

```python
my_subagent = create_remote_subagent(
    name="my-subagent",
    url="https://my-subagent-<id>.us.langgraph.app",
    graph_id="my_graph",
    description="Does X given Y inputs...",
    middleware_config={
        "station": {"variables": ["record_id"], "station_id": "my-station"},
        "server": {"server_id": "myProd", "checkpoint": "Home", "server_index": 0}
    },
)
```

**When to use subagents vs. tools:**
- Use a **tool** when the work is a deterministic function call (API request, computation, field write).
- Use a **subagent** when the work requires its own agentic loop (GUI navigation, multi-step browser automation, a separate LLM reasoning chain).

---

### 5. State Schema with Custom Reducers

The state schema extends `AgentState` with domain-specific fields. Internal fields use `OmitFromInput` to hide them from the LangGraph UI input form.

```python
class WorkflowState(AgentState):
    # === Input Fields (shown in UI) ===
    record_id: str
    env: str  # "Test" or "Prod"

    # === Internal Fields (hidden from input) ===
    messages: Annotated[list, truncate_messages]
    record_data: Annotated[NotRequired[dict], OmitFromInput, merge_dicts]
    flags: Annotated[NotRequired[list[dict]], OmitFromInput, dedupe_flags]
    current_step: Annotated[NotRequired[str], OmitFromInput, last_value_reducer]
    step_reports: Annotated[NotRequired[dict], OmitFromInput, merge_dicts]
    # ...
```

**Key reducer patterns:**

| Reducer | Behavior | Use Case |
|---------|----------|----------|
| `merge_dicts` | Deep-merge new dict into existing | Accumulating data from multiple tools without overwrite |
| `dedupe_flags` | Append + deduplicate by (substep, title) | Preventing duplicate warnings across retries |
| `truncate_messages` | Cap tool result content at N chars | Preventing state bloat from large API responses |
| `last_value_reducer` | Last write wins | Scalar fields like `current_step` |
| `lambda old, new: (old or []) + (new or [])` | Simple append | Accumulating lists (notes, image URLs) |

**Why custom reducers matter:** In a long-running workflow with 15+ steps and 50+ tool calls, naive state updates will either overwrite earlier data or accumulate unbounded lists. Reducers give fine-grained control over how each field evolves.

---

### 6. Middleware Stack

The agent uses a layered middleware stack for cross-cutting concerns:

```
Request
  │
  ▼
┌─────────────────────────────┐
│  WorkflowMiddleware         │  Defines the state schema
├─────────────────────────────┤
│  DynamicToolMiddleware      │  Calls tool_resolver before each LLM call
├─────────────────────────────┤
│  DynamicPlanMiddleware      │  Calls plan_resolver before each LLM call
├─────────────────────────────┤
│  SummarizationMiddleware    │  Compresses completed step context
└─────────────────────────────┘
  │
  ▼
LLM Call
```

| Middleware | Purpose |
|-----------|---------|
| **WorkflowMiddleware** | Attaches the custom state schema (`WorkflowState`) to the agent |
| **DynamicToolMiddleware** | Invokes `tool_resolver(state)` to filter tools before each LLM call |
| **DynamicPlanMiddleware** | Invokes `plan_resolver(state)` to inject step instructions transiently |
| **SummarizationMiddleware** | Creates a rolling summary of completed steps, replacing old messages with a compressed version to stay within token limits |

The summarization middleware is critical for long workflows. Without it, the conversation grows linearly with each step, eventually exceeding context limits. The summary preserves key findings (flags, field values, decisions) while discarding verbose tool outputs.

---

### 7. Config-Driven Workflow Generation

The workflow structure is defined in a single JSON config file — not scattered across code:

```json
{
  "phases": {
    "VERIFICATION": { "order": 1, "steps": [0, 1, 2] },
    "PREPARATION": { "order": 2, "steps": [3] },
    "EXECUTION":   { "order": 3, "steps": [5, 6, 7] }
  },
  "steps": {
    "STEP_00": {
      "name": "Data Gathering",
      "plan_file": "data_gathering.md",
      "substeps": [
        { "id": "0.1", "name": "Find record", "tools": ["find_record"] },
        { "id": "0.2", "name": "Extract fields", "tools": ["extract_fields"] }
      ]
    }
  },
  "middleware": {
    "summarization": { "enabled": true },
    "tool_filtering": { "enabled": true },
    "plan_injection": { "enabled": true },
    "max_tool_result_size": 30000
  }
}
```

A generation script reads this config and produces:

```
workflow_config.json  (edit here)
         │
         ▼
   generate script
         │
         ├──► registry.py   (step config, tool mappings)
         ├──► steps.csv      (human-readable export)
         └──► planner.md     (planning prompt)
```

**Benefits:**
- Non-engineers can review and modify the workflow structure in JSON
- The registry code is never edited manually — it is a generated artifact
- Adding a step is: add JSON entry, write plan markdown, write tool functions, regenerate

---

### 8. Development Mode

Dev mode allows targeted testing without running the full workflow:

| Mode | Behavior |
|------|----------|
| **Cutoff** | Steps after a cutoff point return placeholder responses instead of executing |
| **Skip Steps** | Entire steps are skipped — no tools available, agent moves to next step |
| **Skip Substeps** | Individual substeps auto-marked as "skipped" |
| **Run-Only Substeps** | Only specified substeps execute; everything else is skipped |

**Priority chain:** Input state > config file > environment variables > disabled.

This is essential for iterative development. When building Step 7, you don't want to wait for Steps 0–6 to execute every time. Set a cutoff or skip earlier steps, and only the step under development runs with real tools.

```json
{
  "dev_mode": {
    "enabled": true,
    "skip_steps": ["STEP_01", "STEP_02"],
    "run_only_substeps": ["0.1", "0.2", "7.1", "7.2"]
  }
}
```

---

## Project Layout

```
project/
├── agent.py                  # Main agent definition, state schema, resolvers
├── registry.py               # Step config, tool mappings, step order (generated)
├── step_loader.py            # Dynamic plan/tool loading utilities
├── langgraph.json            # LangGraph Cloud entry point
├── requirements.txt          # Python dependencies
├── env.example               # Environment variable template
│
├── config/
│   ├── workflow_config.json  # Workflow definition (source of truth)
│   ├── generate.py           # Generates registry.py from config
│   └── *.json                # Domain-specific config files
│
├── plans/                    # Step plan markdown files (one per step)
│   ├── system_prompt.template.md
│   ├── step_00_plan.md
│   ├── step_01_plan.md
│   └── ...
│
├── tools/                    # Tool implementations
│   ├── __init__.py           # Tool exports and registry loader
│   ├── <domain>_tools.py     # Domain-specific tool modules
│   ├── general.py            # General tools (write_todo, add_flag, etc.)
│   ├── shared/               # Shared utilities (API clients, helpers)
│   └── schema/               # Data extraction schemas
│
├── tests/                    # Test suites
├── docs/                     # Documentation
└── logs/                     # Runtime logs (gitignored)
```

**Key files and their roles:**

| File | Role |
|------|------|
| `agent.py` | Creates the agent via `create_deep_agent()`. Defines state schema, tool resolver, plan resolver, remote subagents. This is the entry point referenced by `langgraph.json`. |
| `registry.py` | Generated from `workflow_config.json`. Contains `STEP_CONFIG`, `STEP_ORDER`, tool lookup functions, and dev mode logic. Never edit manually. |
| `step_loader.py` | Utility functions to load plan files, resolve step headers, and map step IDs to tool functions. |
| `config/workflow_config.json` | The single source of truth. Defines phases, steps, substeps, tool mappings, middleware settings, and dev mode defaults. |
| `plans/*.md` | One markdown file per step. Contains substep-by-substep instructions for the LLM. Injected transiently by the plan resolver. |
| `tools/` | Each tool is a LangChain `@tool` function that receives injected state and returns a result + state updates via `Command`. |
| `langgraph.json` | LangGraph Cloud manifest. Points to the agent entry point and environment file. |

---

## Data Flow

```
User Input (record_id, env)
       │
       ▼
  ┌─────────────────────┐
  │  Step 0: Gather Data │──► External APIs → populate state
  └──────────┬──────────┘
             ▼
  ┌─────────────────────┐
  │  Step 1: Validate    │──► Check blockers, validate inputs
  └──────────┬──────────┘
             ▼
  ┌─────────────────────┐
  │  Step 2: Verify      │──► Compare sources, flag discrepancies
  └──────────┬──────────┘
             ▼
  ┌─────────────────────┐
  │  Step N: Subagent    │──► collect_info → task(subagent) → analyze_result
  └──────────┬──────────┘
             ▼
         ... more steps ...
             │
             ▼
  ┌─────────────────────┐
  │  Final: Report       │──► Generate report, capture screenshots
  └─────────────────────┘
```

At each step:
1. **Plan resolver** injects the step's markdown instructions
2. **Tool resolver** scopes the available tools
3. The **LLM** reads the plan, calls tools, and uses `write_todo` to track substep progress
4. `save_step_report` persists the step's findings
5. The **summarization middleware** compresses the completed step's messages
6. The agent advances to the next step in `STEP_ORDER`

---

## Deployment Model

### LangGraph Cloud

The project deploys to LangGraph Cloud with zero custom infrastructure:

```json
{
  "dependencies": ["."],
  "graphs": {
    "my-orchestrator": "./agent.py:agent"
  },
  "env": ".env"
}
```

- Push to GitHub triggers auto-build and deploy
- LangGraph Cloud handles the runtime (Uvicorn, state persistence, thread management)
- Each remote subagent is a separate deployment with its own URL
- LangSmith provides tracing and observability

### Local Development

```bash
cp env.example .env        # configure credentials
pip install -r requirements.txt
langgraph dev              # starts on http://localhost:2024
```

The local server exposes the full LangGraph API (threads, runs, state).

---

## Applying This Architecture

To adapt this architecture for a new domain:

### 1. Define your workflow

Write `config/workflow_config.json` with your phases, steps, and substeps. Each step needs a name and a list of tool names it will use.

### 2. Write plan files

Create one `plans/<step>.md` per step. Each file should contain substep instructions that tell the LLM exactly what to do and in what order.

### 3. Implement tools

Write `@tool` functions in `tools/`. Each tool should:
- Accept `state` via `InjectedState` to read workflow data
- Return a `Command` with `update={}` to write back to state
- Be named to match what you registered in the config

### 4. Define your state schema

Extend `AgentState` with your domain's fields. Choose reducers based on how each field should accumulate data across tool calls.

### 5. Create remote subagents (if needed)

For work requiring a different modality (browser automation, specialized models), deploy separate LangGraph Cloud graphs and register them with `create_remote_subagent()`.

### 6. Wire it together

In `agent.py`:
- Import all tools
- Define the state schema and middleware
- Create the agent with `create_deep_agent()`, passing your tool resolver, plan resolver, and subagents

### 7. Generate and run

```bash
./generate.sh              # regenerate registry from config
langgraph dev              # test locally
git push                   # deploy to LangGraph Cloud
```
