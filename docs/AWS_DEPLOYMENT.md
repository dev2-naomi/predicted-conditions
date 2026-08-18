# AWS Deployment — predicted-conditions

Agent-specific companion to the FintorAI
[`AWS_DEPLOYMENT_PLAYBOOK.md`](https://github.com/FintorAI/monte-carlo-intelligence/blob/main/AWS_DEPLOYMENT_PLAYBOOK.md).
Read that first for the general pattern; this doc covers what's specific to
predicted-conditions.

## Why this migration

predicted-conditions was running on **LangGraph Platform Cloud**
(`https://sbiq-predicted-conditions-....us.langgraph.app`), with LangSmith
(`LANGCHAIN_API_KEY`) doing both tracing *and* hosting/auth. This migration
moves **hosting** to AWS Lambda while **keeping LangSmith tracing** for
observability — same pattern as `monte-carlo-intelligence`, `LG-docsOrch`,
and `LG-discOrch`.

## Architecture: Lambda-only (no Fargate)

predicted-conditions runs typically take **8-11 minutes** (per the README),
comfortably under Lambda's 900s hard cap. Per the playbook's own decision
rule ("add Fargate when runs regularly exceed 15 minutes"), this follows the
**Lambda-only pattern** — same as `monte-carlo-intelligence` — not the
Lambda+Fargate pattern used by longer-running agents like `LG-discOrch` /
`LG-docsOrch`.

```
Client (test_cloud.py, etc.)
        │
        ▼
Lambda Function URL (api/main.py, FastAPI + Mangum)
  - GET  /health
  - POST /assistants/search
  - POST /threads
  - GET  /threads/{id}
  - GET  /threads/{id}/state          ← reads from DynamoDB checkpointer
  - POST /threads/{id}/runs           ← creates run record, self-invokes Lambda async
  - GET  /threads/{id}/runs/{run_id}  ← poll for status
        │
        ▼
DynamoDB (predicted-conditions-checkpoints{-dev})
  - LangGraph checkpoints (langgraph-checkpoint-aws)
  - Thread + run metadata (api/thread_store.py)
```

## What changed in the repo

| File | Change |
|---|---|
| `agent.py` | Added `build_agent(checkpointer=None)` factory. The module-level `agent = build_agent()` (no checkpointer) is unchanged, so `langgraph dev` / `langgraph.json` keep working exactly as before. |
| `api/` | New — FastAPI shim exposing a LangGraph Platform-compatible API, per the playbook's standard repo structure. |
| `infra/` | New — CDK stack (`PredictedConditionsStack`), deploy/smoke-test/secrets-sync scripts. |
| `.github/workflows/aws-deploy.yml` | New — CI/CD: `dev` branch → `aws-dev` env / `PredictedConditionsStack-Dev`, `main` branch → `aws-prod` env / `PredictedConditionsStack`. |
| `env.example` / `.env` | Added AWS/API vars (see below). No existing var was renamed. |

**Nothing in `agent.py`'s actual logic, `tools/`, `step_loader.py`, `data/`,
or `plans/` changed.** The API shim just wraps the existing graph.

## Backward compatibility: existing test/run scripts keep working

`test_cloud.py`, `run_manifest_cloud.py`, `run_coborrower_cloud.py`,
`offline_verify.py`, `measure_variance.py`, `cloud_consistency.py`, and
`test_coborrower.py` all call the exact same LangGraph Platform-shaped
endpoints (`POST /threads`, `POST /threads/{id}/runs`,
`GET /threads/{id}/runs/{id}`, `GET /threads/{id}/state`). Since `api/`
reproduces that same wire format, **none of these scripts needed logic
changes** beyond one line each: the `x-api-key` header now reads
`API_KEY` (falling back to `LANGCHAIN_API_KEY` if unset), instead of
`LANGCHAIN_API_KEY` alone.

That fallback matters because on LangGraph Platform Cloud, the LangSmith key
*was* the platform's auth key — one secret, two jobs. On AWS those are two
separate secrets (`LANGCHAIN_API_KEY` for LangSmith tracing,
`API_KEY` for this Lambda's own auth), so reusing one value for both would
either break tracing or break auth. Just repoint `.env`:

```bash
# Before (LangGraph Platform Cloud):
LANGGRAPH_URL=https://sbiq-predicted-conditions-....us.langgraph.app
LANGCHAIN_API_KEY=<langsmith-key>   # doubled as the platform's auth key

# After (this Lambda), once infra/deploy.sh has run:
LANGGRAPH_URL=<ApiUrl output from infra/deploy.sh>
API_KEY=<your own random key, also pushed to Secrets Manager>
LANGCHAIN_API_KEY=<langsmith-key>   # unchanged, still just for tracing
```

## Deploying

```bash
# One-time per AWS account/region
cd infra && python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cdk bootstrap aws://828351637694/us-east-2   # same shared account as
                                              # monte-carlo-intelligence / LG-docsOrch
cd ..

# Deploy
./infra/deploy.sh dev     # → PredictedConditionsStack-Dev
./infra/deploy.sh prod    # → PredictedConditionsStack

# Verify
./infra/smoke_test_aws.sh dev
```

`infra/deploy.sh` sources `.env`, runs `cdk deploy`, then pushes
`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `LANGCHAIN_API_KEY` / `API_KEY` to
Secrets Manager (never as CloudFormation properties — see
`infra/stacks/predicted_conditions_stack.py`).

### CI/CD

```bash
gh auth login
./infra/sync_github_secrets.sh --dry-run   # preview
./infra/sync_github_secrets.sh             # push secrets + variables, create aws-dev/aws-prod environments

git push origin dev    # → deploys PredictedConditionsStack-Dev
git push origin main   # → deploys PredictedConditionsStack
```

Add required-reviewer protection on the `aws-prod` GitHub environment if you
want a manual approval gate before prod deploys (not configured by
`sync_github_secrets.sh` — needs specific reviewer usernames).

## Secrets

| Key | Where it comes from | Used by |
|---|---|---|
| `ANTHROPIC_API_KEY` | `.env` / GitHub secret | `agent.py` (primary LLM) |
| `OPENAI_API_KEY` | `.env` / GitHub secret | `agent.py` (cross-provider fallback) |
| `LANGCHAIN_API_KEY` | `.env` / GitHub secret | LangSmith tracing (kept post-migration) |
| `API_KEY` | Generated locally (`openssl rand -hex 24`) / GitHub secret | `api/main.py`'s `x-api-key` auth middleware |

All four live in Secrets Manager (`AgentSecretsArn` in the deploy output),
loaded into `os.environ` at Lambda cold start by `api/secrets.py`. Verify
after any deploy:

```bash
aws secretsmanager get-secret-value --secret-id <AgentSecretsArn> \
  --query SecretString --output text \
  | python3 -c "import json,sys; [print(k, 'SET' if v else 'EMPTY') for k,v in json.load(sys.stdin).items()]"
```

## Known gaps (see playbook's "Gaps / Next Steps")

- No CloudWatch alarms on Lambda errors/timeouts or DynamoDB throttling.
- No cost/budget alerts.
- No automated secret rotation.
- No rollback automation beyond re-running `cdk deploy` against a prior commit.

The smoke-test step in `.github/workflows/aws-deploy.yml` **does** fail the
workflow on a bad health check / assistants-search response (closing one gap
called out in the shared playbook, where the reference workflow didn't).
