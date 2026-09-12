"""Operate only the explicitly authorized, single-runtime testing PoC."""

from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from dotenv import dotenv_values

HERE = Path(__file__).resolve().parent
TARGET = json.loads((HERE / "target.json").read_text())
STATE = Path.home() / ".local/state/cubeplex-agentcore-poc/20260912"
PREFIX = TARGET["resource_prefix"]
RUNTIME_NAME = TARGET["runtime_name"]
ACCOUNT = TARGET["account"]
REGION = TARGET["region"]
ROLE = f"{PREFIX}-execution"
TAGS = [
    {"Key": "Project", "Value": PREFIX},
    {"Key": "ManagedBy", "Value": "cubeplex-agentcore-poc"},
]


def save(name: str, value: Any) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    STATE.chmod(0o700)
    destination = STATE / name
    destination.write_text(json.dumps(value, indent=2, default=str) + "\n")
    destination.chmod(0o600)


def record(action: str, phase: str, **fields: Any) -> None:
    path = STATE / "operations.jsonl"
    with path.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "at": datetime.now(UTC).isoformat(),
                    "action": action,
                    "phase": phase,
                    **fields,
                }
            )
            + "\n"
        )
    path.chmod(0o600)


def session() -> boto3.Session:
    current = boto3.Session(profile_name=TARGET["profile"], region_name=REGION)
    caller = current.client("sts").get_caller_identity()
    if (
        caller["Account"] != ACCOUNT
        or caller["Arn"] != f"arn:aws:iam::{ACCOUNT}:user/perfecto"
    ):
        raise ValueError("unexpected_aws_identity")
    save("caller.json", {key: caller[key] for key in ("Account", "Arn", "UserId")})
    return current


def client(current: boto3.Session, name: str) -> Any:
    return current.client(
        name,
        config=Config(
            connect_timeout=10,
            read_timeout=30,
            retries={"total_max_attempts": 1, "mode": "standard"},
        ),
    )


def create_foundation(current: boto3.Session) -> None:
    """Create once; existing names and uncertain outcomes require independent inspection."""
    ecr = client(current, "ecr")
    secrets = client(current, "secretsmanager")
    iam = client(current, "iam")
    # Complete all collision checks before the first mutation.
    try:
        ecr.describe_repositories(repositoryNames=[PREFIX])
    except ecr.exceptions.RepositoryNotFoundException:
        pass
    else:
        raise ValueError("ecr_name_exists_inspect_before_resume")
    try:
        secrets.describe_secret(SecretId=f"{PREFIX}/provider")
    except secrets.exceptions.ResourceNotFoundException:
        pass
    else:
        raise ValueError("secret_name_exists_inspect_before_resume")
    try:
        iam.get_role(RoleName=ROLE)
    except iam.exceptions.NoSuchEntityException:
        pass
    else:
        raise ValueError("role_name_exists_inspect_before_resume")
    provider = dotenv_values(Path.home() / ".config/my-provider/.env")
    content = {
        "base_url": provider["BASE_URL"],
        "api_key": provider["KEY"],
        "model": provider["MODEL"],
        "effort": provider.get("EFFORT", "low"),
    }
    if not all(isinstance(value, str) and value for value in content.values()):
        raise ValueError("provider_file_missing_required_values")
    # Validate without serializing a pydantic ValidationError (it can contain input values).
    if content["effort"] not in {"minimal", "low", "medium", "high", "max"}:
        raise ValueError("unsupported_provider_effort")
    generated = json.loads((STATE / "runtime-policy-autopilot.json").read_text())
    statements = [
        statement
        for group in generated["Policies"]
        for statement in group["Policy"]["Statement"]
        if statement["Action"] == ["secretsmanager:GetSecretValue"]
    ]
    if len(statements) != 1:
        raise ValueError("unexpected_autopilot_policy")
    record("create_secret", "mutating")
    secret = secrets.create_secret(
        Name=f"{PREFIX}/provider",
        ClientRequestToken=str(uuid.uuid4()),
        SecretString=json.dumps(content),
        Tags=TAGS,
    )
    save("secret.json", {key: secret[key] for key in ("ARN", "Name", "VersionId")})
    metadata = secrets.describe_secret(SecretId=secret["ARN"])
    if secret["VersionId"] not in metadata["VersionIdsToStages"]:
        raise ValueError("secret_version_readback_mismatch")
    save("secret-readback.json", metadata)
    record("create_secret", "ready", arn=secret["ARN"])
    record("create_ecr", "mutating")
    repository = ecr.create_repository(
        repositoryName=PREFIX,
        imageTagMutability="IMMUTABLE",
        imageScanningConfiguration={"scanOnPush": True},
        encryptionConfiguration={"encryptionType": "AES256"},
        tags=TAGS,
    )["repository"]
    save("ecr.json", repository)
    readback = ecr.describe_repositories(repositoryNames=[PREFIX])["repositories"][0]
    if (
        readback["repositoryArn"] != repository["repositoryArn"]
        or readback["imageTagMutability"] != "IMMUTABLE"
    ):
        raise ValueError("ecr_readback_mismatch")
    save("ecr-readback.json", readback)
    record("create_ecr", "ready", arn=repository["repositoryArn"])
    trust = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                "Action": "sts:AssumeRole",
                "Condition": {
                    "StringEquals": {"aws:SourceAccount": ACCOUNT},
                    "ArnLike": {
                        "aws:SourceArn": f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/{RUNTIME_NAME}-*"
                    },
                },
            }
        ],
    }
    record("create_role", "mutating")
    role = iam.create_role(
        RoleName=ROLE,
        AssumeRolePolicyDocument=json.dumps(trust),
        Description="Single CubePlex AgentCore testing PoC execution role",
        Tags=TAGS,
    )["Role"]
    save("role.json", role)
    # Autopilot's GetSecretValue resource is concretized to the one created secret.
    # No kms:Decrypt is needed for the AWS-managed Secrets Manager encryption key.
    statements[0]["Resource"] = [secret["ARN"]]
    log_group = f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:/aws/bedrock-agentcore/runtimes/{RUNTIME_NAME}-*"
    baseline = [
        {
            "Sid": "PullOnlyPocImage",
            "Effect": "Allow",
            "Action": ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"],
            "Resource": [repository["repositoryArn"]],
        },
        {
            "Sid": "EcrAuthentication",
            "Effect": "Allow",
            "Action": ["ecr:GetAuthorizationToken"],
            "Resource": "*",
        },
        {
            "Sid": "PocLogGroup",
            "Effect": "Allow",
            "Action": [
                "logs:DescribeLogStreams",
                "logs:CreateLogGroup",
                "logs:PutResourcePolicy",
            ],
            "Resource": [log_group],
        },
        {
            "Sid": "PocLogStream",
            "Effect": "Allow",
            "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
            "Resource": [f"{log_group}:log-stream:*"],
        },
        {
            "Sid": "DiscoverLogGroups",
            "Effect": "Allow",
            "Action": ["logs:DescribeLogGroups"],
            "Resource": [f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:*"],
        },
    ]
    policy = {"Version": "2012-10-17", "Statement": baseline + statements}
    save("execution-policy.json", policy)
    save("execution-trust.json", trust)
    iam.put_role_policy(
        RoleName=ROLE,
        PolicyName="cubeplex-poc-runtime",
        PolicyDocument=json.dumps(policy),
    )
    role_readback = iam.get_role(RoleName=ROLE)["Role"]
    policy_readback = iam.get_role_policy(
        RoleName=ROLE, PolicyName="cubeplex-poc-runtime"
    )["PolicyDocument"]
    if role_readback["AssumeRolePolicyDocument"] != trust or policy_readback != policy:
        raise ValueError("role_readback_mismatch")
    save("role-readback.json", role_readback)
    save("policy-readback.json", policy_readback)
    record("create_role", "ready", arn=role["Arn"])
    print(json.dumps({"status": "foundation_ready"}))


def runtime_request(image_uri: str) -> dict[str, Any]:
    expected = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/{PREFIX}@sha256:"
    if (
        not image_uri.startswith(expected)
        or re.fullmatch(r"[a-f0-9]{64}", image_uri[len(expected) :]) is None
    ):
        raise ValueError("image_must_be_immutable_poc_ecr_digest")
    secret = json.loads((STATE / "secret.json").read_text())
    role = json.loads((STATE / "role.json").read_text())
    return {
        "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": image_uri}},
        "roleArn": role["Arn"],
        "networkConfiguration": {"networkMode": "PUBLIC"},
        "protocolConfiguration": {"serverProtocol": "HTTP"},
        "lifecycleConfiguration": {
            "idleRuntimeSessionTimeout": TARGET["idle_seconds"],
            "maxLifetime": TARGET["max_lifetime_seconds"],
        },
        "environmentVariables": {
            "CUBEPLEX_POC_PROVIDER_SECRET_ARN": secret["ARN"],
            "CUBEPLEX_POC_ALLOWED_TEAM_ID": TARGET["allowed_team_id"],
            "CUBEPLEX_POC_ALLOWED_CHANNEL_ID": TARGET["allowed_channel_id"],
            "CUBEPLEX_POC_ALLOWED_USER_IDS": TARGET["allowed_user_ids"],
            "CUBEPLEX_POC_ALLOWED_REPOSITORY": TARGET["repository"],
        },
    }


def create_runtime(current: boto3.Session, image_uri: str) -> None:
    control = client(current, "bedrock-agentcore-control")
    for page in control.get_paginator("list_agent_runtimes").paginate():
        if any(
            runtime["agentRuntimeName"] == RUNTIME_NAME
            for runtime in page["agentRuntimes"]
        ):
            raise ValueError("runtime_exists_inspect_before_resume")
    request = runtime_request(image_uri)
    request.update(
        agentRuntimeName=RUNTIME_NAME,
        clientToken=str(uuid.uuid4()),
        description="CubePlex CubeLoop read-only GitHub PoC",
        tags={tag["Key"]: tag["Value"] for tag in TAGS},
    )
    save("runtime-create-request.json", request)
    record("create_runtime", "mutating")
    response = control.create_agent_runtime(**request)
    save("runtime.json", response)
    record(
        "create_runtime",
        "transitional",
        arn=response["agentRuntimeArn"],
        runtime_id=response["agentRuntimeId"],
    )
    print(
        json.dumps(
            {
                key: response[key]
                for key in (
                    "agentRuntimeArn",
                    "agentRuntimeId",
                    "agentRuntimeVersion",
                    "status",
                )
            }
        )
    )


def readback(current: boto3.Session) -> None:
    resource = json.loads((STATE / "runtime.json").read_text())
    response = client(current, "bedrock-agentcore-control").get_agent_runtime(
        agentRuntimeId=resource["agentRuntimeId"]
    )
    save("runtime-readback.json", response)
    status = response["status"]
    if status == "READY":
        expected = json.loads((STATE / "runtime-create-request.json").read_text())
        for key in (
            "agentRuntimeArtifact",
            "roleArn",
            "networkConfiguration",
            "protocolConfiguration",
            "lifecycleConfiguration",
            "environmentVariables",
        ):
            if response.get(key) != expected[key]:
                raise ValueError(f"runtime_readback_mismatch_{key}")
        record(
            "create_runtime",
            "ready",
            arn=response["agentRuntimeArn"],
            version=response["agentRuntimeVersion"],
        )
    print(
        json.dumps(
            {
                key: response.get(key)
                for key in (
                    "agentRuntimeArn",
                    "agentRuntimeId",
                    "agentRuntimeVersion",
                    "status",
                    "failureReason",
                    "lifecycleConfiguration",
                )
            }
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("foundation")
    runtime = commands.add_parser("runtime")
    runtime.add_argument("--image-uri", required=True)
    commands.add_parser("readback")
    args = parser.parse_args()
    try:
        current = session()
        if args.command == "foundation":
            create_foundation(current)
        elif args.command == "runtime":
            create_runtime(current, args.image_uri)
        else:
            readback(current)
        return 0
    except ClientError as error:
        # Never serialize an AWS request; SecretString and provider data stay in memory.
        print(
            json.dumps(
                {"error_type": "aws_error", "code": error.response["Error"]["Code"]}
            ),
            file=sys.stderr,
        )
        return 1
    except (ValueError, KeyError) as error:
        print(
            json.dumps({"error_type": type(error).__name__, "code": str(error)}),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
