#!/usr/bin/env bash
# Push local .env values to GitHub Actions secrets/variables so
# .github/workflows/aws-deploy.yml can deploy without any local step.
#
# Usage:
#   ./infra/sync_github_secrets.sh --dry-run   # preview what would be pushed
#   ./infra/sync_github_secrets.sh             # actually push
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

if ! command -v gh >/dev/null 2>&1; then
  echo "gh CLI not found. Install: brew install gh"
  exit 1
fi
if ! gh auth status >/dev/null 2>&1; then
  echo "Not logged in to gh. Run: gh auth login"
  exit 1
fi

REPO="$(gh repo view --json nameWithOwner -q .nameWithOwner)"
echo "Target repo: $REPO"

if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
fi

# Sensitive — pushed as GitHub Actions secrets.
SECRET_NAMES=(
  ANTHROPIC_API_KEY
  OPENAI_API_KEY
  LANGCHAIN_API_KEY
  API_KEY
  AWS_ACCESS_KEY_ID
  AWS_SECRET_ACCESS_KEY
)

# Non-sensitive — pushed as GitHub Actions variables.
VARIABLE_NAMES=(
  AWS_REGION
  ANTHROPIC_MODEL
  ANTHROPIC_FALLBACK_MODEL
  OPENAI_FALLBACK_MODEL
  LANGCHAIN_PROJECT
  CORS_ALLOW_ORIGINS
)

echo ""
echo "Ensuring GitHub environments aws-dev / aws-prod exist..."
if [[ "$DRY_RUN" -eq 0 ]]; then
  gh api "repos/$REPO/environments/aws-dev" -X PUT >/dev/null
  gh api "repos/$REPO/environments/aws-prod" -X PUT >/dev/null
  # Restrict aws-prod deploys to the main branch only.
  gh api "repos/$REPO/environments/aws-prod/deployment-branch-policies" \
    -X POST -f name='main' >/dev/null 2>&1 || true
else
  echo "  (dry-run) would create/verify environments aws-dev, aws-prod"
fi

for name in "${SECRET_NAMES[@]}"; do
  value="${!name:-}"
  if [[ -z "$value" ]]; then
    echo "  skip secret $name (not set in .env)"
    continue
  fi
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "  (dry-run) would set secret: $name"
  else
    echo "$value" | gh secret set "$name" --repo "$REPO"
    echo "  set secret: $name"
  fi
done

for name in "${VARIABLE_NAMES[@]}"; do
  value="${!name:-}"
  if [[ -z "$value" ]]; then
    echo "  skip variable $name (not set in .env)"
    continue
  fi
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "  (dry-run) would set variable: $name = $value"
  else
    gh variable set "$name" --repo "$REPO" --body "$value"
    echo "  set variable: $name"
  fi
done

echo ""
echo "Done."
