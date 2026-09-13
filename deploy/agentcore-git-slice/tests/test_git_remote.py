from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from cubeplex_git_slice import git_remote


def test_remote_helper_capabilities_and_list(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        git_remote,
        "_config",
        lambda: {"branch": "agentcore/fix-inclusive-total"},
    )
    monkeypatch.setattr("sys.stdin", io.StringIO("capabilities\nlist\nquit\n"))

    assert git_remote.main() == 0
    output = capsys.readouterr().out
    assert "push\n" in output
    assert "? refs/heads/agentcore/fix-inclusive-total\n" in output


def _git(repo: Path, *args: str) -> str:
    return (
        subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.DEVNULL)
        .decode()
        .strip()
    )


def test_real_git_push_finishes_remote_helper_batch(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "test")
    _git(repo, "config", "user.email", "test@example.invalid")
    (repo / "intervals.py").write_text("value = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "intervals.py").write_text("value = 2\n")
    _git(repo, "commit", "-am", "agent fix")
    head = _git(repo, "rev-parse", "HEAD")
    branch = "agentcore/fix-inclusive-total"
    _git(repo, "remote", "add", "origin", "broker::local-fixture")
    config = tmp_path / "task.json"
    config.write_text(json.dumps({"repo": "fixture/repo", "branch": branch, "base_sha": base}))
    record = tmp_path / "broker-call.json"
    helper = tmp_path / "git-remote-broker"
    helper.write_text(
        f"#!{sys.executable}\n"
        "import json,sys\nfrom pathlib import Path\n"
        f"sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})\n"
        "from cubeplex_git_slice import git_remote\n"
        "class Broker:\n"
        " async def push(self,data):\n"
        f"  Path({str(record)!r}).write_text(json.dumps(data))\n"
        "  return {'commit':data['commit'],'status':'pushed'}\n"
        "git_remote._client=lambda config: Broker()\n"
        "raise SystemExit(git_remote.main())\n"
    )
    helper.chmod(0o700)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "CUBEPLEX_GIT_TASK_CONFIG": str(config),
    }
    process = subprocess.Popen(
        ["git", "push", "origin", f"HEAD:refs/heads/{branch}"],
        cwd=repo,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        _, stderr = process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        pytest.fail("real git push waited forever for the helper batch terminator")
    assert process.returncode == 0, stderr.decode()
    body = json.loads(record.read_text())
    assert body["commit"] == head and body["base_sha"] == base
    assert body["branch"] == branch and body["bundle_b64"]
    assert _git(repo, "for-each-ref", "refs/agentcore-bundle/") == ""
    for extra, destination in ((["--force"], f"refs/heads/{branch}"), ([], "refs/heads/main")):
        record.unlink(missing_ok=True)
        rejected = subprocess.run(
            ["git", "push", *extra, "origin", f"HEAD:{destination}"],
            cwd=repo,
            env=env,
            capture_output=True,
            timeout=5,
        )
        assert rejected.returncode != 0
        assert not record.exists()


@pytest.mark.parametrize(
    "source,destination",
    [
        ("HEAD", "refs/heads/main"),
        ("+HEAD", "refs/heads/agentcore/fix-inclusive-total"),
        ("", "refs/heads/agentcore/fix-inclusive-total"),
    ],
)
async def test_push_rejects_destination_force_and_delete_before_broker(
    source: str, destination: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(git_remote, "_client", lambda _: calls.append("broker"))
    monkeypatch.setattr(git_remote, "_git", lambda *_: "b" * 40)
    with pytest.raises(RuntimeError, match="push_refspec_denied"):
        await git_remote._push(
            tmp_path, {"branch": "agentcore/fix-inclusive-total"}, source, destination
        )
    assert not calls


def test_helper_rejects_multi_ref_or_unterminated_batch_without_publishing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from unittest.mock import AsyncMock

    push = AsyncMock()
    monkeypatch.setattr(git_remote, "_push", push)
    monkeypatch.setattr(git_remote, "_config", lambda: {"branch": "agentcore/fix-inclusive-total"})
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            "push HEAD:refs/heads/agentcore/fix-inclusive-total\npush HEAD:refs/heads/main\n\n"
        ),
    )
    assert git_remote.main() == 0
    output = capsys.readouterr().out
    assert output.endswith("\n\n")
    assert output.count("error ") == 2
    push.assert_not_awaited()
    monkeypatch.setattr(
        "sys.stdin", io.StringIO("push HEAD:refs/heads/agentcore/fix-inclusive-total\n")
    )
    assert git_remote.main() == 1
    assert capsys.readouterr().out == ""
    push.assert_not_awaited()
