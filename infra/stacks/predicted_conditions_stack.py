"""AWS CDK stack for the predicted-conditions serverless agent.

Lambda + Fargate (added 2026-10-07): runs typically take 8-11 minutes,
comfortably under Lambda's 900s hard cap, but some inputs (more submitted
documents -> more satisfaction-check passes) legitimately exceed it — two
real runs were confirmed hard-killed by AWS at exactly 900.00s via
CloudWatch (`Status: timeout`), with no bug/hang involved, just more work
than Lambda allows. Added the same Lambda+Fargate pattern used by
longer-running agents (LG-discOrch, LG-docsOrch): the Lambda stays the API
front door (POST /threads/{id}/runs still returns immediately with the same
URL/auth), background-run *execution* now dispatches to an ECS Fargate task
(api/worker.py, no execution-time ceiling) instead of the original
Lambda-self-invoke, which remains as a fallback — see
api/services/runs.py:_dispatch_background_run for the exact priority order.
Adapted from the LG-docsOrch reference implementation (see
docs/LANGSMITH_TO_AWS_MIGRATION_PLAYBOOK.md in that repo) for the general
Lambda+Fargate pattern, and from monte-carlo-intelligence for the
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
    aws_ec2 as ec2,
    aws_ecr_assets as ecr_assets,
    aws_ecs as ecs,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
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

        # ── Fargate worker: escapes Lambda's 900s ceiling for long runs ────
        #
        # Most predicted-conditions runs finish in 8-11 minutes (fine inline
        # in Lambda), but some legitimately exceed 900s (confirmed via
        # CloudWatch on 2026-10-07 — two runs hard-killed by AWS at exactly
        # 900.00s, no bug involved). The Lambda stays the API front door
        # (POST /threads/{id}/runs still returns immediately, same URL);
        # execution for background runs dispatches here instead, via
        # ecs.run_task from api/services/runs.py. Same Docker image as the
        # Lambda (api/Dockerfile), just an entry_point + command override —
        # see the WorkerContainer definition below for why both (not just
        # command) need overriding.
        # Reuse the account's existing default VPC instead of provisioning a
        # new one — this account's us-east-2 VPC quota (5, the AWS default)
        # was already exhausted by sibling agents' own dedicated worker VPCs
        # (DocsOrchAgentStack x2, DiscOrchAgentStack x2) plus the account's
        # own default VPC, confirmed via a failed real deploy on 2026-10-07
        # (`CREATE_FAILED ... The maximum number of VPCs has been reached`).
        # A default VPC already has public subnets + an internet gateway in
        # every AZ with MapPublicIpOnLaunch=true, which is exactly what this
        # worker needs (public-subnet-only, assignPublicIp=ENABLED below, no
        # NAT) — no need for a dedicated VPC at all. from_lookup needs a
        # one-time AWS-credentialed context lookup at `cdk synth` time
        # (cached afterward in cdk.context.json), same as the AZ-lookup
        # pitfall already documented elsewhere for this pattern.
        worker_vpc = ec2.Vpc.from_lookup(self, "WorkerVpc", is_default=True)

        worker_security_group = ec2.SecurityGroup(
            self,
            "WorkerSecurityGroup",
            vpc=worker_vpc,
            description=(
                "predicted-conditions Fargate worker - outbound only "
                "(Anthropic, OpenAI, DynamoDB, Secrets Manager, LangSmith); "
                "no inbound needed."
            ),
            allow_all_outbound=True,
        )

        worker_cluster = ecs.Cluster(
            self,
            "WorkerCluster",
            cluster_name=(None if is_prod else f"predicted-conditions-worker{suffix}"),
            vpc=worker_vpc,
            container_insights_v2=ecs.ContainerInsights.ENABLED,
        )

        worker_log_group = logs.LogGroup(
            self,
            "WorkerLogGroup",
            log_group_name=f"/predicted-conditions/worker{suffix}",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        worker_container_name = "PredictedConditionsWorker"
        worker_task_definition = ecs.FargateTaskDefinition(
            self,
            "WorkerTaskDefinition",
            cpu=2048,  # 2 vCPU
            memory_limit_mib=8192,  # 8 GB — headroom for the same workload the 2048MB Lambda runs today
            runtime_platform=ecs.RuntimePlatform(
                cpu_architecture=ecs.CpuArchitecture.ARM64,
                operating_system_family=ecs.OperatingSystemFamily.LINUX,
            ),
        )
        worker_task_definition.add_container(
            "WorkerContainer",
            container_name=worker_container_name,
            image=ecs.ContainerImage.from_asset(
                str(REPO_ROOT),
                file="api/Dockerfile",
                exclude=_DOCKER_BUILD_EXCLUDES,
                platform=_DOCKER_PLATFORM,
            ),
            # api/Dockerfile's base image (public.ecr.aws/lambda/python:3.12)
            # ENTRYPOINTs into /lambda-entrypoint.sh, which hands off to the
            # Lambda Runtime Interface Client — that polls the Lambda
            # Runtime API for invocation events. Outside Lambda (here, in
            # Fargate) that API doesn't exist, so the RIC would just hang
            # forever waiting for an invocation that never comes. Overriding
            # entry_point (not just command) bypasses that script entirely
            # and runs api/worker.py as a plain script instead.
            entry_point=["python3"],
            command=["-m", "api.worker"],
            environment={
                "CHECKPOINT_TABLE_NAME": checkpoint_table.table_name,
                "CHECKPOINT_S3_BUCKET": checkpoint_offload_bucket.bucket_name,
                "AGENT_SECRETS_ARN": agent_secrets.secret_arn,
                "ANTHROPIC_MODEL": _deploy_env("ANTHROPIC_MODEL", "claude-opus-4-5"),
                "ANTHROPIC_FALLBACK_MODEL": _deploy_env("ANTHROPIC_FALLBACK_MODEL", "claude-sonnet-4-5"),
                "LLM_TEMPERATURE": _deploy_env("LLM_TEMPERATURE", "0.2"),
                "OPENAI_FALLBACK_MODEL": _deploy_env("OPENAI_FALLBACK_MODEL", "gpt-5"),
                "OPENAI_REASONING_EFFORT": _deploy_env("OPENAI_REASONING_EFFORT", "medium"),
                "PRIMARY_PROVIDER": _deploy_env("PRIMARY_PROVIDER", ""),
                "OPENAI_PRIMARY_MODEL": _deploy_env("OPENAI_PRIMARY_MODEL", ""),
                "LLM_MAX_RETRIES": _deploy_env("LLM_MAX_RETRIES", "8"),
                "LLM_RETRY_COOLDOWN": _deploy_env("LLM_RETRY_COOLDOWN", "5"),
                "LLM_RETRY_MAX_BACKOFF": _deploy_env("LLM_RETRY_MAX_BACKOFF", "60"),
                "LANGCHAIN_TRACING_V2": _deploy_env("LANGCHAIN_TRACING_V2", "true"),
                "LANGCHAIN_PROJECT": _deploy_env("LANGCHAIN_PROJECT", f"predicted-conditions{suffix}"),
                # Worker's own safety net against a runaway/hung task — see
                # api/worker.py's wall-clock guard. Deliberately much longer
                # than Lambda's 900s ceiling (that's the whole point) and
                # than this agent's normal 8-13 min runtime.
                "PC_WORKER_MAX_RUN_SECONDS": _deploy_env("PC_WORKER_MAX_RUN_SECONDS", str(30 * 60)),
            },
            logging=ecs.LogDrivers.aws_logs(stream_prefix="worker", log_group=worker_log_group),
        )

        checkpoint_table.grant_read_write_data(worker_task_definition.task_role)
        checkpoint_offload_bucket.grant_read_write(worker_task_definition.task_role)
        agent_secrets.grant_read(worker_task_definition.task_role)

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
            # 900s is the Lambda platform maximum. Background runs no longer
            # execute inline in this Lambda at all once Fargate is
            # configured (the WORKER_* env vars below) — they're dispatched
            # to the Fargate worker via ecs.run_task (see
            # api/services/runs.py:create_background_run). This timeout only
            # bounds synchronous /runs/wait, /runs/stream, the dispatch call
            # itself (all fast), and the legacy Lambda-self-invoke fallback
            # path (used only if the Fargate env vars below are unset).
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
                # Only applied when the active model is non-Opus (Opus's
                # extended thinking forces temperature=1 regardless — see
                # agent.py). Lower = more consistent wording across reruns.
                "LLM_TEMPERATURE": _deploy_env("LLM_TEMPERATURE", "0.2"),
                "OPENAI_FALLBACK_MODEL": _deploy_env("OPENAI_FALLBACK_MODEL", "gpt-5"),
                "OPENAI_REASONING_EFFORT": _deploy_env("OPENAI_REASONING_EFFORT", "medium"),
                # Dev-only override added 2026-10-05 for the ANTHROPIC_API_KEY
                # outage: set PRIMARY_PROVIDER=openai to run entirely on
                # OPENAI_PRIMARY_MODEL instead of Anthropic (see agent.py's
                # primary-model-selection block). Both default to "" / unset
                # so normal (Anthropic-primary) behavior is unaffected unless
                # explicitly opted into via .env or an inline env var on the
                # deploy command.
                "PRIMARY_PROVIDER": _deploy_env("PRIMARY_PROVIDER", ""),
                "OPENAI_PRIMARY_MODEL": _deploy_env("OPENAI_PRIMARY_MODEL", ""),
                "LLM_MAX_RETRIES": _deploy_env("LLM_MAX_RETRIES", "8"),
                "LLM_RETRY_COOLDOWN": _deploy_env("LLM_RETRY_COOLDOWN", "5"),
                "LLM_RETRY_MAX_BACKOFF": _deploy_env("LLM_RETRY_MAX_BACKOFF", "60"),
                # Separate LangSmith project per stage so dev traces don't
                # mix into prod's trace history. LangSmith tracing is kept
                # alongside AWS hosting for observability continuity.
                "LANGCHAIN_TRACING_V2": _deploy_env("LANGCHAIN_TRACING_V2", "true"),
                "LANGCHAIN_PROJECT": _deploy_env("LANGCHAIN_PROJECT", f"predicted-conditions{suffix}"),
                "CORS_ALLOW_ORIGINS": _deploy_env("CORS_ALLOW_ORIGINS", "*"),
                # Fargate worker dispatch target — see
                # api/services/runs.py:_dispatch_via_fargate. If any of these
                # were ever unset, create_background_run falls back to the
                # Lambda self-invoke path (900s-capped) below instead.
                "WORKER_TASK_DEFINITION_ARN": worker_task_definition.task_definition_arn,
                "WORKER_CLUSTER_ARN": worker_cluster.cluster_arn,
                "WORKER_SUBNET_IDS": ",".join(s.subnet_id for s in worker_vpc.public_subnets),
                "WORKER_SECURITY_GROUP_ID": worker_security_group.security_group_id,
                "WORKER_CONTAINER_NAME": worker_container_name,
            },
        )

        checkpoint_table.grant_read_write_data(agent_fn)
        checkpoint_offload_bucket.grant_read_write(agent_fn)
        agent_secrets.grant_read(agent_fn)

        # Let the Lambda's own role dispatch Fargate tasks for background
        # runs. Scoped to this task definition's family (any revision — a
        # redeploy bumps the revision number) and this cluster only — not
        # "*".
        agent_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ecs:RunTask"],
                resources=[
                    f"arn:aws:ecs:{self.region}:{self.account}:task-definition/"
                    f"{worker_task_definition.family}:*"
                ],
                conditions={"ArnEquals": {"ecs:cluster": worker_cluster.cluster_arn}},
            )
        )
        agent_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["iam:PassRole"],
                resources=[
                    worker_task_definition.task_role.role_arn,
                    worker_task_definition.obtain_execution_role().role_arn,
                ],
            )
        )

        # Lambda self-invoke fallback for background runs (used only if the
        # Fargate env vars above are ever unset) — see
        # api/services/runs.py:_dispatch_via_lambda_self_invoke. Resource-based
        # permission (add_permission), not an identity-policy statement, to
        # avoid a circular CloudFormation dependency between the function and
        # its own default policy (same issue the ecs:RunTask/iam:PassRole
        # statements above would otherwise create too, if attached via
        # grant_invoke instead — see LG-docsOrch's stack for the identical
        # pattern/explanation).
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
        CfnOutput(self, "WorkerClusterArn", value=worker_cluster.cluster_arn)
        CfnOutput(self, "WorkerTaskDefinitionArn", value=worker_task_definition.task_definition_arn)
        CfnOutput(self, "WorkerLogGroupName", value=worker_log_group.log_group_name)
        CfnOutput(self, "Stage", value=stage)
