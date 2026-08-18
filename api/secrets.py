"""Load secrets from AWS Secrets Manager on Lambda cold start.

Local dev / ``langgraph dev`` never sets ``AGENT_SECRETS_ARN``, so this is a
no-op there — secrets keep coming from ``.env`` via ``python-dotenv`` as
before. On Lambda, ``AGENT_SECRETS_ARN`` points at the Secrets Manager entry
created by the CDK stack (infra/stacks/predicted_conditions_stack.py) and
populated out-of-band by scripts/deploy.sh / .github/workflows/deploy.yml.
"""

from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)
_loaded = False

# Keys hydrated from Secrets Manager into os.environ at cold start, so
# agent.py / step_loader.py / tools/ stay unchanged whether running under
# `langgraph dev`, LangGraph Platform, or this Lambda. Compiled from the env
# vars actually read by agent.py (see agent.py's os.environ.get calls) plus
# the API layer's own auth key. Keep in sync with
# infra/stacks/predicted_conditions_stack.py:SECRET_KEYS.
SECRET_KEYS = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "LANGCHAIN_API_KEY",
    "API_KEY",
)


def load_secrets() -> None:
    global _loaded
    if _loaded:
        return

    secret_arn = os.environ.get("AGENT_SECRETS_ARN", "").strip()
    if not secret_arn:
        _loaded = True
        return

    try:
        import boto3

        client = boto3.client("secretsmanager")
        resp = client.get_secret_value(SecretId=secret_arn)
        payload = json.loads(resp["SecretString"])
        for key in SECRET_KEYS:
            value = payload.get(key)
            if value and not os.environ.get(key):
                os.environ[key] = str(value)
        logger.info("Loaded agent secrets from Secrets Manager")
    except Exception:
        logger.exception("Failed to load secrets from %s", secret_arn)
    finally:
        _loaded = True
