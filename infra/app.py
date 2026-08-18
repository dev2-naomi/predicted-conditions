#!/usr/bin/env python3
import os

import aws_cdk as cdk

from stacks.predicted_conditions_stack import PredictedConditionsStack

# Pin account/region explicitly instead of the CFN pseudo-params or
# CDK_DEFAULT_ACCOUNT/REGION — the `cdk` CLI recomputes CDK_DEFAULT_* from
# the ambient AWS profile right before invoking this file, silently
# overwriting any override under those same names.
#
# Same AWS account as monte-carlo-intelligence / LG-docsOrch
# (828351637694 / us-east-2) — that account hosts other unrelated Fintor
# agents, so PredictedConditionsStack's resource names are
# predicted-conditions-specific (predicted-conditions-checkpoints, etc.) to
# avoid colliding with sibling stacks.
KNOWN_ACCOUNT = "828351637694"
KNOWN_REGION = "us-east-2"

ACCOUNT = os.environ.get("PREDICTED_CONDITIONS_AWS_ACCOUNT_ID", KNOWN_ACCOUNT)
REGION = os.environ.get("PREDICTED_CONDITIONS_AWS_REGION", KNOWN_REGION)

# Stage separation: PREDICTED_CONDITIONS_STAGE=dev|prod selects which stack
# instance to synthesize/deploy. Defaults to "dev" so a bare `cdk deploy`
# never accidentally touches prod.
STAGE = os.environ.get("PREDICTED_CONDITIONS_STAGE", "dev").strip().lower()
if STAGE not in ("dev", "prod"):
    raise SystemExit(f"PREDICTED_CONDITIONS_STAGE must be 'dev' or 'prod', got: {STAGE!r}")

STACK_ID = "PredictedConditionsStack" if STAGE == "prod" else "PredictedConditionsStack-Dev"

print(f"[infra/app.py] Targeting account={ACCOUNT} region={REGION} stage={STAGE}")
if ACCOUNT != KNOWN_ACCOUNT or REGION != KNOWN_REGION:
    print(
        f"[infra/app.py] NOTE: overridden via PREDICTED_CONDITIONS_AWS_ACCOUNT_ID/"
        f"PREDICTED_CONDITIONS_AWS_REGION (default is account={KNOWN_ACCOUNT} region={KNOWN_REGION})"
    )

app = cdk.App()
PredictedConditionsStack(
    app,
    STACK_ID,
    stage=STAGE,
    env=cdk.Environment(account=ACCOUNT, region=REGION),
)
app.synth()
