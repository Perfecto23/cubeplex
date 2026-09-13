from __future__ import annotations

import base64
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

from cubeplex_git_slice.git_publication import BRANCH, REMOTE, REPO, GitPublication
from cubeplex_git_slice.state import BrokerError
from test_broker import manifest
from test_broker_state import make_store


def git(path: Path, *args: str) -> bytes:
    return subprocess.check_output(
        ["git", "-c", "core.hooksPath=/dev/null", "-C", str(path), *args],
        stderr=subprocess.DEVNULL,
    )


@pytest.fixture
def repository(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "fixture"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Broker test")
    git(repo, "config", "user.email", "broker@example.invalid")
    (repo / "intervals.py").write_text("def total(a, b):\n    return b - a\n")
    (repo / "README.md").write_text("Fixture\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "fixture base")
    return repo, git(repo, "rev-parse", "HEAD").decode().strip()


def bundle(
    repository: tuple[Path, str],
    *,
    changed: str = "intervals.py",
    second: bool = False,
    symlink: bool = False,
) -> tuple[bytes, str, dict[str, Any]]:
    repo, base = repository
    target = repo / changed
    if symlink:
        target.unlink()
        target.symlink_to("/etc/passwd")
    else:
        target.write_text("def total(a, b):\n    return b - a + 1\n")
    git(repo, "add", changed)
    git(repo, "commit", "-m", "agent-created fix")
    if second:
        target.write_text("def total(a, b):\n    return 1 + b - a\n")
        git(repo, "commit", "-am", "unapproved second commit")
    head = git(repo, "rev-parse", "HEAD").decode().strip()
    output = repo.parent / "submission.bundle"
    git(repo, "bundle", "create", str(output), f"{base}..HEAD")
    constraints = {**manifest(), "base_sha": base}
    return output.read_bytes(), head, constraints


class LocalFixturePublication(GitPublication):
    def __init__(self, repo: Path) -> None:
        super().__init__(make_store(), lambda: "never-needed", httpx.Client())
        self.repo = repo
        self.auth_calls = 0

    def _git(self, directory: Path, args: list[str], **kwargs: Any) -> bytes:
        if kwargs.get("auth"):
            self.auth_calls += 1
        # Only substitute the public Git network origin; real bare Git object
        # verification/diff/apply commands run unchanged.
        args = [str(self.repo) if arg == REMOTE else arg for arg in args]
        return super()._git(directory, args, **kwargs)


def test_real_bundle_validation_and_readme_patch_never_execute_repository(
    repository: tuple[Path, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw, head, constraints = bundle(repository)
    publication = LocalFixturePublication(repository[0])
    hook_dir = tmp_path / "hooks"
    hook_dir.mkdir()
    marker = tmp_path / "executed"
    hook = hook_dir / "post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    hook.chmod(0o700)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(hook_dir))
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", str(hook))
    (repository[0] / "README.md").write_text("Fixture\nContinuation handoff\n")
    patch = git(repository[0], "diff", "--no-ext-diff", "--no-textconv", "--", "README.md")
    with publication.validate_bundle(raw, head, constraints) as bare:
        assert publication._git(bare, ["rev-parse", "--is-bare-repository"]).strip() == b"true"
        assert not (bare / "intervals.py").exists()
        publication.validate_patch(bare, patch, head)
        bad = patch.replace(b"README.md", b"intervals.py")
        with pytest.raises(BrokerError):
            publication.validate_patch(bare, bad, head)
    assert not marker.exists()
    assert publication.auth_calls == 0


@pytest.mark.parametrize(
    "change,second,symlink,code",
    [
        ("README.md", False, False, "changed_path_denied"),
        ("intervals.py", True, False, "commit_parent_denied"),
        ("intervals.py", False, True, "file_mode_denied"),
    ],
)
def test_real_git_rejects_other_paths_second_commit_and_symlink(
    repository: tuple[Path, str], change: str, second: bool, symlink: bool, code: str
) -> None:
    raw, head, constraints = bundle(repository, changed=change, second=second, symlink=symlink)
    publication = LocalFixturePublication(repository[0])
    with (
        pytest.raises(BrokerError, match=code),
        publication.validate_bundle(raw, head, constraints),
    ):
        pass
    assert publication.auth_calls == 0


class PublicationBoundary(GitPublication):
    def __init__(self, *, accepted: bool) -> None:
        super().__init__(make_store(), lambda: "fake-secret", httpx.Client())
        self.head: str | None = None
        self.accepted = accepted
        self.pushes = 0
        self.prs = 0
        self.existing_pr: dict[str, Any] | None = None

    @contextmanager
    def validate_bundle(self, *args: Any):
        yield Path("/unused")

    def remote_head(self) -> str | None:
        return self.head

    def _git(self, directory: Path, args: list[str], **kwargs: Any) -> bytes:
        assert args[0] == "push" and args[1] == REMOTE
        assert args[2] == f"{'b' * 40}:refs/heads/{BRANCH}"
        assert kwargs.get("auth") is True
        self.pushes += 1
        if self.accepted:
            self.head = "b" * 40
        raise BrokerError("git_operation_failed")

    def _find_pr(self, commit: str) -> dict[str, Any] | None:
        return self.existing_pr

    def _api(self, method: str, path: str, body: Any = None) -> Any:
        assert method == "POST" and path == f"/repos/{REPO}/pulls"
        assert body["base"] == "main" and body["head"] == BRANCH
        self.prs += 1
        if self.accepted:
            self.existing_pr = {"number": 1, "url": f"https://github.com/{REPO}/pull/1"}
        raise BrokerError("github_unavailable")


def push_data() -> dict[str, Any]:
    return {
        "repo": REPO,
        "branch": BRANCH,
        "base_sha": "a" * 40,
        "commit": "b" * 40,
        "bundle_b64": base64.b64encode(b"test-only").decode(),
    }


def test_push_and_pr_accepted_then_disconnected_are_resolved_only_by_readback() -> None:
    publication = PublicationBoundary(accepted=True)
    assert publication.push(push_data(), manifest())["status"] == "pushed"
    assert publication.push(push_data(), manifest())["status"] == "already_pushed"
    assert publication.push(push_data(), manifest(), readonly=True)["status"] == "already_pushed"
    data = {"commit": "b" * 40, "title": "fix", "body": "test"}
    assert publication.pr(data)["status"] == "created"
    assert publication.pr(data)["status"] == "already_exists"
    assert publication.pr(data, readonly=True)["status"] == "already_exists"
    assert publication.pushes == 1 and publication.prs == 1


def test_unknown_side_effect_is_never_retried_even_with_new_request_id() -> None:
    publication = PublicationBoundary(accepted=False)
    for readonly in (False, False, True):
        with pytest.raises(BrokerError, match="push_outcome_unknown"):
            publication.push(push_data(), manifest(), readonly=readonly)
    assert publication.pushes == 1
    publication.head = "b" * 40
    publication.push(push_data(), manifest(), readonly=True)
    data = {"commit": "b" * 40, "title": "fix", "body": "test"}
    for readonly in (False, False, True):
        with pytest.raises(BrokerError, match="pr_outcome_unknown"):
            publication.pr(data, readonly=readonly)
    assert publication.prs == 1


def test_github_api_target_headers_and_safe_error_boundary() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(500, text="private-token-in-untrusted-server-error")

    publication = GitPublication(
        make_store(), lambda: "private-token", httpx.Client(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(BrokerError, match="github_scope_denied"):
        publication._api("POST", "/repos/another/repo/pulls")
    assert not seen
    with pytest.raises(BrokerError) as raised:
        publication.remote_head()
    assert str(raised.value) == "github_unavailable"
    assert seen[0].url.host == "api.github.com"
    assert "private-token" not in str(seen[0].url)


def test_git_askpass_never_places_secret_in_argv_url_or_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    def run(command: list[str], **kwargs: Any) -> Any:
        captured.update(command=command, env=kwargs["env"])
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-enter-git")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "99")
    publication = GitPublication(make_store(), lambda: "private-token", httpx.Client())
    publication._git(tmp_path, ["push", REMOTE, f"{'b' * 40}:refs/heads/{BRANCH}"], auth=True)
    assert "private-token" not in str(captured["command"])
    assert "AWS_SECRET_ACCESS_KEY" not in captured["env"]
    assert "GIT_CONFIG_COUNT" not in captured["env"]
    assert captured["env"]["BROKER_GIT_PASSWORD"] == "private-token"
    script = Path(captured["env"]["GIT_ASKPASS"])
    assert "private-token" not in script.read_text()
    assert script.stat().st_mode & 0o777 == 0o700
