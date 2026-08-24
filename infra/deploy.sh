#!/usr/bin/env bash
# Deploy the predicted-conditions agent stack to AWS.
# Usage: ./infra/deploy.sh dev   (or: ./infra/deploy.sh prod)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STAGE="${1:-dev}"
if [[ "$STAGE" != "dev" && "$STAGE" != "prod" ]]; then
  echo "Usage: $0 [dev|prod]"
  exit 1
fi

if ! aws sts get-caller-identity >/dev/null 2>&1; then
  echo "AWS credentials not configured. Run: aws configure (or export AWS_PROFILE)"
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "Docker is not running. Start Docker before deploying."
  exit 1
fi

cd "$ROOT/infra"
python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q -r requirements.txt

# Load local .env so CDK can inject values into Lambda + Secrets Manager
if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
fi

export PREDICTED_CONDITIONS_STAGE="$STAGE"
if [[ "$STAGE" == "prod" ]]; then
  STACK_ID="PredictedConditionsStack"
else
  STACK_ID="PredictedConditionsStack-Dev"
fi

echo "Bootstrapping CDK (safe to re-run)..."
cdk bootstrap

echo "Deploying stack $STACK_ID (stage=$STAGE)..."
OUTPUTS_TMP="$(mktemp)"
cdk deploy "$STACK_ID" --require-approval never --outputs-file "$OUTPUTS_TMP"

python3 - <<'PY' "$OUTPUTS_TMP" "$ROOT/output.json"
import json
import sys

src, dest = sys.argv[1], sys.argv[2]
with open(src, encoding="utf-8") as f:
    stacks = json.load(f)

flat: dict[str, str] = {}
for stack_name, outputs in stacks.items():
    for key, value in outputs.items():
        flat[f"{stack_name}.{key}"] = value

with open(dest, "w", encoding="utf-8") as f:
    json.dump(flat, f, indent=2)
    f.write("\n")
PY
rm -f "$OUTPUTS_TMP"

echo ""
echo "Wrote CDK outputs to $ROOT/output.json"
cat "$ROOT/output.json"
echo ""

# Push real secret values directly to Secrets Manager via the API — this is
# NOT a CloudFormation resource property, so it never appears in a stack
# template. CDK only seeds the secret with an empty placeholder (see
# infra/stacks/predicted_conditions_stack.py); this step is what actually
# makes it usable. Keep this key list in sync with
# infra/stacks/predicted_conditions_stack.py:SECRET_KEYS and
# api/secrets.py:SECRET_KEYS.
SECRET_ARN="$(python3 -c "
import json
with open('$ROOT/output.json', encoding='utf-8') as f:
    print(json.load(f).get('$STACK_ID.AgentSecretsArn', ''))
" 2>/dev/null || true)"

# dev and prod are separate secrets with separate API_KEY values (see
# docs/AWS_DEPLOYMENT.md). .env only has one API_KEY (used for dev); to keep
# prod's dedicated key from being overwritten back to dev's value on every
# `deploy.sh prod`, prefer API_KEY_PROD when it's set and we're deploying prod.
if [[ "$STAGE" == "prod" && -n "${API_KEY_PROD:-}" ]]; then
  export API_KEY="$API_KEY_PROD"
fi

if [[ -n "$SECRET_ARN" && -n "${ANTHROPIC_API_KEY:-}" ]]; then
  echo "Pushing secret values to Secrets Manager ($SECRET_ARN)..."
  SECRET_PAYLOAD_FILE="$(mktemp)"
  python3 - <<PY > "$SECRET_PAYLOAD_FILE"
import json
import os

print(json.dumps({
    "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", ""),
    "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY", ""),
    "LANGCHAIN_API_KEY": os.environ.get("LANGCHAIN_API_KEY", ""),
    "API_KEY": os.environ.get("API_KEY", ""),
}))
PY
  aws secretsmanager put-secret-value \
    --region us-east-2 \
    --secret-id "$SECRET_ARN" \
    --secret-string "file://$SECRET_PAYLOAD_FILE" >/dev/null
  rm -f "$SECRET_PAYLOAD_FILE"
  echo "Secret values updated."
  echo "Note: warm Lambda instances keep whatever they cached at cold start —"
  echo "new values take effect on the next cold start, not immediately."
else
  echo "Skipping Secrets Manager update (no ANTHROPIC_API_KEY in environment/.env)."
  echo "Set manually: aws secretsmanager put-secret-value --secret-id <AgentSecretsArn> --secret-string '{...}'"
fi

echo ""
echo "After deploy ($STACK_ID, stage=$STAGE):"
echo "  1. Run ./infra/smoke_test_aws.sh $STAGE to verify"
echo "  2. Point clients at the ApiUrl output above (same request/response"
echo "     shapes as LangGraph Platform — see docs/AWS_DEPLOYMENT.md)"
