#!/usr/bin/env bash
# Post-deploy smoke tests.
# Usage: ./infra/smoke_test_aws.sh dev   (reads output.json for the URL)
#    or: API_URL=https://xxx.lambda-url.us-east-2.on.aws ./infra/smoke_test_aws.sh
set -eo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STAGE="${1:-dev}"
STACK_ID="PredictedConditionsStack"
[[ "$STAGE" == "dev" ]] && STACK_ID="PredictedConditionsStack-Dev"

if [[ -z "${API_URL:-}" && -f "$ROOT/output.json" ]]; then
  API_URL="$(python3 -c "
import json
with open('$ROOT/output.json', encoding='utf-8') as f:
    print(json.load(f).get('$STACK_ID.ApiUrl', ''))
")"
fi
API_URL="${API_URL:?Set API_URL, or run infra/deploy.sh first so output.json exists}"
API_URL="${API_URL%/}"

if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
fi

_curl() {
  if [[ -n "${API_KEY:-}" ]]; then
    curl -sf -H "x-api-key: $API_KEY" "$@"
  else
    curl -sf "$@"
  fi
}

echo "==> GET /health"
_curl "$API_URL/health" | grep -q '"status":"ok"' || _curl "$API_URL/health" | grep -q '"status": "ok"'

echo "==> POST /assistants/search"
_curl -X POST "$API_URL/assistants/search" \
  -H "Content-Type: application/json" -d '{}' | grep -q predicted-conditions

echo "==> POST /threads"
THREAD_RESP="$(_curl -X POST "$API_URL/threads" -H "Content-Type: application/json" -d '{}')"
echo "$THREAD_RESP" | grep -q thread_id

THREAD_ID="$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['thread_id'])" "$THREAD_RESP")"

echo "==> GET /threads/{id}"
_curl "$API_URL/threads/$THREAD_ID" | grep -q "$THREAD_ID"

echo ""
echo "Basic smoke tests passed."
echo "NOT run here (needs a real loan XML/manifest/eligibility payload, ~8-11 min):"
echo "  POST /threads/{id}/runs (background run) + poll GET /threads/{id}/runs/{run_id}"
echo "  until status is success/error, or GET /threads/{id}/state for progress."
echo "  See test_cloud.py for a full example against this same API shape."
