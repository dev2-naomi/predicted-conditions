"""AWS CDK stack for the predicted-conditions serverless agent.

Lambda-only (no Fargate): runs typically take 8-11 minutes, comfortably
under Lambda's 900s hard cap, so this doesn't need the Lambda+Fargate
pattern used by longer-running agents (e.g. LG-discOrch, LG-docsOrch).
Adapted from the monte-carlo-intelligence AWS migration reference — see
AWS_DEPLOYMENT_PLAYBOOK.md for the shared rationale behind the
secrets/region-pinning patterns used here.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_dynamodb as dynamodb,
    aws_ecr_assets as ecr_assets,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_s3 as s3,
    aws_secretsmanager as secretsmanager,
)
from constructs import Construct

REPO_ROOT = Path(__file__).resolve().parents[2]

# Keep this in sync with api/secrets.py:SECRET_KEYS.
SECRET_KEYS = [
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "LANGCHAIN_API_KEY",
    "API_KEY",
]

# This repo's root is cluttered with large ad-hoc test/debug artifacts
# (multi-MB JSON state dumps, .zip archives, log files) accumulated during
# local development — none of it is needed by the Lambda image, and
# including it would bloat every Docker build. Everything under data/,
# plans/, tools/, api/, and the handful of top-level .py files the
# Dockerfile explicitly COPYs is what actually ships.
_DOCKER_BUILD_EXCLUDES = [
    ".venv",
    "venv",
    ".git",
    "tests",
    "docs",
    "infra",
    "test_results",
    "test_results.zip",
    "compiled_inputs",
    "compiled_inputs.zip",
    "batch_results",
    "config",
    "orchestrator and focused md prompts",
    "plans/v2_migration_plan.md",
    "plans/a.txt",
    "*.log",
    "*.zip",
    "*_output.json",
    "*_final_state.json",
    "*_state.json",
    "*_run.log",
    "*_run_log.txt",
    "*_logs.json",
    "*_logs.txt",
    "*_manifest.json",
    "thread_*",
    "cloud_*.json",
    "output-manifest-*.json",
    "prechange.json",
    "postchange.json",
]
_DOCKER_PLATFORM = ecr_assets.Platform.LINUX_ARM64

# Everything api/Dockerfile actually COPYs into the image. CDK's default
# source-hash for DockerImageCode.from_image_asset() failed to pick up a
# Dockerfile-only edit here (same hash before/after editing api/Dockerfile,
# reproduced via `cdk diff` — likely a hashing quirk on this large repo
# tree), so we compute our own content hash of exactly the shipped files
# and pass it as extra_hash to force a rebuild whenever any of them change.
_IMAGE_SOURCE_PATHS = [
    "api/Dockerfile",
    "api/requirements.txt",
    "agent.py",
    "registry.py",
    "step_loader.py",
    "tools",
    "data",
    "plans",
    "api",
]


def _image_source_hash() -> str:
    import hashlib

    h = hashlib.sha256()
    paths: list[Path] = []
    for rel in _IMAGE_SOURCE_PATHS:
        p = REPO_ROOT / rel
        if p.is_file():
            paths.append(p)
        elif p.is_dir():
            paths.extend(sorted(f for f in p.rglob("*") if f.is_file()))
    for f in sorted(paths):
        h.update(str(f.relative_to(REPO_ROOT)).encode())
        h.update(f.read_bytes())
    return h.hexdigest()


def _deploy_env(name: str, default: str = "") -> str:
    """Read deploy-time env (source .env before cdk deploy)."""
    return os.environ.get(name, default)


class PredictedConditionsStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, stage: str = "dev", **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        is_prod = stage == "prod"
        suffix = "" if is_prod else f"-{stage}"

        checkpoint_table = dynamodb.Table(
            self,
            "CheckpointTable",
            table_name=f"predicted-conditions-checkpoints{suffix}",
            partition_key=dynamodb.Attribute(name="PK", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="SK", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # DynamoDB caps items at 400KB. predicted-conditions' LangGraph state
        # (loan XML + manifest/eligibility JSON + accumulated module/step
        # outputs) regularly exceeds that by the later steps of a run, so
        # checkpoints/writes over ~350KB get offloaded here by
        # DynamoDBSaver's built-in s3_offload_config (see api/checkpointer.py)
        # instead of failing the PutItem call outright.
        checkpoint_offload_bucket = s3.Bucket(
            self,
            "CheckpointOffloadBucket",
            # Auto-generated name (CDK-assigned) — S3 bucket names are
            # globally unique across all AWS accounts, so a fixed name
            # risks collisions between dev/prod or re-deploys.
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            lifecycle_rules=[
                s3.LifecycleRule(expiration=Duration.days(30)),
            ],
        )

        # Real secret values are intentionally NOT passed to CDK/CloudFormation
        # here. GenerateSecretString seeds an empty placeholder once, at
        # creation; real values are pushed via
        # `aws secretsmanager put-secret-value` in scripts/deploy.sh /
        # .github/workflows/deploy.yml, after `cdk deploy` — a plain API
        # call, never a CloudFormation resource property, so it can't end up
        # in a synthesized template.
        agent_secrets = secretsmanager.Secret(
            self,
            "AgentSecrets",
            secret_name=(None if is_prod else f"predicted-conditions-agent-secrets{suffix}"),
            description=(
                f"predicted-conditions agent API keys ({stage}). Values are managed "
                "out-of-band via `aws secretsmanager put-secret-value` (see "
                "scripts/deploy.sh), not by CloudFormation."
            ),
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template=json.dumps({key: "" for key in SECRET_KEYS}),
                generate_string_key="_cfn_placeholder",
                exclude_punctuation=True,
            ),
        )

        agent_fn = lambda_.DockerImageFunction(
            self,
            "AgentFunction",
            function_name=(None if is_prod else f"predicted-conditions-agent{suffix}"),
            description=f"predicted-conditions LangGraph Platform-compatible agent API ({stage})",
            code=lambda_.DockerImageCode.from_image_asset(
                str(REPO_ROOT),
                file="api/Dockerfile",
                exclude=_DOCKER_BUILD_EXCLUDES,
                platform=_DOCKER_PLATFORM,
                extra_hash=_image_source_hash(),
            ),
            memory_size=2048,
            # 900s is the Lambda platform maximum. Full predicted-conditions
            # runs take 8-11 minutes — comfortably inside this, so unlike
            # longer-running agents this stack has no Fargate worker at all
            # (see the module docstring / AWS_DEPLOYMENT_PLAYBOOK.md's
            # "When to add Fargate?" ADR).
            timeout=Duration.seconds(900),
            architecture=lambda_.Architecture.ARM_64,
            environment={
                # Non-secret config only. Secrets are intentionally NOT set
                # here — api/secrets.py fetches them from AGENT_SECRETS_ARN
                # at cold start instead.
                "CHECKPOINT_TABLE_NAME": checkpoint_table.table_name,
                "CHECKPOINT_S3_BUCKET": checkpoint_offload_bucket.bucket_name,
                "AGENT_SECRETS_ARN": agent_secrets.secret_arn,
                "ANTHROPIC_MODEL": _deploy_env("ANTHROPIC_MODEL", "claude-opus-4-5"),
                "ANTHROPIC_FALLBACK_MODEL": _deploy_env("ANTHROPIC_FALLBACK_MODEL", "claude-sonnet-4-5"),
                "OPENAI_FALLBACK_MODEL": _deploy_env("OPENAI_FALLBACK_MODEL", "gpt-5"),
                "OPENAI_REASONING_EFFORT": _deploy_env("OPENAI_REASONING_EFFORT", "medium"),
                "LLM_MAX_RETRIES": _deploy_env("LLM_MAX_RETRIES", "8"),
                "LLM_RETRY_COOLDOWN": _deploy_env("LLM_RETRY_COOLDOWN", "5"),
                "LLM_RETRY_MAX_BACKOFF": _deploy_env("LLM_RETRY_MAX_BACKOFF", "60"),
                # Separate LangSmith project per stage so dev traces don't
                # mix into prod's trace history. LangSmith tracing is kept
                # alongside AWS hosting for observability continuity.
                "LANGCHAIN_TRACING_V2": _deploy_env("LANGCHAIN_TRACING_V2", "true"),
                "LANGCHAIN_PROJECT": _deploy_env("LANGCHAIN_PROJECT", f"predicted-conditions{suffix}"),
                "CORS_ALLOW_ORIGINS": _deploy_env("CORS_ALLOW_ORIGINS", "*"),
            },
        )

        checkpoint_table.grant_read_write_data(agent_fn)
        checkpoint_offload_bucket.grant_read_write(agent_fn)
        agent_secrets.grant_read(agent_fn)

        # Lambda self-invoke fallback for background runs — see
        # api/services/runs.py:_dispatch_via_lambda_self_invoke. Resource-based
        # permission (add_permission), not an identity-policy statement, to
        # avoid a circular CloudFormation dependency between the function and
        # its own default policy (same issue documented in LG-docsOrch's
        # stack for the identical pattern).
        agent_fn.add_permission(
            "SelfInvoke",
            principal=iam.ArnPrincipal(agent_fn.role.role_arn),
            action="lambda:InvokeFunction",
        )
        # No automatic retries of a partial/failed background run — a retry
        # would silently re-execute a run that may have already produced
        # partial side effects. create_background_run already surfaces
        # failures as a terminal "error" run-status update.
        agent_fn.configure_async_invoke(retry_attempts=0)

        # Lambda Function URL — no API Gateway 30s cap (runs up to the
        # Lambda timeout, 900s here). See AWS_DEPLOYMENT_PLAYBOOK.md's
        # "Why Lambda + Function URL (not API Gateway)?" ADR.
        #
        # BUFFERED, not RESPONSE_STREAM: response streaming on a Function
        # URL requires the handler itself to use Lambda's streaming response
        # API (awslambdaric's streaming decorator), which plain Mangum
        # doesn't implement. /runs/stream (SSE) still returns a full
        # response, just not incrementally — every other endpoint here
        # returns a normal buffered JSON response anyway.
        function_url = agent_fn.add_function_url(
            auth_type=lambda_.FunctionUrlAuthType.NONE,
            invoke_mode=lambda_.InvokeMode.BUFFERED,
            cors=lambda_.FunctionUrlCorsOptions(
                allowed_origins=["*"],
                allowed_methods=[lambda_.HttpMethod.ALL],
                allowed_headers=["content-type", "authorization", "x-api-key"],
                allow_credentials=True,
            ),
        )

        CfnOutput(self, "ApiUrl", value=function_url.url)
        CfnOutput(self, "FunctionUrl", value=function_url.url)
        CfnOutput(self, "CheckpointTableName", value=checkpoint_table.table_name)
        CfnOutput(self, "CheckpointOffloadBucketName", value=checkpoint_offload_bucket.bucket_name)
        CfnOutput(self, "AgentSecretsArn", value=agent_secrets.secret_arn)
        CfnOutput(self, "LambdaFunctionName", value=agent_fn.function_name)
        CfnOutput(self, "Stage", value=stage)
