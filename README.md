# Predicted Conditions

An agentic underwriting conditions engine that generates predictive underwriting conditions for non-QM mortgage loans. Built on [LangGraph](https://langchain-ai.github.io/langgraph/) with a single ReAct agent loop, it reads loan data (XML + JSON), consults NQMF underwriting guidelines, and produces a prioritized, de-duplicated list of actionable conditions.

## How It Works

The system operates as a **9-step sequential pipeline** orchestrated by a single LLM agent. Each step has its own scoped tools and plan file, so the LLM only sees what's relevant to the current phase.

```
STEP_00  Scenario Builder     ─ Parse XML/JSON inputs, build unified scenario summary
STEP_01  Cross-Cutting        ─ Programmatic overlay conflict checks, structural gating
STEP_02  Income               ─ LLM reasons over income guidelines → conditions
STEP_03  Assets & Reserves    ─ LLM reasons over asset guidelines → conditions
STEP_04  Credit               ─ LLM reasons over credit guidelines → conditions
STEP_05  Property & Appraisal ─ LLM reasons over property guidelines → conditions
STEP_06  Title & Closing      ─ LLM reasons over title guidelines → conditions
STEP_07  Compliance           ─ LLM reasons over compliance guidelines → conditions
STEP_08  Merger & Ranker      ─ Merge, de-duplicate, filter, rank, produce final output
```

### Key Design Decisions

- **Agentic, not programmatic**: Steps 02–07 use the LLM to reason over the guidelines rather than hard-coded `if/else` trees. The LLM reads the relevant guideline sections, examines the scenario, and determines which conditions to generate. This makes the system adaptable to guideline changes without code modifications.

- **Dynamic tool scoping**: Before each LLM invocation, only the tools for the current step are bound. This reduces token cost by 60–75% and prevents the LLM from calling out-of-scope tools.

- **Dynamic plan injection**: Each step has a plan file (`plans/step_XX_*.md`) injected as a transient system message. Plans define the step's role, condition families, deterministic checks, and quality rules.

- **Guideline-first**: All conditions must trace back to a specific section in `data/guidelines.md`. The `load_guideline_sections` tool dynamically retrieves only the relevant guideline sections for the current step.

- **Post-merge quality filters**: The merger applies cross-module de-duplication (normalizing family IDs across modules), removes "not applicable" negative conditions, and filters speculative conditions.

## Inputs

The agent requires three inputs:

| Input | Format | Description |
|-------|--------|-------------|
| `loan_file_xml` | MISMO XML (iLAD 2.0 or FNM 3.0) | Loan application data — borrowers, property, financials |
| `loan_profile_json` | JSON | General loan parameters — program, FICO, LTV, occupancy, etc. |
| `submitted_documents_json` | JSON array | List of `{doc_id, name}` objects identifying which document types were submitted |

Example input structure:

```
data/input/case_scenario/SelectITIN/
├── sample_case.json                    # loan_profile_json
├── Amezcua_Corona_impac - 05152024 3.xml  # loan_file_xml
└── Pertinent Documents/                # submitted_documents_json (derived from these)
    ├── 117.json   (Credit Report)
    ├── 200.json   (Purchase Contract)
    ├── 349.json   (Lease Agreement)
    └── ...
```

## Output

A single JSON object containing:

```json
{
  "scenario_summary": { ... },
  "seen_conflicts": [ ... ],
  "conditions": [
    {
      "condition_id": "COMP-003",
      "condition_family_id": "ENTITY_VESTING_RESTRICTION",
      "category": "compliance",
      "title": "CRITICAL: Verify Vesting Complies with ITIN Program Restrictions",
      "description": "...",
      "required_documents": ["Title Commitment showing vesting"],
      "required_data_elements": ["Intended vesting type"],
      "owner": "underwriter",
      "severity": "HARD-STOP",
      "priority": 1,
      "confidence": 0.98,
      "triggers": ["Program is Select ITIN", "LLC documentation present"],
      "evidence_found": ["Articles of Organization present"],
      "guideline_trace": "ITIN - ELIGIBILITY: Not permitted to vest as LLC...",
      "overlay_trace": null,
      "resolution_criteria": "Clarify purpose of LLC documentation...",
      "dependencies": [],
      "tags": ["compliance", "ITIN", "entity_vesting"]
    }
  ],
  "stats": {
    "total_conditions": 39,
    "hard_stops": 36,
    "by_category": { "compliance": 9, "credit": 4, "income": 7, ... },
    "by_priority": { "1": 13, "2": 21, "3": 5 }
  }
}
```

Conditions are ranked by priority (P1 > P2 > P3), then severity (HARD-STOP > SOFT-STOP), then category.

## Project Structure

```
predicted-conditions/
├── agent.py                  # LangGraph StateGraph, orchestrator node, routing
├── registry.py               # Auto-generated step→tool mappings
├── step_loader.py            # Dynamic tool/plan resolution per step
├── test_pipeline.py          # End-to-end test runner
│
├── plans/                    # Step plan files (injected as system messages)
│   ├── system_prompt.md      # Global system prompt with quality rules
│   ├── step_00_scenario_builder.md
│   ├── step_01_cross_cutting.md
│   ├── step_02_income.md
│   ├── ...
│   └── step_08_merger_ranker.md
│
├── tools/                    # LangGraph tool implementations
│   ├── __init__.py           # Exports ALL_TOOLS
│   ├── scenario_tools.py     # STEP_00: XML/JSON parsing, scenario building
│   ├── crosscutting_tools.py # STEP_01: overlay conflicts, structural checks
│   ├── income_tools.py       # STEP_02: income condition storage
│   ├── assets_tools.py       # STEP_03: asset condition storage
│   ├── credit_tools.py       # STEP_04: credit condition storage
│   ├── property_tools.py     # STEP_05: property condition storage
│   ├── title_tools.py        # STEP_06: title condition storage
│   ├── compliance_tools.py   # STEP_07: compliance condition storage
│   ├── merger_tools.py       # STEP_08: merge, de-dup, rank, final output
│   ├── general.py            # Cross-step tools (todos, flags, reports)
│   ├── guideline_reader.py   # load_guideline_sections tool
│   └── shared/
│       ├── guidelines.py     # GuidelinesDocument parser for guidelines.md
│       └── xml_parser.py     # MISMO XML extraction (iLAD 2.0 / FNM 3.0)
│
├── data/
│   ├── guidelines.md         # NQMF Underwriting Guidelines (source of truth)
│   └── input/                # Sample case data
│
├── config/
│   ├── workflow_config.json  # Step definitions and tool assignments
│   └── generate.py           # Generates registry.py from config
│
├── requirements.txt
├── env.example
└── langgraph.json            # LangGraph deployment descriptor
```

## Setup

### Prerequisites

- Python 3.9+
- An Anthropic API key

### Installation

```bash
# Clone the repository
git clone <repo-url>
cd predicted-conditions

# Create a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
pip install python-dotenv  # for test runner

# Configure environment
cp env.example .env
# Edit .env and add your ANTHROPIC_API_KEY
```

### Running

```bash
# Run the full pipeline with the sample case
python test_pipeline.py
```

This will:
1. Load the SelectITIN sample case (XML + JSON + submitted documents)
2. Execute all 9 steps sequentially via the LLM agent
3. Print each tool call as it executes
4. Save the final output to `test_output.json`

Typical runtime is 5–8 minutes with `claude-opus-4-5`.

## Architecture Details

### State Management

The agent uses a `PredictiveConditionsState` TypedDict with custom reducers:
- `messages` — append-only message history (LangGraph's `add_messages`)
- `scenario_summary` — deep-merged dict across tool calls
- `module_outputs` — deep-merged dict keyed by step number (e.g., `"02"`, `"08_merge"`)
- `flags` — de-duplicated list of underwriter flags
- `current_step` — last-value reducer, auto-advanced by `save_step_report`

### Tool Architecture

Tools that modify state return `Command(update={...})` objects with a `ToolMessage` for LangGraph's message history consistency. State is injected via `Annotated[dict, InjectedState]`, so the LLM never needs to pass state as an argument.

Domain-specific tools (Steps 02–07) are thin wrappers — the LLM does the reasoning over guidelines and passes its generated conditions as a JSON list. The tool stores them in the correct `module_outputs` slot.

### Merger & De-Duplication

The merger (Step 08) applies three phases:
1. **Negative/speculative filter** — removes conditions whose titles indicate "not applicable", "exempt", or "if applicable"
2. **Cross-module de-dup** — normalizes `condition_family_id` values by stripping module prefixes and resolving synonyms (e.g., `PATRIOT_ACT_OFAC_VERIFICATION` and `FRAUD_ALERT_OFAC_VERIFICATION` both map to `OFAC_SCREENING`)
3. **Strictest-wins merge** — when duplicates exist, keeps the stricter severity/priority and unions all required documents, traces, and criteria

### Guidelines Integration

`data/guidelines.md` is the single source of truth for underwriting rules. The `GuidelinesDocument` class parses it into searchable sections by heading. During Steps 02–07, the LLM calls `load_guideline_sections` with relevant section names, receives the guideline text, and reasons over it to determine which conditions apply to the current scenario.

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | Yes | — | Anthropic API key |
| `ANTHROPIC_MODEL` | No | `claude-opus-4-5` | Model to use |
| `LANGCHAIN_TRACING_V2` | No | `false` | Enable LangSmith tracing |
| `LANGCHAIN_API_KEY` | No | — | LangSmith API key |
| `LANGCHAIN_PROJECT` | No | `predicted-conditions` | LangSmith project name |

## License

See [LICENSE](LICENSE) for details.
