"""Run a reviewed script on the node owned by this PoC's CloudFormation stack."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time

import boto3
from botocore.config import Config


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("script", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile", default="moego-testing")
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument("--stack", default="cubeplex-product-20260912")
    args = parser.parse_args()
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    identity = session.client("sts").get_caller_identity()
    if identity["Account"] != "986420599013" or args.region != "us-west-2":
        raise RuntimeError(
            "This testing operator requires the authorized account and region"
        )
    stack = session.client("cloudformation").describe_stacks(StackName=args.stack)[
        "Stacks"
    ][0]
    outputs = {item["OutputKey"]: item["OutputValue"] for item in stack["Outputs"]}
    client = session.client("ssm", config=Config(retries={"total_max_attempts": 1}))
    result = client.send_command(
        InstanceIds=[outputs["InstanceId"]],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": [args.script.read_text()], "executionTimeout": ["600"]},
        Comment="CubePlex testing deployment operation",
        TimeoutSeconds=60,
    )
    command_id = result["Command"]["CommandId"]
    print(
        json.dumps({"command_id": command_id, "instance_id": outputs["InstanceId"]}),
        flush=True,
    )
    for _ in range(330):
        time.sleep(2)
        try:
            invocation = client.get_command_invocation(
                CommandId=command_id, InstanceId=outputs["InstanceId"]
            )
        except client.exceptions.InvocationDoesNotExist:
            continue
        if invocation["Status"] in {"Pending", "InProgress", "Delayed"}:
            continue
        args.output.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(invocation, stream, indent=2, default=str)
        print(json.dumps({"status": invocation["Status"], "output": str(args.output)}))
        if invocation["Status"] != "Success":
            raise SystemExit(1)
        return
    raise TimeoutError(f"Read back SSM command {command_id}; do not blindly rerun it")


if __name__ == "__main__":
    main()
