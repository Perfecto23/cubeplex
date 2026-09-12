"""Publish this testing node's kubeconfig to its designated credential store."""

import argparse
from pathlib import Path

import boto3


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("secret_arn")
    args = parser.parse_args()
    client = boto3.client("secretsmanager", region_name="us-west-2")
    client.put_secret_value(
        SecretId=args.secret_arn,
        SecretString=Path("/etc/rancher/k3s/k3s.yaml").read_text(),
    )
    print("Cluster access material stored successfully")


if __name__ == "__main__":
    main()
