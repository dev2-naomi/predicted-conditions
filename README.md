# Predicted Conditions

An agentic underwriting conditions engine that generates predictive underwriting conditions for non-QM mortgage loans. Built on [LangGraph](https://langchain-ai.github.io/langgraph/) with a single ReAct agent loop, it reads loan data (MISMO XML + document manifest + eligibility engine output), consults NQMF underwriting guidelines and program matrices, and produces a prioritized, de-duplicated list of actionable conditions.

## How It Works

The system operates as an **11-step sequential pipeline** orchestrated by a single LLM agent. Each step has its own scoped tools and plan file, so the LLM only sees what's relevant to the current phase.

```
STEP_00   Scenario Builder          ─ Parse XML + manifest + eligibility, build scenario summary
STEP_00b  Document Completeness     ─ Deterministic check: are required documents present?
STEP_01   Cross-Cutting Gatekeeper  ─ Overlay conflict checks, missing core variable detection
STEP_02   Income Conditions         ─ LLM reasons over income guidelines → conditions
STEP_03   Assets & Reserves         ─ LLM reasons over asset guidelines → conditions
STEP_04   Credit                    ─ LLM reasons over credit guidelines → conditions
STEP_05   Property & Appraisal      ─ LLM reasons over property guidelines → conditions
STEP_06   Title & Closing           ─ LLM reasons over title guidelines → conditions
STEP_07   Compliance                ─ LLM reasons over compliance guidelines → conditions
STEP_08   Program Matrix Eligibility─ Hybrid: deterministic grid checks + LLM qualitative rules
STEP_09   Merger & Ranker           ─ Normalize, merge, de-duplicate, rank, produce final output
```

### Key Design Decisions

- **Agentic, not programmatic**: Steps 02-07 use the LLM to reason over guidelines rather than hard-coded `if/else` trees. The LLM reads relevant guideline sections, examines the scenario, and determines which conditions apply. This makes the system adaptable to guideline changes without code modifications.

- **Hybrid where it matters**: STEP_08 (Program Matrix) and STEP_00b (Document Completeness) use deterministic checks for numeric/structural rules (LTV/FICO grids, reserve schedules, required document lists) and only send qualitative rules to the LLM. This improves speed and accuracy for exact comparisons.

- **Dynamic tool scoping**: Before each LLM invocation, only the tools for the current step are bound. This reduces token cost by 60-75% and prevents the LLM from calling out-of-scope tools.

- **Dynamic plan injection**: Each step has a plan file (`plans/step_XX_*.md`) injected as a transient system message. Plans define the step's role, condition families, and quality rules.

- **Message summarization**: Completed steps are compressed into a compact summary before each LLM call, keeping only the current step's messages in full detail. This prevents the context window from growing unboundedly across steps.

- **Condition normalization**: All conditions pass through a normalization layer in the merger that enforces canonical priority (`P0`-`P3`), severity (`HARD-STOP`/`SOFT-STOP`/`INFO`), category names, and field aliases regardless of how the LLM formatted them.

## Inputs

The agent accepts three primary inputs:

| Input | Format | Description |
|-------|--------|-------------|
| MISMO XML | iLAD/DU Export XML | Loan application data — borrowers, property, financials, declarations |
| Manifest JSON | JSON | Classified document inventory from the submission package |
| Eligibility JSON | JSON | Eligibility engine output — authoritative application data + eligible/ineligible programs |

An optional external JSON override (`loan_profile_json`) can inject fields not present in the XML or eligibility output.

### Frontend Integration

#### Deployment

The agent is deployed to **LangGraph Cloud** and accessed via the [LangGraph SDK](https://langchain-ai.github.io/langgraph/cloud/reference/sdk/python_sdk_ref/).

| Property | Value |
|----------|-------|
| Deployment URL | `https://sbiq-predicted-conditions-189178cdda215830aeb0df97e0fab56b.us.langgraph.app` |
| Assistant ID | `predicted-conditions` |
| Auth | `x-api-key` header with LangSmith API key |

#### Request Format

All three inputs are passed as raw JSON strings — no pre-processing needed. The agent auto-generates its own instruction prompt and defaults to `STEP_00`, so callers only need to send data.

```json
{
  "assistant_id": "predicted-conditions",
  "input": {
    "loan_file_xml": "<raw MISMO XML string>",
    "manifest_json": "<raw Tasktile manifest JSON string>",
    "eligibility_json": "<raw eligibility engine output JSON string>"
  }
}
```

| Input Key | Required | Format | Description |
|-----------|----------|--------|-------------|
| `loan_file_xml` | Yes | String (XML) | MISMO iLAD/DU Export XML — the primary loan application data |
| `manifest_json` | Yes | String (JSON) | Tasktile document manifest — classified inventory of submitted documents |
| `eligibility_json` | Yes | String (JSON) | Eligibility engine output — `application_data`, `eligible_programs`, `program_results` |
| `loan_profile_json` | No | String (JSON) | Optional override — injects fields not present in the XML or eligibility output |

| `current_step` | No | String | Defaults to `STEP_00` if omitted. Can be used to resume from a specific step. |

`env` is accepted but currently unused — kept as a future config hook.

#### Calling the API (JavaScript/TypeScript)

```typescript
import { Client } from "@langchain/langgraph-sdk";

const client = new Client({
  apiUrl: "https://sbiq-predicted-conditions-189178cdda215830aeb0df97e0fab56b.us.langgraph.app",
  apiKey: LANGSMITH_API_KEY,
});

// 1. Create a thread
const thread = await client.threads.create();

// 2. Start a streaming run
const stream = client.runs.stream(thread.thread_id, "predicted-conditions", {
  input: {
    loan_file_xml: rawXmlString,
    manifest_json: rawManifestJsonString,
    eligibility_json: rawEligibilityJsonString,
  },
  streamMode: "updates",
});

// 3. Listen for updates (optional — for progress tracking)
for await (const event of stream) {
  if (event.event === "updates") {
    // event.data contains per-node state updates
    // Check for current_step changes to track pipeline progress
  }
}

// 4. Get the final state
const state = await client.threads.getState(thread.thread_id);
const finalOutput = state.values.final_output;
```

#### Calling the API (Python)

```python
from langgraph_sdk import get_client

client = get_client(url=DEPLOYMENT_URL, api_key=LANGSMITH_API_KEY)
thread = await client.threads.create()

async for event in client.runs.stream(
    thread_id=thread["thread_id"],
    assistant_id="predicted-conditions",
    input={
        "loan_file_xml": raw_xml_string,
        "manifest_json": raw_manifest_json_string,
        "eligibility_json": raw_eligibility_json_string,
    },
    stream_mode="updates",
):
    pass  # or handle streaming updates

state = await client.threads.get_state(thread_id=thread["thread_id"])
final_output = state["values"]["final_output"]
```

#### Response: `final_output`

After the run completes, `state.values.final_output` contains:

| Key | Type | Description |
|-----|------|-------------|
| `scenario_summary` | object | Loan profile used for condition generation (see below) |
| `conditions` | array | **Distilled conditions** — only the fields an underwriter needs to act on |
| `conditions_full` | array | **Full conditions** — includes LLM-generated traces, evidence, and resolution criteria |
| `stats` | object | Aggregate counts by category, priority, and severity |
| `seen_conflicts` | array | Any overlay conflicts detected between steps |

#### Response: `conditions` (distilled)

Each entry in the `conditions` array contains only actionable fields:

```json
{
  "category": "Income",
  "severity": "HARD-STOP",
  "priority": "P1",
  "title": "Income Documentation Type Clarification Required",
  "description": "The submitted documents include a bank statement but no W2s, paystubs, or tax returns. Clarification is needed to confirm whether the borrower is qualifying under Full Documentation (W2/wage earner) or Alternative Documentation (Bank Statement income for self-employed).",
  "required_documents": ["Income Type Declaration Letter", "Employment Status Confirmation"],
  "required_data_elements": ["employment_type", "self_employment_status"]
}
```

#### Response: `conditions_full`

Each entry in `conditions_full` extends the distilled view with LLM-generated reasoning:

```json
{
  "condition_id": "INC-001",
  "condition_family_id": "INCOME_DOC_TYPE_CLARIFICATION",
  "category": "Income",
  "severity": "HARD-STOP",
  "priority": "P1",
  "title": "Income Documentation Type Clarification Required",
  "description": "...",
  "required_documents": ["Income Type Declaration Letter"],
  "required_data_elements": ["employment_type", "self_employment_status"],
  "owner": "PROCESSOR",
  "confidence": 0.9,
  "triggers": ["Bank statement present without W2/paystubs"],
  "evidence_found": ["Bank Statement submitted", "No W2 or paystubs in document package"],
  "guideline_trace": "NQMF FULL DOCUMENTATION: Income must be verified with 30 days paystubs...",
  "overlay_trace": null,
  "resolution_criteria": "Borrower or LO confirms income qualification method...",
  "dependencies": [],
  "tags": ["income", "documentation_type"],
  "source_module": "02",
  "guideline_ref": "guidelines.md"
}
```

| Extra Field | Description |
|-------------|-------------|
| `condition_id` | Short ID assigned by the generating step (e.g., `INC-001`, `ASSET-003`) |
| `condition_family_id` | De-duplication key — conditions with the same family ID are merged |
| `source_module` | Which pipeline step generated it (`00b`, `02`-`08`) |
| `owner` | Suggested responsible party (`PROCESSOR`, `UNDERWRITER`, `BORROWER`) |
| `confidence` | LLM's confidence score (0-1) |
| `triggers` | What loan data triggered this condition |
| `evidence_found` | Specific evidence from the loan file |
| `guideline_trace` | Relevant guideline text the LLM cited |
| `resolution_criteria` | How to clear this condition |
| `tags` | Categorization tags |

#### Response: `scenario_summary`

The `scenario_summary` inside `final_output` is a structured view of the loan profile:

```json
{
  "program": "Flex Select",
  "purpose": "Purchase",
  "occupancy": "OO",
  "property": { "property_type": "SFR", "state": "CA", "county": "Kern", "city": "San Diego" },
  "numbers": { "loan_amount": 328800, "LTV": 80, "CLTV": 80, "DTI": 12, "note_rate": 6.874 },
  "credit": { "fico": 760, "fico_source": "loan_profile", "credit_events": ["none"] },
  "borrowers": [{ "name": "Veronica Marie Montes", "self_employed": true, "citizenship": "US Citizen" }],
  "income_profile": { "income_types": ["W2", "bank_statement"], "primary_income_type": "W2" },
  "eligible_programs": ["Flex Select", "Flex Supreme"],
  "ineligible_programs": ["DSCR Multi (5-8 Units)", "Foreign National", "Investor DSCR", "..."]
}
```

#### Response: `stats`

```json
{
  "total_conditions": 48,
  "hard_stops": 18,
  "by_category": {
    "Document Completeness": 7, "Income": 7, "Compliance": 7, "Title": 6,
    "Program Eligibility": 7, "Assets": 5, "Property": 6, "Credit": 3
  },
  "by_priority": { "P1": 15, "P2": 9, "P3": 24 }
}
```

#### Checking Completion

A completed run has `current_step: "STEP_09"` and `final_output` present in state. To verify:

```typescript
const state = await client.threads.getState(thread.thread_id);
const isComplete = state.values.current_step === "STEP_09" && state.values.final_output != null;
```

If the pipeline hasn't finished, `state.values.current_step` indicates which step it's currently on, and `state.values.step_reports` contains results from completed steps.

#### Tracking Progress via Streaming

When using `streamMode: "updates"`, each event contains state updates from the orchestrator node. Look for `current_step` changes to track which pipeline phase is active:

```
STEP_00  → Parsing inputs, building scenario
STEP_00b → Checking document completeness
STEP_01  → Cross-cutting checks
STEP_02  → Income conditions
STEP_03  → Asset conditions
STEP_04  → Credit conditions
STEP_05  → Property conditions
STEP_06  → Title & closing conditions
STEP_07  → Compliance conditions
STEP_08  → Program matrix eligibility
STEP_09  → Merging, ranking, final output
```

Typical runtime is **8-12 minutes** end-to-end.

#### Error Handling

| Scenario | How to detect |
|----------|---------------|
| Run completed successfully | `current_step === "STEP_09"` and `final_output` is present |
| Run still in progress | `final_output` is null; check `current_step` for progress |
| Run errored | Stream emits an `error` event; check LangSmith traces for details |

### What Gets Extracted

**From the XML** (parsed by `tools/shared/xml_parser.py`):
- Borrower names, SSN, DOB, citizenship, self-employment status
- Loan amount, LTV, CLTV, interest rate, term, amortization type
- Property address, type, occupancy, units
- Liabilities, declarations, housing expenses, credit events

**From the manifest** (parsed by `tools/shared/manifest_parser.py`):
- Each document is classified by category (e.g., `credit_report`, `bank_statement`, `appraisal`)
- De-duplicated by `(category_id, group_name)`, keeping the latest indexing task
- Extracted fields from document processing (entity metadata, parsed values)

**From the eligibility output** (parsed by `parse_eligibility_output` in STEP_00):
- `application_data`: authoritative FICO, LTV, CLTV, DTI, reserves, property type, occupancy, income doc type, channel, borrower type, citizenship — these override XML-derived values
- `eligible_programs`: list of programs that passed eligibility (e.g., `["Flex Select"]`)
- `program_results`: per-program pass/fail details with specific rule violations
- Extra fields: `ReservesMonths`, `FirstTimeHomeBuyer`, `DecliningMarket`, `CreditEventSeasoning`, `SSRScore`, `HPMLStatus`, etc.

### How the Agent Uses the Data

1. **STEP_00** parses all three inputs and builds a unified `scenario_summary`. The eligibility engine's `application_data` takes priority over XML-derived values for numeric fields (FICO, LTV, DTI, etc.) since it comes from a validated upstream system.

2. **STEP_00b** checks the submitted documents against a required checklist based on transaction type (purchase vs. refi), occupancy (investment LLC needs articles of organization), and income documentation type (W2 needs paystubs, bank statement needs 12-24 months of statements).

3. **Steps 02-07** each load relevant guideline sections from `data/guidelines.md`, then the LLM cross-references the guidelines against the `scenario_summary` to determine which conditions apply.

4. **STEP_08** checks the loan against program-specific matrices from `data/program_matrices.md`. When `eligible_programs` is provided from the eligibility engine, matrix checks are **scoped only to those programs** — skipping ineligible programs entirely. Deterministic checks handle LTV/FICO grids, DTI caps, reserve requirements, and loan amount limits. The LLM handles qualitative rules like declining market restrictions and credit event seasoning.

## Output

The final output is a JSON object with two views of the conditions:

### Distilled View (`conditions`)

The primary output — only the fields an underwriter needs to act on:

```json
{
  "scenario_summary": {
    "program": "Flex Select",
    "purpose": "Purchase",
    "occupancy": "OO",
    "property": { "property_type": "SFR", "state": "CA", "county": "Kern" },
    "numbers": { "loan_amount": 328800, "LTV": 80, "CLTV": 80, "DTI": 12 },
    "credit": { "fico": 760 },
    "borrowers": [{ "name": "Veronica Marie Montes" }],
    "eligible_programs": ["Flex Select", "Flex Supreme"]
  },
  "conditions": [
    {
      "category": "Document Completeness",
      "severity": "HARD-STOP",
      "priority": "P1",
      "title": "Missing: Credit Report (dated within 90 days)",
      "description": "The submission package is missing 'Credit Report (dated within 90 days)', which is required for all transactions.",
      "required_documents": ["Credit Report (dated within 90 days)"],
      "required_data_elements": []
    },
    {
      "category": "Income",
      "severity": "HARD-STOP",
      "priority": "P1",
      "title": "Income Documentation Type Clarification Required",
      "description": "Clarification is needed to confirm whether the borrower is qualifying under Full Documentation (W2/wage earner) or Alternative Documentation (Bank Statement income for self-employed).",
      "required_documents": ["Income Type Declaration Letter"],
      "required_data_elements": ["employment_type", "self_employment_status"]
    }
  ],
  "conditions_full": [ /* full condition objects with all LLM-generated fields */ ],
  "stats": {
    "total_conditions": 48,
    "hard_stops": 18,
    "by_category": { "Document Completeness": 7, "Income": 7, "Compliance": 7, "Title": 6, "Program Eligibility": 7, "Assets": 5, "Property": 6, "Credit": 3 },
    "by_priority": { "P1": 15, "P2": 9, "P3": 24 }
  }
}
```

### Condition Fields

| Field | Values | Description |
|-------|--------|-------------|
| `category` | `Document Completeness`, `Program Eligibility`, `Compliance`, `Credit`, `Income`, `Assets`, `Property`, `Title` | Which underwriting facet this condition belongs to |
| `severity` | `HARD-STOP`, `SOFT-STOP`, `INFO` | `HARD-STOP` = cannot proceed, `SOFT-STOP` = needs resolution, `INFO` = advisory |
| `priority` | `P0`, `P1`, `P2`, `P3` | `P0` = critical/blocking, `P1` = high, `P2` = medium, `P3` = standard |
| `title` | string | Short actionable title |
| `description` | string | Full explanation with specific details from the loan scenario |
| `required_documents` | string[] | Documents needed to clear this condition |
| `required_data_elements` | string[] | Data fields needed to clear this condition |

### Sorting Order

Conditions are sorted by: priority (P0 first) → severity (HARD-STOP first) → category (Document Completeness/Program Eligibility first, then Compliance, Credit, Income, Assets, Property, Title).

### Sample Runs

**Montes — with eligibility engine (FICO 760, Flex Select + Flex Supreme eligible)**: 48 conditions, 18 hard-stops. Eligibility engine provides authoritative FICO (760), LTV (80%), DTI (12%), and reserves (12 months). STEP_08 scoped to eligible programs only. 102 tool calls, completed in ~9 minutes.

**Melendez Niccum — without eligibility engine (FICO 755 via override)**: 50 conditions, 4 hard-stops. Program inferred as Flex Select. Matrix checks run against inferred program only.

## Project Structure

```
predicted-conditions/
├── agent.py                  # LangGraph StateGraph, orchestrator node, message summarization
├── registry.py               # Auto-generated step→tool mappings from workflow_config.json
├── step_loader.py            # Dynamic tool/plan resolution per step
├── test_pipeline.py          # End-to-end test runner
│
├── plans/                    # Step plan files (injected as system messages)
│   ├── system_prompt.md
│   ├── step_00_scenario_builder.md
│   ├── step_00b_doc_completeness.md
│   ├── step_01_cross_cutting.md
│   ├── step_02_income.md
│   ├── step_03_assets.md
│   ├── step_04_credit.md
│   ├── step_05_property_appraisal.md
│   ├── step_06_title_closing.md
│   ├── step_07_compliance.md
│   ├── step_08_program_eligibility.md
│   └── step_09_merger_ranker.md
│
├── tools/
│   ├── __init__.py               # Exports ALL_TOOLS and per-step tool lists
│   ├── general.py                # Cross-step: write_todo, save_step_report, get_workflow_status
│   ├── scenario_tools.py         # STEP_00: XML/JSON parsing, scenario building
│   ├── doc_completeness_tools.py # STEP_00b: deterministic document checklist
│   ├── crosscutting_tools.py     # STEP_01: overlay conflicts, structural checks
│   ├── income_tools.py           # STEP_02: guideline loading + income condition storage
│   ├── assets_tools.py           # STEP_03: asset condition storage
│   ├── credit_tools.py           # STEP_04: credit condition storage
│   ├── property_tools.py         # STEP_05: property condition storage
│   ├── title_tools.py            # STEP_06: title condition storage
│   ├── compliance_tools.py       # STEP_07: compliance condition storage
│   ├── matrix_eligibility_tools.py # STEP_08: deterministic + LLM matrix checks
│   ├── merger_tools.py           # STEP_09: normalize, merge, de-dup, rank, final output
│   └── shared/
│       ├── guidelines.py         # GuidelinesDocument parser for guidelines.md
│       ├── xml_parser.py         # Dynamic MISMO XML extraction (3-layer architecture)
│       ├── manifest_parser.py    # Document manifest parser with de-duplication
│       └── matrix_parser.py      # Program matrix parser with deterministic grid/reserve extraction
│
├── data/
│   ├── guidelines.md             # NQMF Underwriting Guidelines (source of truth)
│   ├── program_matrices.md       # Program-specific LTV/FICO grids, reserves, eligibility
│   ├── submission_documents.md   # Required document checklists by transaction type
│   └── input/                    # Test case data (XML + manifest JSON)
│
├── config/
│   ├── workflow_config.json      # Step definitions, tool assignments, middleware config
│   └── generate.py               # Generates registry.py from workflow_config.json
│
├── requirements.txt
├── env.example
└── langgraph.json                # LangGraph deployment descriptor
```

## Setup

### Prerequisites

- Python 3.9+
- An Anthropic API key

### Installation

```bash
git clone <repo-url>
cd predicted-conditions

python3 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt

cp env.example .env
# Edit .env and add your ANTHROPIC_API_KEY
```

### Running

```bash
# Run with a case directory (auto-detects XML, manifest, eligibility files)
python test_pipeline.py data/input/3-16-2

# Run with explicit eligibility file path
ELIGIBILITY_PATH="/path/to/eligibility_output.json" \
python test_pipeline.py data/input/3-16-2

# Run with manual overrides (when no eligibility engine output is available)
TEST_PROGRAM="Flex Select" TEST_FICO=755 \
python test_pipeline.py data/input/3-16
```

The test runner auto-detects files in the input directory:
- `*.xml` → `loan_file_xml`
- `*manifest*.json` → `manifest_json`
- `*eligibility*` or `*sample_output*.json` → `eligibility_json`

It then executes all 11 steps sequentially via the LLM agent (~90-110 tool calls), prints each tool call as it executes, and saves the final output to `test_output.json` and full state to `test_final_state.json`.

Typical runtime is 8-12 minutes with `claude-opus-4-5` (~90-110 tool calls).

## Architecture Details

### State Management

The agent uses a `PredictiveConditionsState` TypedDict with custom reducers:

| Field | Reducer | Description |
|-------|---------|-------------|
| `loan_file_xml` | — | Raw MISMO XML string (input) |
| `manifest_json` | — | Raw manifest JSON string (input) |
| `eligibility_json` | — | Raw eligibility engine output JSON (input) |
| `messages` | `add_messages` | Append-only message history |
| `scenario_summary` | `_merge_dicts` | Deep-merged dict across tool calls |
| `module_outputs` | `_merge_dicts` | Per-module condition storage (keyed `"00b"`, `"02"`, `"08"`, `"09_merge"`, etc.) |
| `current_step` | `_last_value` | Auto-advanced by `save_step_report` |
| `step_reports` | `_merge_dicts` | Per-step summaries for the summarization middleware |
| `final_output` | `_last_value` | The assembled output JSON |

### Tool Architecture

Tools that modify state return `Command(update={...})` objects with a `ToolMessage`. State is injected via `Annotated[dict, InjectedState]`, so the LLM never passes state as an argument.

**Condition generation tools** (Steps 02-07) are thin storage wrappers. The LLM does the reasoning over guidelines and passes its conditions as a JSON list. Each tool:
1. Enforces the canonical `category` for its domain (e.g., `generate_credit_conditions` always sets `category = "Credit"`)
2. Adds domain tags
3. Stores conditions in the correct `module_outputs` slot

### Condition Normalization (STEP_09)

Before merging, all conditions pass through `_normalize_condition` which:

1. **Maps field aliases**: LLM-variant field names are coerced to canonical names (`condition_name` → `title`, `condition_text` → `description`, `detail` → `description`)
2. **Normalizes priority**: `HIGH`/`high`/`CRITICAL`/`1`/`2` → `P0`-`P3`
3. **Normalizes severity**: `HARD_STOP`/`HARDSTOP`/`WARNING` → `HARD-STOP`/`SOFT-STOP`/`INFO`
4. **Normalizes category**: `property_appraisal`/`Property/Appraisal`/`title_closing` → canonical names
5. **Fills missing titles**: Falls back to truncated description if title is missing

### Merger & De-Duplication (STEP_09)

The merger applies four phases:

1. **Normalization** — canonical priority, severity, category, and field names
2. **Negative/speculative filter** — removes conditions whose titles indicate "not applicable", "exempt", or "if applicable"
3. **Cross-module de-dup** — normalizes `condition_family_id` values by stripping module prefixes and resolving synonyms (e.g., `PATRIOT_ACT_OFAC_VERIFICATION` and `FRAUD_ALERT_OFAC_VERIFICATION` both map to `OFAC_SCREENING`)
4. **Strictest-wins merge** — when duplicates exist, keeps the stricter severity/priority and unions all required documents, traces, and criteria

### Message Summarization

To prevent the context window from growing across 11 steps, completed steps are compressed into a compact summary before each LLM invocation. Only the current step's messages are kept in full detail. This is implemented in `agent.py`'s `_summarize_completed_steps` function and reduces cumulative context by ~80%.

### Program Matrix (STEP_08)

The hybrid approach splits program eligibility into:

- **Deterministic** (`check_matrix_eligibility`): Parses LTV/FICO grids, reserve schedules, DTI caps, loan amount ranges, and borrower eligibility directly from `program_matrices.md`. Produces instant, exact conditions. When `eligible_programs` is available from the eligibility engine, checks run only for those programs.
- **Qualitative** (`load_program_matrix` + `generate_matrix_conditions`): Sends only the textual/interpretive rules (declining markets, credit event seasoning, product type restrictions) to the LLM. Input is trimmed ~76% compared to the full matrix text. Also scoped to eligible programs when available.

### Guidelines Integration

`data/guidelines.md` is the single source of truth for underwriting rules. The `GuidelinesDocument` class parses it into searchable sections by heading. During Steps 02-07, the LLM calls `load_guideline_sections` with relevant section names, receives the guideline text, and reasons over it to determine which conditions apply to the current scenario.

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | Yes | — | Anthropic API key |
| `ANTHROPIC_MODEL` | No | `claude-opus-4-5` | Model to use |
| `MANIFEST_PATH` | No | — | Path to document manifest JSON |
| `ELIGIBILITY_PATH` | No | — | Path to eligibility engine output JSON |
| `TEST_PROGRAM` | No | — | Override loan program for testing (e.g., `Flex Select`) |
| `TEST_FICO` | No | — | Override FICO score for testing (e.g., `755`) |
| `LANGCHAIN_TRACING_V2` | No | `false` | Enable LangSmith tracing |
| `LANGCHAIN_API_KEY` | No | — | LangSmith API key |
| `LANGCHAIN_PROJECT` | No | `predicted-conditions` | LangSmith project name |

## License

See [LICENSE](LICENSE) for details.
