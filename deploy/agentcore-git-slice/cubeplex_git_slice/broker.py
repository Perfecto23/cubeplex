"""IAM-admitted Lambda broker. No Agent, repository checkout or tool execution."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import boto3
import httpx
from botocore.config import Config

from .git_publication import BRANCH, REMOTE, REPO, SHA, GitPublication, unbase64
from .state import BrokerError, TaskStore, canonical, digest

OPS = {"manifest", "model", "push", "pr", "checkpoint_put", "checkpoint_get", "status"}
ENVELOPE = {"version", "task_id", "stage", "capability", "request_id", "op", "data"}
SNAPSHOT = {
    "schema_version",
    "stage",
    "boot_id",
    "head_sha",
    "base_sha",
    "branch",
    "git_bundle_b64",
    "patch_b64",
    "untracked",
    "messages",
    "metrics",
    "result",
}
MODEL_FIELDS = {
    "model",
    "input",
    "instructions",
    "tools",
    "tool_choice",
    "parallel_tool_calls",
    "max_output_tokens",
    "reasoning",
    "include",
    "temperature",
    "top_p",
    "stream",
    "store",
    "text",
}


def _fields(value: Any, expected: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise BrokerError("invalid_fields")
    return value


def _string(value: Any, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum:
        raise BrokerError("invalid_value")
    return value


def _sha(value: Any) -> str:
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise BrokerError("invalid_commit")
    return value


def _limit(manifest: dict[str, Any], key: str, hard_max: int, *, minimum: int = 1) -> int:
    value = manifest.get(key)
    if type(value) is not int or not minimum <= value <= hard_max:
        raise BrokerError("manifest_invalid")
    return value


def validate_manifest(manifest: dict[str, Any], task_id: str) -> None:
    if (
        manifest.get("schema_version") != 1
        or manifest.get("task_id") != task_id
        or manifest.get("repo") != REPO
        or manifest.get("remote_url") != REMOTE
        or manifest.get("branch") != BRANCH
        or manifest.get("allowed_paths") != ["intervals.py"]
        or manifest.get("artifact_paths") != ["README.md", "continuation.md"]
        or manifest.get("active_stage") not in {"work", "resume"}
    ):
        raise BrokerError("manifest_invalid")
    _sha(manifest.get("base_sha"))
    _string(manifest.get("model"))
    if not re.fullmatch(r"[0-9a-f]{64}", str(manifest.get("capability_sha256", ""))):
        raise BrokerError("manifest_invalid")
    for key, maximum in {
        "max_model_calls": 100,
        "max_model_request_bytes": 65536,
        "max_model_output_tokens": 2048,
        "max_bundle_bytes": 2097152,
        "max_snapshot_bytes": 4194304,
        "max_changed_bytes": 65536,
    }.items():
        _limit(manifest, key, maximum, minimum=0 if key == "max_model_calls" else 1)


def _model_body(value: Any, manifest: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - MODEL_FIELDS:
        raise BrokerError("model_request_denied")
    if len(canonical(value)) > manifest["max_model_request_bytes"]:
        raise BrokerError("model_request_too_large")
    if "include" in value and value["include"] not in ([], ["reasoning.encrypted_content"]):
        raise BrokerError("model_request_denied")
    if (
        value.get("model") != manifest["model"]
        or value.get("stream") is not True
        or value.get("store") is not False
    ):
        raise BrokerError("model_request_denied")
    maximum = value.get("max_output_tokens", manifest["max_model_output_tokens"])
    if type(maximum) is not int or not 1 <= maximum <= manifest["max_model_output_tokens"]:
        raise BrokerError("model_output_limit")
    tools = value.get("tools", [])
    if not isinstance(tools, list) or len(tools) > 16:
        raise BrokerError("model_tools_denied")
    for tool in tools:
        if (
            not isinstance(tool, dict)
            or tool.get("type") != "function"
            or set(tool) - {"type", "name", "description", "parameters", "strict"}
        ):
            raise BrokerError("model_tools_denied")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,63}", str(tool.get("name", ""))):
            raise BrokerError("model_tools_denied")
    inputs = value.get("input")
    if not isinstance(inputs, (str, list)):
        raise BrokerError("model_input_denied")
    for item in inputs if isinstance(inputs, list) else []:
        if not isinstance(item, dict) or item.get("type", "message") not in {
            "message",
            "function_call",
            "function_call_output",
            "reasoning",
        }:
            raise BrokerError("model_input_denied")
        content = item.get("content", [])
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict) or block.get("type") not in {
                    "input_text",
                    "output_text",
                    "refusal",
                }:
                    raise BrokerError("model_input_denied")
    result = dict(value)
    result["max_output_tokens"] = maximum
    return result


def validate_sse(raw: bytes) -> str:
    """Reject EOF/incomplete rather than triggering CubeLoop's partial-EOF fallback."""
    try:
        text = raw.decode("utf-8")
        if not text.replace("\r\n", "\n").endswith("\n\n"):
            raise BrokerError("model_response_invalid")
        completed = False
        for packet in text.replace("\r\n", "\n").split("\n\n"):
            data = "\n".join(
                line[5:].lstrip(" ") for line in packet.splitlines() if line.startswith("data:")
            )
            if not data:
                continue
            if data == "[DONE]":
                if not completed:
                    raise BrokerError("model_response_invalid")
                continue
            event = json.loads(data)
            kind = event.get("type")
            if completed or kind in {"error", "response.failed", "response.incomplete"}:
                raise BrokerError("model_response_invalid")
            if kind == "response.completed":
                if event.get("response", {}).get("status") != "completed":
                    raise BrokerError("model_response_invalid")
                completed = True
        if not completed:
            raise BrokerError("model_response_invalid")
        return text
    except (ValueError, UnicodeError, AttributeError) as exc:
        raise BrokerError("model_response_invalid") from exc


class Broker:
    def __init__(
        self,
        *,
        store: TaskStore,
        task_id: str,
        secrets: Any,
        model_secret_arn: str,
        github_secret_arn: str,
        http: httpx.Client,
        publication: GitPublication | None = None,
    ) -> None:
        self.store, self.task_id, self.secrets = store, task_id, secrets
        self.model_secret_arn, self.github_secret_arn = model_secret_arn, github_secret_arn
        self.http = http
        self.publication = publication or GitPublication(store, self._github_token, http)

    def _secret(self, arn: str) -> dict[str, Any]:
        try:
            result = json.loads(self.secrets.get_secret_value(SecretId=arn)["SecretString"])
            if not isinstance(result, dict):
                raise ValueError("invalid secret")
            return result
        except Exception as exc:
            raise BrokerError("credential_unavailable") from exc

    def _github_token(self) -> str:
        token = _string(self._secret(self.github_secret_arn).get("token"), 8192)
        if "\n" in token or "\r" in token:
            raise BrokerError("credential_unavailable")
        return token

    def _authorize(self, event: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        event = _fields(event, ENVELOPE)
        if (
            type(event["version"]) is not int
            or event["version"] != 1
            or event["task_id"] != self.task_id
        ):
            raise BrokerError("task_denied")
        if event["stage"] not in {"work", "resume"} or event["op"] not in OPS:
            raise BrokerError("operation_denied")
        if not isinstance(event["data"], dict):
            raise BrokerError("invalid_fields")
        try:
            if str(UUID(event["request_id"])) != event["request_id"]:
                raise ValueError("noncanonical request id")
        except (ValueError, TypeError, AttributeError) as exc:
            raise BrokerError("invalid_request_id") from exc
        capability = _string(event["capability"], 512)
        manifest = self.store.manifest()
        validate_manifest(manifest, self.task_id)
        try:
            deadline = datetime.fromisoformat(manifest["deadline"].replace("Z", "+00:00"))
        except (ValueError, KeyError, AttributeError) as exc:
            raise BrokerError("manifest_invalid") from exc
        if deadline.tzinfo is None or deadline <= datetime.now(UTC):
            raise BrokerError("capability_expired")
        supplied = hashlib.sha256(capability.encode()).hexdigest()
        if event["stage"] != manifest["active_stage"] or not hmac.compare_digest(
            supplied, manifest["capability_sha256"]
        ):
            raise BrokerError("capability_denied")
        if len(canonical(event)) > 5 * 1024 * 1024:
            raise BrokerError("payload_too_large")
        return event, manifest

    def handle(self, event: Any) -> dict[str, Any]:
        request_id: str | None = None
        try:
            event, manifest = self._authorize(event)
            fingerprint = digest(
                {key: value for key, value in event.items() if key != "capability"}
            )
            owner, record = self.store.begin_request(
                event["request_id"], fingerprint, event["op"], event["stage"]
            )
            request_id = event["request_id"]
            if not owner:
                if record["status"] in {"done", "failed"}:
                    return record["response"]
                if event["op"] not in {"push", "pr"}:
                    return {"ok": False, "error": {"code": "request_outcome_unknown"}}
            result = self._dispatch(event, manifest, readonly=not owner)
            return self.store.finish_request(request_id, {"ok": True, "result": result})
        except BrokerError as exc:
            response = {"ok": False, "error": {"code": exc.code}}
        except Exception:
            response = {"ok": False, "error": {"code": "broker_unavailable"}}
        if request_id is not None:
            try:
                return self.store.finish_request(request_id, response)
            except Exception:
                pass
        return response

    def _dispatch(self, event: dict[str, Any], manifest: dict[str, Any], *, readonly: bool) -> Any:
        op, data = event["op"], event["data"]
        if op in {"manifest", "status", "checkpoint_get"}:
            _fields(data, set())
            if op == "status":
                return self.store.public_status()
            if op == "checkpoint_get":
                return self.store.load_snapshot()
            return {
                key: manifest.get(key)
                for key in (
                    "repo",
                    "remote_url",
                    "base_sha",
                    "branch",
                    "model",
                    "allowed_paths",
                    "artifact_paths",
                    "active_stage",
                    "deadline",
                    "canary_secret_arn",
                    "canary_sha256",
                    "max_model_calls",
                    "max_model_output_tokens",
                    "max_model_request_bytes",
                    "max_bundle_bytes",
                    "max_snapshot_bytes",
                    "worker_role_arn",
                    "forbidden_value_sha256",
                )
            }
        if op == "model":
            _fields(data, {"body"})
            if event["stage"] in self.store.state()["completed_stages"]:
                raise BrokerError("stage_already_completed")
            body = _model_body(data["body"], manifest)
            self.store.claim_model(event["request_id"], manifest["max_model_calls"])
            return self._model(body, manifest)
        if op in {"push", "pr"}:
            expected = {"repo", "branch", "commit"} | (
                {"base_sha", "bundle_b64"} if op == "push" else {"title", "body"}
            )
            _fields(data, expected)
            if data["repo"] != REPO or data["branch"] != BRANCH:
                raise BrokerError("git_scope_denied")
            _sha(data["commit"])
            if op == "push":
                if data["base_sha"] != manifest["base_sha"]:
                    raise BrokerError("git_base_denied")
                return self.publication.push(data, manifest, readonly=readonly)
            _string(data["title"], 160)
            _string(data["body"], 8192)
            return self.publication.pr(data, readonly=readonly)
        _fields(data, {"snapshot"})
        snapshot = self._snapshot(data["snapshot"], event["stage"], manifest)
        return {"snapshot_sha256": self.store.save_snapshot(snapshot)}

    def _model(self, body: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
        secret = self._secret(self.model_secret_arn)
        if secret.get("model") != manifest["model"]:
            raise BrokerError("model_configuration_invalid")
        base_url = _string(secret.get("base_url"), 2048)
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise BrokerError("model_configuration_invalid")
        api_key = _string(secret.get("api_key"), 8192)
        try:
            started = time.monotonic()
            with self.http.stream(
                "POST",
                base_url.rstrip("/") + "/responses",
                json=body,
                headers={"Authorization": f"Bearer {api_key}", "Accept": "text/event-stream"},
                follow_redirects=False,
                timeout=httpx.Timeout(60, connect=10),
            ) as response:
                if response.status_code != 200:
                    raise BrokerError("model_unavailable")
                if "text/event-stream" not in response.headers.get("content-type", "").lower():
                    raise BrokerError("model_response_invalid")
                raw = bytearray()
                for part in response.iter_bytes():
                    raw.extend(part)
                    if len(raw) > 1024 * 1024 or time.monotonic() - started > 90:
                        raise BrokerError("model_response_invalid")
            text = validate_sse(bytes(raw))
            return {"status": 200, "headers": {"content-type": "text/event-stream"}, "body": text}
        except BrokerError:
            raise
        except Exception as exc:
            raise BrokerError("model_outcome_unknown") from exc

    def _snapshot(self, value: Any, stage: str, manifest: dict[str, Any]) -> dict[str, Any]:
        value = _fields(value, SNAPSHOT)
        if len(canonical(value)) > manifest["max_snapshot_bytes"]:
            raise BrokerError("snapshot_too_large")
        if (
            type(value["schema_version"]) is not int
            or value["schema_version"] != 1
            or value["stage"] != stage
            or value["base_sha"] != manifest["base_sha"]
            or value["branch"] != BRANCH
        ):
            raise BrokerError("snapshot_scope_denied")
        _string(value["boot_id"], 128)
        commit = _sha(value["head_sha"])
        if self.store.public_status()["commit"] != commit:
            raise BrokerError("snapshot_head_mismatch")
        if (
            not isinstance(value["messages"], list)
            or not isinstance(value["metrics"], dict)
            or not isinstance(value["result"], dict)
        ):
            raise BrokerError("snapshot_invalid")
        for message in value["messages"]:
            if not isinstance(message, dict) or message.get("role") not in {
                "user",
                "assistant",
                "tool_result",
            }:
                raise BrokerError("snapshot_messages_invalid")
        if not isinstance(value["untracked"], dict) or set(value["untracked"]) - {
            "continuation.md"
        }:
            raise BrokerError("artifact_path_denied")
        for content in value["untracked"].values():
            try:
                unbase64(content, 65536).decode("utf-8")
            except UnicodeError as exc:
                raise BrokerError("artifact_invalid") from exc
        bundle = unbase64(value["git_bundle_b64"], manifest["max_bundle_bytes"])
        patch = unbase64(value["patch_b64"], 65536)
        with self.publication.validate_bundle(bundle, commit, manifest) as directory:
            self.publication.validate_patch(directory, patch, commit)
        return value


_instance: Broker | None = None


def lambda_handler(event: Any, context: Any) -> dict[str, Any]:
    """Admission is Lambda Invoke IAM, never an event-supplied identity claim."""
    del context
    global _instance
    try:
        if _instance is None:
            task_id = os.environ["TASK_ID"]
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", task_id):
                raise BrokerError("task_configuration_invalid")
            config = Config(retries={"total_max_attempts": 1}, connect_timeout=5, read_timeout=15)
            session = boto3.Session()
            store = TaskStore(
                session.client("s3", config=config), os.environ["TASK_BUCKET"], task_id
            )
            _instance = Broker(
                store=store,
                task_id=task_id,
                secrets=session.client("secretsmanager", config=config),
                model_secret_arn=os.environ["MODEL_SECRET_ARN"],
                github_secret_arn=os.environ["GITHUB_SECRET_ARN"],
                http=httpx.Client(trust_env=False, follow_redirects=False),
            )
        return _instance.handle(event)
    except Exception:
        return {"ok": False, "error": {"code": "broker_unavailable"}}
