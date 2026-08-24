# Calling the predicted-conditions API

How to call the deployed AWS API — both `dev` and `prod` — including auth.
For *why*/*how it was migrated*, see [`AWS_DEPLOYMENT.md`](./AWS_DEPLOYMENT.md).
This doc is just the calling reference.

## Base URLs

| Env | Base URL | CloudFormation stack |
|---|---|---|
| **dev** | `https://2mf66qctrm3odfx3xxh4uzi4cy0zbbbn.lambda-url.us-east-2.on.aws` | `PredictedConditionsStack-Dev` |
| **prod** | `https://or2rexaz5ukx2ydmb3povh2d5a0rhgzn.lambda-url.us-east-2.on.aws` | `PredictedConditionsStack` |

These are AWS Lambda Function URLs — they don't change between deploys of the
same stack. If you ever need to re-confirm them (e.g. after a stack
teardown/recreate), the authoritative source is CloudFormation, not `.env`
(which only reflects whichever env was deployed most recently):

```bash
aws cloudformation describe-stacks --region us-east-2 \
  --stack-name PredictedConditionsStack-Dev \
  --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" --output text

aws cloudformation describe-stacks --region us-east-2 \
  --stack-name PredictedConditionsStack \
  --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" --output text
```

## Authentication

Every request (except `GET /health`) requires an `x-api-key` header matching
the `API_KEY` secret for that stage:

```
x-api-key: <API_KEY>
```

- `dev` and `prod` have **separate, dedicated** `API_KEY` values — each
  stack has its own `AgentSecrets` entry in Secrets Manager, and they
  are intentionally different so a dev key can't be used against prod
  (or vice versa; you'll get a `401`).
- Locally: dev's value lives in `.env` as `API_KEY`, prod's as
  `API_KEY_PROD` (both gitignored, never committed). `infra/deploy.sh`
  reads `API_KEY_PROD` (not `API_KEY`) when deploying the prod stage —
  see `env.example` for details.
- Authoritatively, each stage's key lives in Secrets Manager, in the
  `AgentSecretsArn` output from that stage's stack (`API_KEY` field):

```bash
# dev
aws secretsmanager get-secret-value --region us-east-2 \
  --secret-id predicted-conditions-agent-secrets-dev \
  --query SecretString --output text | python3 -m json.tool

# prod — get the ARN first (name has a CDK-generated suffix)
aws cloudformation describe-stacks --region us-east-2 \
  --stack-name PredictedConditionsStack \
  --query "Stacks[0].Outputs[?OutputKey=='AgentSecretsArn'].OutputValue" --output text
aws secretsmanager get-secret-value --region us-east-2 --secret-id <arn-from-above> \
  --query SecretString --output text | python3 -m json.tool
```

Missing/wrong key → `401 {"detail": "Invalid API key"}`. This is enforced by
the middleware in `api/main.py`.

> Don't confuse `API_KEY` (this API's own auth) with `LANGCHAIN_API_KEY`
> (LangSmith tracing). They're separate secrets — see
> [`AWS_DEPLOYMENT.md`](./AWS_DEPLOYMENT.md#backward-compatibility-existing-testrun-scripts-keep-working).

## Assistant ID

There's exactly one graph registered (`api/registry.py`):

```
assistant_id = "predicted-conditions"
```

## Endpoints

Runs take **8-11 minutes**, so this API mirrors LangGraph Platform's async
run model: `POST /threads/{id}/runs` returns immediately with a `pending`
run, and you poll for the result. There's no `/runs/wait`-style long-poll
built for the *threaded* form on purpose — see below.

| Method | Path | Purpose |
|---|---|---|
| `GET`  | `/health` | Liveness check. No auth required. |
| `POST` | `/assistants/search` | List available assistants. |
| `POST` | `/threads` | Create a new thread. |
| `GET`  | `/threads/{thread_id}` | Fetch thread metadata. |
| `GET`  | `/threads/{thread_id}/state` | Fetch current graph state (incl. `final_output` once done). |
| `POST` | `/threads/{thread_id}/runs` | Start a run on a thread — returns immediately, status `pending`. |
| `GET`  | `/threads/{thread_id}/runs/{run_id}` | Poll a run's status. |
| `POST` | `/threads/{thread_id}/runs/wait` | Start a run on a thread and block until it finishes (holds the HTTP connection for the full 8-11 min — see note below). |
| `POST` | `/threads/{thread_id}/runs/stream` | Same, but streamed as SSE. |
| `POST` | `/runs/wait` | Threadless one-shot run (creates an ephemeral thread internally), blocks until done. |
| `POST` | `/runs/stream` | Threadless one-shot run, streamed as SSE. |

> **Prefer `POST /threads/{id}/runs` + polling** for real usage. The
> `/wait` and `/stream` variants hold one HTTP connection open for the
> entire 8-11 minute run, which most load balancers/HTTP clients/browsers
> will time out on well before that. `/wait`/`/stream` exist mainly for
> Platform-wire-format compatibility and short local tests.

## Request / response shapes

### `POST /assistants/search`

```bash
curl -sS -X POST "$BASE_URL/assistants/search" \
  -H "x-api-key: $API_KEY" -H "Content-Type: application/json" \
  -d '{}'
```

```json
[
  {
    "assistant_id": "predicted-conditions",
    "graph_id": "predicted-conditions",
    "name": "Predicted Conditions",
    "description": "Predictive underwriting conditions engine for non-QM mortgage loans."
  }
]
```

### `POST /threads`

```bash
curl -sS -X POST "$BASE_URL/threads" \
  -H "x-api-key: $API_KEY" -H "Content-Type: application/json" \
  -d '{}'
```

```json
{ "thread_id": "019fxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx", "created_at": "...", "metadata": {} }
```

### `POST /threads/{thread_id}/runs`

Body fields, per `api/platform/models.py::RunCreate`:

| Field | Type | Notes |
|---|---|---|
| `assistant_id` | `str` | Required. Always `"predicted-conditions"`. |
| `input` | `dict` | The three raw inputs (see below). |
| `config` | `dict \| null` | Optional, e.g. `{"recursion_limit": 250}`. |
| `stream_mode` | `str \| list[str] \| null` | Unused for the non-streaming create endpoint. |

`input` is the same three-field payload every run script in this repo sends
(see `test_cloud.py`):

```json
{
  "assistant_id": "predicted-conditions",
  "input": {
    "loan_file_xml": "<raw XML string>",
    "manifest_json": "<raw JSON string>",
    "eligibility_json": "<raw JSON string>"
  },
  "config": { "recursion_limit": 250 }
}
```

```bash
curl -sS -X POST "$BASE_URL/threads/$THREAD_ID/runs" \
  -H "x-api-key: $API_KEY" -H "Content-Type: application/json" \
  -d @run_body.json
```

Response — `202`-style immediate ack:

```json
{ "run_id": "...", "thread_id": "...", "status": "pending", "assistant_id": "predicted-conditions" }
```

### `GET /threads/{thread_id}/runs/{run_id}` — poll

```bash
curl -sS "$BASE_URL/threads/$THREAD_ID/runs/$RUN_ID" -H "x-api-key: $API_KEY"
```

`status` is one of `pending`, `success`, `error`, `timeout`, `interrupted`.
Poll every ~15s; runs typically finish in 8-11 minutes.

### `GET /threads/{thread_id}/state` — fetch the result

Once `status == "success"`:

```bash
curl -sS "$BASE_URL/threads/$THREAD_ID/state" -H "x-api-key: $API_KEY"
```

```json
{ "values": { "final_output": { "document_requests": [ /* ... */ ], "stats": { /* ... */ } }, "...": "..." } }
```

`values.final_output.document_requests` is the payload you actually want.

## Full round-trip example (Python)

This is exactly what `test_cloud.py` does — reuse it directly for real
testing (`python test_cloud.py compiled_inputs/<case>`), or as a reference:

```python
import os, time, requests

BASE_URL = "https://2mf66qctrm3odfx3xxh4uzi4cy0zbbbn.lambda-url.us-east-2.on.aws"  # dev
API_KEY = os.environ["API_KEY"]
HEADERS = {"x-api-key": API_KEY, "Content-Type": "application/json"}

thread_id = requests.post(f"{BASE_URL}/threads", headers=HEADERS, json={}).json()["thread_id"]

run = requests.post(
    f"{BASE_URL}/threads/{thread_id}/runs",
    headers=HEADERS,
    json={
        "assistant_id": "predicted-conditions",
        "input": {
            "loan_file_xml": open("loan.xml").read(),
            "manifest_json": open("manifest.json").read(),
            "eligibility_json": open("eligibility.json").read(),
        },
        "config": {"recursion_limit": 250},
    },
).json()
run_id = run["run_id"]

while True:
    status = requests.get(f"{BASE_URL}/threads/{thread_id}/runs/{run_id}", headers=HEADERS).json()["status"]
    print(status)
    if status in {"success", "error", "timeout", "interrupted"}:
        break
    time.sleep(15)

state = requests.get(f"{BASE_URL}/threads/{thread_id}/state", headers=HEADERS).json()
document_requests = state["values"]["final_output"]["document_requests"]
```

To point this at **prod** instead, swap `BASE_URL` for the prod URL above
*and* use prod's dedicated key (`API_KEY_PROD` locally, or fetch it from
prod's Secrets Manager entry — see [Authentication](#authentication)).
Mixing a dev key with the prod URL (or vice versa) gets you a `401`.

## Using the existing repo scripts (recommended)

Rather than hand-rolling requests, set `.env` and reuse what's already here:

```bash
# .env
LANGGRAPH_URL=https://2mf66qctrm3odfx3xxh4uzi4cy0zbbbn.lambda-url.us-east-2.on.aws   # dev
# LANGGRAPH_URL=https://or2rexaz5ukx2ydmb3povh2d5a0rhgzn.lambda-url.us-east-2.on.aws # prod
API_KEY=<the matching stage's key>
```

```bash
python test_cloud.py compiled_inputs/<case>          # single-case smoke test
python run_manifest_cloud.py ...                      # manifest-driven batch run
python measure_variance.py ...                        # repeat-run stochasticity check
```

All of these already prefer `API_KEY` over `LANGCHAIN_API_KEY` for auth (see
`AWS_DEPLOYMENT.md`), so no code changes are needed — just point
`LANGGRAPH_URL`/`API_KEY` at the stage you want.

## Document type catalog

`document_requests[].document_type` in the run output is always one of a
fixed, enforced set — **172 NQM-relevant types**, filtered from the full
Encompass catalog in `data/doctype_masterlist.json` and enforced by
`tools/shared/normalize.py::normalize_all()`.

The filtered list (grouped by category: Assets, Compliance, Credit,
Cross-Cutting, Income, Income/Assets, Property, Title) is available as JSON
at [`data/nqm_document_types.json`](../data/nqm_document_types.json) —
each entry has `doctype_id`, `document_type`, `category`, `nqm_relevant`.

A handful of additional types can appear even though they're outside this
172-type set: `Government-Issued Photo ID` (an umbrella type standing in for
Drivers License / Passport / Non-Driver ID / Permanent Resident Card /
Social Security ID / Travel VISA), plus `Loan Application (1003)`,
`CPA Prepared P&L Letter`, `Asset Depletion Worksheet`, and generic `1099` —
all injected by deterministic rules in `tools/doc_rules.py`. Everything else
an LLM step might try to emit gets silently dropped.

## Error responses

| Status | Meaning |
|---|---|
| `401` | Missing/wrong `x-api-key`. |
| `404` | Unknown `assistant_id`, thread, or run. |
| `422` | Missing required field (e.g. no `assistant_id`), or `thread_id` incorrectly set inside `config.configurable` on a threadless run. |
| `500` | Unhandled error inside the graph run — check CloudWatch logs for the relevant Lambda (`predicted-conditions-agent-dev` / prod function name from the stack's `LambdaFunctionName` output). |
