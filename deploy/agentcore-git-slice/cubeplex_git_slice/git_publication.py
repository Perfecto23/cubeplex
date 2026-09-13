"""Validate Git objects and relay a single commit; never check out or execute a repository."""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from .state import BrokerError, TaskStore

REPO = "Perfecto23/cubeplex-microvm-git-poc-20260913"
BRANCH = "agentcore/fix-inclusive-total"
REMOTE = f"https://github.com/{REPO}.git"
SHA = re.compile(r"[0-9a-f]{40}\Z")


def unbase64(value: Any, maximum: int) -> bytes:
    if not isinstance(value, str) or len(value) > ((maximum + 2) // 3) * 4:
        raise BrokerError("payload_too_large")
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise BrokerError("invalid_base64") from exc
    if len(raw) > maximum:
        raise BrokerError("payload_too_large")
    return raw


def _limits() -> None:
    # The untrusted pack may compress much larger objects than its wire size.
    import resource

    resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_CPU, (25, 25))
    if sys.platform == "linux":
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024,) * 2)


class GitPublication:
    def __init__(
        self,
        store: TaskStore,
        token: Callable[[], str],
        http: httpx.Client,
    ) -> None:
        self.store, self.token, self.http = store, token, http

    def _api(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        if method not in {"GET", "POST"} or not path.startswith(f"/repos/{REPO}/"):
            raise BrokerError("github_scope_denied")
        try:
            with self.http.stream(
                method,
                "https://api.github.com" + path,
                headers={
                    "Authorization": f"Bearer {self.token()}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                json=body,
                follow_redirects=False,
                timeout=20,
            ) as response:
                if response.status_code == 404 and method == "GET":
                    return None
                if response.status_code not in {200, 201}:
                    raise BrokerError("github_unavailable")
                raw = bytearray()
                for part in response.iter_bytes():
                    raw.extend(part)
                    if len(raw) > 1024 * 1024:
                        raise BrokerError("github_unavailable")
                return json.loads(raw)
        except BrokerError:
            raise
        except Exception as exc:
            raise BrokerError("github_unavailable") from exc

    def remote_head(self) -> str | None:
        value = self._api("GET", f"/repos/{REPO}/git/ref/heads/{quote(BRANCH, safe='')}")
        if value is None:
            return None
        commit = value.get("object", {}).get("sha") if isinstance(value, dict) else None
        if not isinstance(commit, str) or not SHA.fullmatch(commit):
            raise BrokerError("github_unavailable")
        return commit

    def _git(
        self, directory: Path, args: list[str], *, data: bytes | None = None, auth: bool = False
    ) -> bytes:
        # Do not inherit AWS credentials, injected Git configuration, SSH
        # commands, alternate object paths, credential helpers or user hooks.
        env = {
            "PATH": os.defpath,
            "HOME": str(directory),
            "LANG": "C.UTF-8",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ALLOW_PROTOCOL": "https:file",
        }
        if auth:
            askpass = directory / "broker-askpass.py"
            askpass.write_text(
                f"#!{sys.executable}\n"
                "import os,sys\n"
                "print('x-access-token' if 'username' in sys.argv[1].lower() "
                "else os.environ['BROKER_GIT_PASSWORD'])\n"
            )
            askpass.chmod(0o700)
            env.update(GIT_ASKPASS=str(askpass), BROKER_GIT_PASSWORD=self.token())
        command = [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "credential.helper=",
            "-c",
            "http.followRedirects=false",
            "-c",
            "protocol.ext.allow=never",
            "-c",
            "core.attributesFile=/dev/null",
            "-C",
            str(directory),
            *args,
        ]
        try:
            with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
                result = subprocess.run(
                    command,
                    input=data,
                    stdout=output,
                    stderr=errors,
                    env=env,
                    timeout=35,
                    check=False,
                    preexec_fn=_limits,
                )
                output.seek(0)
                captured = output.read(1024 * 1024 + 1)
        except Exception as exc:
            raise BrokerError("git_operation_failed") from exc
        if result.returncode or len(captured) > 1024 * 1024:
            raise BrokerError("git_operation_failed")
        return captured

    @contextmanager
    def validate_bundle(
        self, bundle: bytes, commit: str, manifest: dict[str, Any]
    ) -> Iterator[Path]:
        if not SHA.fullmatch(commit):
            raise BrokerError("invalid_commit")
        with tempfile.TemporaryDirectory(prefix="cubeplex-broker-") as temporary:
            directory = Path(temporary)
            self._git(directory, ["init", "--bare", "."])
            self._git(directory, ["fetch", "--no-tags", "--depth=1", REMOTE, manifest["base_sha"]])
            bundle_path = directory / "submission.bundle"
            bundle_path.write_bytes(bundle)
            self._git(directory, ["bundle", "verify", str(bundle_path)])
            self._git(directory, ["fetch", "--no-tags", str(bundle_path), commit])
            if int(self._git(directory, ["cat-file", "-s", commit])) > 65536:
                raise BrokerError("commit_too_large")
            raw_commit = self._git(directory, ["cat-file", "-p", commit])
            header = raw_commit.split(b"\n\n", 1)[0]
            parents = [
                line[7:].decode("ascii")
                for line in header.splitlines()
                if line.startswith(b"parent ")
            ]
            if parents != [manifest["base_sha"]]:
                raise BrokerError("commit_parent_denied")
            changed = self._git(
                directory,
                [
                    "diff",
                    "--no-ext-diff",
                    "--no-textconv",
                    "--no-renames",
                    "--name-only",
                    "-z",
                    manifest["base_sha"],
                    commit,
                    "--",
                ],
            ).split(b"\0")
            if changed != [b"intervals.py", b""]:
                raise BrokerError("changed_path_denied")
            for ref in (manifest["base_sha"], commit):
                tree = self._git(directory, ["ls-tree", ref, "--", "intervals.py"])
                if not tree.startswith(b"100644 blob "):
                    raise BrokerError("file_mode_denied")
                if (
                    int(self._git(directory, ["cat-file", "-s", f"{ref}:intervals.py"]))
                    > manifest["max_changed_bytes"]
                ):
                    raise BrokerError("changed_file_too_large")
                content = self._git(
                    directory, ["show", "--no-ext-diff", "--no-textconv", f"{ref}:intervals.py"]
                )
                if len(content) > manifest["max_changed_bytes"] or b"\0" in content:
                    raise BrokerError("changed_file_too_large")
                try:
                    content.decode("utf-8")
                except UnicodeError as exc:
                    raise BrokerError("changed_file_invalid") from exc
            yield directory

    def validate_patch(self, directory: Path, patch: bytes, commit: str) -> None:
        if not patch:
            return
        forbidden = (
            b"old mode ",
            b"new mode ",
            b"new file mode ",
            b"deleted file mode ",
            b"rename ",
            b"copy ",
            b"GIT binary patch",
        )
        if any(line.startswith(forbidden) for line in patch.splitlines()):
            raise BrokerError("artifact_patch_denied")
        paths = self._git(directory, ["apply", "--numstat", "-z", "-"], data=patch).split(b"\0")
        if len(paths) != 2 or paths[0].split(b"\t")[-1] != b"README.md" or paths[1]:
            raise BrokerError("artifact_patch_denied")
        self._git(directory, ["read-tree", commit])
        self._git(
            directory, ["apply", "--cached", "--check", "--whitespace=nowarn", "-"], data=patch
        )

    def push(
        self, data: dict[str, Any], manifest: dict[str, Any], *, readonly: bool = False
    ) -> dict[str, Any]:
        commit = data["commit"]
        if readonly:
            if self.store.state().get("commit") != commit:
                raise BrokerError("push_outcome_unknown")
            if self.remote_head() == commit:
                self.store.finish_push(commit)
                return {"commit": commit, "status": "already_pushed"}
            raise BrokerError("push_outcome_unknown")
        bundle = unbase64(data["bundle_b64"], manifest["max_bundle_bytes"])
        with self.validate_bundle(bundle, commit, manifest) as directory:
            owner = self.store.reserve_push(commit)
            current = self.remote_head()
            if current == commit:
                self.store.finish_push(commit)
                return {"commit": commit, "status": "already_pushed"}
            if current is not None:
                raise BrokerError("remote_branch_conflict")
            if not owner:
                raise BrokerError("push_outcome_unknown")
            try:
                self._git(directory, ["push", REMOTE, f"{commit}:refs/heads/{BRANCH}"], auth=True)
            except BrokerError:
                # The server may have accepted the write before disconnection.
                # Only a readback can resolve it; no second push is permitted.
                pass
            try:
                confirmed = self.remote_head()
            except BrokerError as exc:
                raise BrokerError("push_outcome_unknown") from exc
            if confirmed != commit:
                raise BrokerError("push_outcome_unknown")
            self.store.finish_push(commit)
            return {"commit": commit, "status": "pushed"}

    def _find_pr(self, commit: str) -> dict[str, Any] | None:
        owner = REPO.split("/")[0]
        path = f"/repos/{REPO}/pulls?state=all&head={quote(owner + ':' + BRANCH, safe='')}&base=main&per_page=100"
        rows = self._api("GET", path)
        if not isinstance(rows, list):
            raise BrokerError("github_unavailable")
        for row in rows:
            if (
                row.get("head", {}).get("sha") == commit
                and row.get("head", {}).get("ref") == BRANCH
                and row.get("head", {}).get("repo", {}).get("full_name") == REPO
                and row.get("base", {}).get("ref") == "main"
            ):
                number = row.get("number")
                if type(number) is not int or number < 1:
                    raise BrokerError("github_unavailable")
                return {"number": number, "url": f"https://github.com/{REPO}/pull/{number}"}
        if len(rows) >= 100:
            raise BrokerError("pr_readback_ambiguous")
        return None

    def pr(self, data: dict[str, Any], *, readonly: bool = False) -> dict[str, Any]:
        commit = data["commit"]
        state = self.store.state()
        if state.get("commit") != commit or state.get("push_phase") != "pushed":
            raise BrokerError("commit_not_pushed")
        if self.remote_head() != commit:
            raise BrokerError("remote_branch_conflict")
        existing = self._find_pr(commit)
        if existing is not None:
            self.store.finish_pr(existing)
            return {**existing, "status": "already_exists"}
        if readonly or not self.store.reserve_pr(commit):
            raise BrokerError("pr_outcome_unknown")
        try:
            self._api(
                "POST",
                f"/repos/{REPO}/pulls",
                {
                    "head": BRANCH,
                    "base": "main",
                    "title": data["title"],
                    "body": data["body"],
                },
            )
        except BrokerError:
            pass
        try:
            confirmed = self._find_pr(commit)
        except BrokerError as exc:
            raise BrokerError("pr_outcome_unknown") from exc
        if confirmed is None:
            raise BrokerError("pr_outcome_unknown")
        self.store.finish_pr(confirmed)
        return {**confirmed, "status": "created"}
