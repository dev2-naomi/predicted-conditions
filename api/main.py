"""FastAPI application — LangGraph Platform-compatible API for AWS Lambda."""

from __future__ import annotations

import os

from api.secrets import load_secrets

load_secrets()

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from mangum import Mangum

from api.platform.assistants import router as assistants_router
from api.platform.runs import router as runs_router
from api.platform.threads import router as threads_router

app = FastAPI(
    title="Predicted Conditions Agent API",
    description="LangGraph Platform-compatible serverless agent API for predicted-conditions",
    version="0.1.0",
)

# Lambda Function URL adds its own CORS headers. Enabling FastAPI CORS as
# well produces duplicate Access-Control-Allow-Origin values, which browsers
# reject.
if not os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
    _cors_origins = os.environ.get("CORS_ALLOW_ORIGINS", "*")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in _cors_origins.split(",")],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

app.include_router(assistants_router)
app.include_router(threads_router)
app.include_router(runs_router)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.middleware("http")
async def api_key_middleware(request: Request, call_next):
    expected_key = os.environ.get("API_KEY", "").strip()
    if request.method == "OPTIONS":
        return await call_next(request)
    if expected_key and request.url.path != "/health":
        provided = request.headers.get("x-api-key", "")
        if provided != expected_key:
            return JSONResponse(status_code=401, content={"detail": "Invalid API key"})
    return await call_next(request)


_mangum_handler = Mangum(app, lifespan="off")


def handler(event, context):
    """Lambda entrypoint.

    Normal Function URL invocations are HTTP-event-shaped and go through
    Mangum as usual. A background-run self-invoke (see
    api/services/runs.py:_dispatch_via_lambda_self_invoke, used since this
    agent is Lambda-only — see the "When to add Fargate?" note there)
    instead sends a plain `{"predicted_conditions_worker": {...}}` payload
    via `lambda:Invoke` with InvocationType="Event" — not an HTTP event at
    all, so Mangum would reject it. We special-case that shape here, before
    Mangum ever sees it.
    """
    from api.services.runs import WORKER_EVENT_KEY, execute_background_run, load_persisted_run

    if isinstance(event, dict) and WORKER_EVENT_KEY in event:
        run_id = event[WORKER_EVENT_KEY]["run_id"]
        thread_id, assistant_id, run_body = load_persisted_run(run_id)
        return execute_background_run(thread_id, run_id, assistant_id, run_body)

    return _mangum_handler(event, context)
