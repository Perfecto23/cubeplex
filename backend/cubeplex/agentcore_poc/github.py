"""Read-only GitHub access pinned to one authorized repository and commit."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from typing import Any
from urllib.parse import quote

import httpx
from cubeloop.agent.types import AgentTool, AgentToolResult
from cubeloop.providers.base import TextContent
from pydantic import Field, field_validator

from cubeplex.agentcore_poc.contracts import Evidence, StrictModel

REPOSITORY = "Perfecto23/corplink-rs"
MAX_FILE_BYTES = 128_000
MAX_RESPONSE_BYTES = 4_000_000
_SHA = re.compile(r"^[a-f0-9]{40}$")


class RepositoryReadError(RuntimeError):
    """Safe error codes only; never upstream response bodies or credentials."""


def validate_path(value: str, *, allow_empty: bool = False) -> str:
    if value == "" and allow_empty:
        return value
    if (
        not value
        or len(value) > 500
        or value.startswith("/")
        or any(part in ("", ".", "..") for part in value.split("/"))
        or any(char in value for char in ("\\", ":", "%", "\x00", "\n", "\r"))
    ):
        raise ValueError("invalid_repository_path")
    return value


class ListFilesInput(StrictModel):
    prefix: str = Field(default="", max_length=500)

    @field_validator("prefix")
    @classmethod
    def safe_prefix(cls, value: str) -> str:
        return validate_path(value, allow_empty=True)


class ReadFileInput(StrictModel):
    path: str
    start_line: int = Field(default=1, ge=1, le=100_000)
    max_lines: int = Field(default=100, ge=1, le=120)

    @field_validator("path")
    @classmethod
    def safe_path(cls, value: str) -> str:
        return validate_path(value)


class GitHubReader:
    def __init__(self, client: httpx.AsyncClient, repository: str = REPOSITORY) -> None:
        if repository != REPOSITORY:
            raise RepositoryReadError("repository_denied")
        self.client = client
        self.repository = repository
        self.commit_sha: str | None = None
        self.default_branch: str | None = None
        self.files: dict[str, dict[str, Any]] = {}
        self.evidence: list[Evidence] = []
        self._tool_calls = 0
        self.budget_exhausted = False

    async def _get(self, suffix: str) -> dict[str, Any]:
        url = f"https://api.github.com/repos/{self.repository}"
        if suffix:
            url += f"/{suffix}"
        try:
            async with self.client.stream("GET", url, follow_redirects=False) as response:
                if response.status_code != 200:
                    raise RepositoryReadError("github_read_failed")
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise RepositoryReadError("github_response_too_large")
                    chunks.append(chunk)
            data = json.loads(b"".join(chunks))
        except (httpx.HTTPError, ValueError) as exc:
            raise RepositoryReadError("github_read_failed") from exc
        if not isinstance(data, dict):
            raise RepositoryReadError("github_invalid_response")
        return data

    async def initialize(self) -> None:
        metadata = await self._get("")
        if metadata.get("full_name") != self.repository or metadata.get("private") is not False:
            raise RepositoryReadError("repository_metadata_mismatch")
        branch = metadata.get("default_branch")
        if not isinstance(branch, str) or not branch:
            raise RepositoryReadError("github_invalid_branch")
        self.default_branch = branch
        commit = await self._get(f"commits/{quote(branch, safe='')}")
        sha = commit.get("sha")
        if not isinstance(sha, str) or not _SHA.fullmatch(sha):
            raise RepositoryReadError("github_invalid_commit")
        self.commit_sha = sha
        tree = await self._get(f"git/trees/{sha}?recursive=1")
        if tree.get("truncated") is not False or not isinstance(tree.get("tree"), list):
            raise RepositoryReadError("github_incomplete_tree")
        for item in tree["tree"]:
            if not isinstance(item, dict) or item.get("type") != "blob":
                continue
            path, blob_sha = item.get("path"), item.get("sha")
            if not isinstance(path, str) or not isinstance(blob_sha, str):
                raise RepositoryReadError("github_invalid_tree")
            if not _SHA.fullmatch(blob_sha):
                raise RepositoryReadError("github_invalid_tree")
            if item.get("mode") not in ("100644", "100755"):
                continue
            self.files[path] = item

    def _consume_call(self) -> None:
        self._tool_calls += 1
        if self._tool_calls > 8:
            self.budget_exhausted = True
            raise RepositoryReadError("tool_budget_exhausted")
        if self.commit_sha is None:
            raise RepositoryReadError("repository_not_initialized")

    async def list_files(self, args: ListFilesInput) -> dict[str, Any]:
        self._consume_call()
        paths = sorted(path for path in self.files if path.startswith(args.prefix))
        return {"commit_sha": self.commit_sha, "paths": paths[:300], "truncated": len(paths) > 300}

    async def read_file(self, args: ReadFileInput) -> Evidence:
        self._consume_call()
        entry = self.files.get(args.path)
        if entry is None:
            raise RepositoryReadError("file_not_in_pinned_tree")
        size = entry.get("size")
        if not isinstance(size, int) or size < 0 or size > MAX_FILE_BYTES:
            raise RepositoryReadError("file_too_large")
        blob = await self._get(f"git/blobs/{entry['sha']}")
        if blob.get("encoding") != "base64" or not isinstance(blob.get("content"), str):
            raise RepositoryReadError("unsupported_blob")
        try:
            content = base64.b64decode("".join(blob["content"].split()), validate=True)
            if len(content) > MAX_FILE_BYTES:
                raise RepositoryReadError("file_too_large")
            actual_sha = hashlib.sha1(
                f"blob {len(content)}\0".encode() + content, usedforsecurity=False
            ).hexdigest()
            if actual_sha != entry["sha"] or blob.get("sha") != actual_sha:
                raise RepositoryReadError("blob_sha_mismatch")
            lines = content.decode("utf-8").splitlines()
        except (ValueError, UnicodeError) as exc:
            raise RepositoryReadError("unsupported_blob") from exc
        if args.start_line > len(lines):
            raise RepositoryReadError("line_out_of_range")
        end_line = min(args.start_line + args.max_lines - 1, len(lines))
        selected = lines[args.start_line - 1 : end_line]
        if sum(len(line) for line in selected) > 20_000:
            raise RepositoryReadError("excerpt_too_large")
        evidence = Evidence(
            path=args.path,
            start_line=args.start_line,
            end_line=end_line,
            blob_sha=actual_sha,
            excerpt="\n".join(selected),
        )
        self.evidence.append(evidence)
        return evidence

    def tools(self) -> list[AgentTool[Any]]:
        async def list_execute(
            tool_call_id: str,
            args: ListFilesInput,
            *,
            signal: object = None,
            on_update: object = None,
        ) -> AgentToolResult:
            del tool_call_id, signal, on_update
            try:
                result = await self.list_files(args)
                return AgentToolResult(content=[TextContent(text=json.dumps(result))])
            except RepositoryReadError as exc:
                return self._error_result(exc)

        async def read_execute(
            tool_call_id: str,
            args: ReadFileInput,
            *,
            signal: object = None,
            on_update: object = None,
        ) -> AgentToolResult:
            del tool_call_id, signal, on_update
            try:
                result = await self.read_file(args)
                return AgentToolResult(content=[TextContent(text=result.model_dump_json())])
            except RepositoryReadError as exc:
                return self._error_result(exc)

        return [
            AgentTool(
                name="list_repository_files",
                description="List paths in the authorized repository at the run's pinned commit.",
                parameters=ListFilesInput,
                execute=list_execute,
                execution_mode="sequential",
            ),
            AgentTool(
                name="read_repository_file",
                description="Read a UTF-8 file excerpt from the pinned commit; returns line evidence.",
                parameters=ReadFileInput,
                execute=read_execute,
                execution_mode="sequential",
            ),
        ]

    def _error_result(self, exc: RepositoryReadError) -> AgentToolResult:
        return AgentToolResult(
            content=[TextContent(text=str(exc))],
            is_error=True,
            terminate=self.budget_exhausted,
        )
