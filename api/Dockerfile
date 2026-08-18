# Native Lambda Python runtime (NOT the Lambda Web Adapter pattern) — this
# agent is Lambda-only and relies on a Lambda self-invoke to dispatch
# background runs (see api/services/runs.py:_dispatch_via_lambda_self_invoke).
# The Web Adapter intercepts every invocation and converts it into an HTTP
# request against a local server, so a raw (non-HTTP-shaped) self-invoke
# payload never reaches api/main.py:handler() at all — it gets silently
# translated into a synthetic "POST /events" request instead. Using this
# base image instead means the Lambda Runtime Interface Client calls
# api.main.handler directly for *every* invocation type, HTTP or not.
FROM public.ecr.aws/lambda/python:3.12

WORKDIR ${LAMBDA_TASK_ROOT}

# Install dependencies first for layer caching
COPY api/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code. agent.py's imports (step_loader, registry, tools)
# resolve relative to LAMBDA_TASK_ROOT, matching how `langgraph dev` runs it
# from the repo root — no import-path changes needed in agent-specific code.
COPY agent.py ./
COPY registry.py ./
COPY step_loader.py ./
COPY tools/ ./tools/
COPY data/ ./data/
COPY plans/ ./plans/
COPY api/ ./api/

CMD ["api.main.handler"]
