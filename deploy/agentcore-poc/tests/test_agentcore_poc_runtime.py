"""PoC trust boundaries: scope, pinned source, and provider completion before tool execution."""

from __future__ import annotations

import base64
import hashlib
import json

import httpx
import pytest
from bedrock_agentcore.runtime.context import RequestContext
from cubeloop.providers.base import AssistantMessage, ToolCall
from cubeplex.agentcore_poc.agent import (
    CompletionGuard,
    build_model,
    normalized_base_url,
)
from cubeplex.agentcore_poc.contracts import (
    InvocationRequest,
    ProviderSecret,
    RuntimeScope,
    ScopeDenied,
    derive_runtime_session_id,
)
from cubeplex.agentcore_poc.github import (
    GitHubReader,
    ListFilesInput,
    ReadFileInput,
    RepositoryReadError,
)
from cubeplex.agentcore_poc.runtime import invoke
from cubeplex.agents.graph import create_cubeplex_agent
from pydantic import ValidationError

REQUEST = {
    "schema_version": "1",
    "run_id": "a98cbf6c-4e5e-4e95-9370-a3768a544a46",
    "team_id": "T011CF3CMJN",
    "channel_id": "C0BKH447YGN",
    "thread_ts": "1789181400.000001",
    "user_id": "U09USS444UE",
    "prompt": "Explain the repository using source evidence.",
    "repository": "Perfecto23/corplink-rs",
}
COMMIT = "a" * 40
CONTENT = b"first line\nsecond line\nthird line\n"
BLOB = hashlib.sha1(f"blob {len(CONTENT)}\0".encode() + CONTENT).hexdigest()


def scope_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in {
        "TEAM_ID": REQUEST["team_id"],
        "CHANNEL_ID": REQUEST["channel_id"],
        "USER_IDS": REQUEST["user_id"],
        "REPOSITORY": REQUEST["repository"],
    }.items():
        monkeypatch.setenv(f"CUBEPLEX_POC_ALLOWED_{key}", value)


@pytest.mark.parametrize("field", ["team_id", "channel_id", "user_id", "repository"])
def test_scope_rejects_each_cross_scope_field(field: str) -> None:
    scope = RuntimeScope(
        team_id=REQUEST["team_id"],
        channel_id=REQUEST["channel_id"],
        user_ids=frozenset({REQUEST["user_id"]}),
    )
    replacement = {
        "team_id": "T9999999999",
        "channel_id": "C9999999999",
        "user_id": "U9999999999",
        "repository": "other/private",
    }
    with pytest.raises(ScopeDenied):
        scope.authorize(
            InvocationRequest.model_validate({**REQUEST, field: replacement[field]})
        )


def test_payload_rejects_unknown_fields_and_types() -> None:
    for update in (
        {"command": "curl attacker.test"},
        {"provider_api_key": "test"},
        {"prompt": 123},
    ):
        with pytest.raises(ValidationError):
            InvocationRequest.model_validate({**REQUEST, **update})


def test_session_is_scope_bound_but_stable_across_runs() -> None:
    session = derive_runtime_session_id(REQUEST)
    assert len(session) >= 33
    assert session == derive_runtime_session_id({**REQUEST, "run_id": "b" * 32})
    for field, replacement in {
        "user_id": "U9999999999",
        "thread_ts": "1789181400.000002",
        "repository": "other/repo",
    }.items():
        assert session != derive_runtime_session_id({**REQUEST, field: replacement})


@pytest.mark.asyncio
async def test_scope_and_session_reject_before_secret_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope_env(monkeypatch)
    touched = False

    def load_secret() -> None:
        nonlocal touched
        touched = True
        raise AssertionError("must not access credentials")

    monkeypatch.setattr(
        "cubeplex.agentcore_poc.runtime.load_provider_secret", load_secret
    )
    for payload, session in [
        (
            {**REQUEST, "repository": "other/private"},
            derive_runtime_session_id(REQUEST),
        ),
        (REQUEST, "wrong-session"),
        (
            {**REQUEST, "url": "https://attacker.test"},
            derive_runtime_session_id(REQUEST),
        ),
    ]:
        response = await invoke(payload, RequestContext(session_id=session))
        assert response["status"] in ("denied", "invalid_request")
        assert not response["answer"]
    assert not touched


@pytest.mark.asyncio
async def test_errors_do_not_echo_secret_or_exception_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope_env(monkeypatch)

    def load_secret() -> None:
        raise RuntimeError("api_key=secret-must-not-leak; upstream body")

    monkeypatch.setattr(
        "cubeplex.agentcore_poc.runtime.load_provider_secret", load_secret
    )
    response = await invoke(
        REQUEST, RequestContext(session_id=derive_runtime_session_id(REQUEST))
    )
    assert response["status"] == "internal_error"
    assert "secret-must-not-leak" not in json.dumps(response)
    assert response["error_code"] == "runtime_failed"


@pytest.mark.parametrize(
    "path",
    [
        "../secret",
        "/etc/passwd",
        "https://attacker.test",
        "x/../y",
        "a%2fb",
        "a\\b",
        "a\nb",
    ],
)
def test_tool_arguments_reject_path_escape(path: str) -> None:
    with pytest.raises(ValidationError):
        ReadFileInput(path=path)


def github_transport(request: httpx.Request) -> httpx.Response:
    assert request.method == "GET"
    assert request.url.host == "api.github.com"
    path = request.url.path
    if path.endswith("/corplink-rs"):
        return httpx.Response(
            200,
            json={
                "full_name": REQUEST["repository"],
                "private": False,
                "default_branch": "master",
            },
        )
    if path.endswith("/commits/master"):
        return httpx.Response(200, json={"sha": COMMIT})
    if path.endswith(f"/git/trees/{COMMIT}"):
        return httpx.Response(
            200,
            json={
                "truncated": False,
                "tree": [
                    {
                        "path": "src/main.rs",
                        "type": "blob",
                        "mode": "100644",
                        "sha": BLOB,
                        "size": len(CONTENT),
                    },
                    {
                        "path": "linked-secret",
                        "type": "blob",
                        "mode": "120000",
                        "sha": "b" * 40,
                        "size": 15,
                    },
                ],
            },
        )
    if path.endswith(f"/git/blobs/{BLOB}"):
        return httpx.Response(
            200,
            json={
                "sha": BLOB,
                "encoding": "base64",
                "content": base64.b64encode(CONTENT).decode(),
            },
        )
    raise AssertionError(f"unexpected GitHub path {path}")


@pytest.mark.asyncio
async def test_source_reads_are_pinned_and_evidence_uses_real_lines() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(github_transport)
    ) as client:
        reader = GitHubReader(client)
        await reader.initialize()
        files = await reader.list_files(ListFilesInput())
        assert files["paths"] == ["src/main.rs"]
        evidence = await reader.read_file(
            ReadFileInput(path="src/main.rs", start_line=2, max_lines=2)
        )
        assert reader.commit_sha == COMMIT
        assert evidence.excerpt == "second line\nthird line"
        assert (evidence.start_line, evidence.end_line, evidence.blob_sha) == (
            2,
            3,
            BLOB,
        )
        assert reader.evidence == [evidence]
        with pytest.raises(RepositoryReadError, match="file_not_in_pinned_tree"):
            await reader.read_file(ReadFileInput(path="linked-secret"))
        with pytest.raises(RepositoryReadError, match="file_not_in_pinned_tree"):
            await reader.read_file(ReadFileInput(path="unlisted-file"))


@pytest.mark.asyncio
async def test_repository_scope_redirect_and_blob_mismatch_fail_closed() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(github_transport)
    ) as client:
        with pytest.raises(RepositoryReadError, match="repository_denied"):
            GitHubReader(client, "other/repo")

    def tampered(request: httpx.Request) -> httpx.Response:
        response = github_transport(request)
        if "/git/blobs/" in request.url.path:
            data = response.json()
            data["content"] = base64.b64encode(b"changed").decode()
            return httpx.Response(200, json=data)
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(tampered)) as client:
        reader = GitHubReader(client)
        await reader.initialize()
        with pytest.raises(RepositoryReadError, match="blob_sha_mismatch"):
            await reader.read_file(ReadFileInput(path="src/main.rs"))
        assert not reader.evidence
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(302, headers={"location": "https://attacker.test"})
        )
    ) as client:
        with pytest.raises(RepositoryReadError, match="github_read_failed"):
            await GitHubReader(client).initialize()


@pytest.mark.asyncio
async def test_tool_budget_prevents_ninth_repository_operation() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(github_transport)
    ) as client:
        reader = GitHubReader(client)
        await reader.initialize()
        for _ in range(8):
            await reader.list_files(ListFilesInput())
        with pytest.raises(RepositoryReadError, match="tool_budget_exhausted"):
            await reader.list_files(ListFilesInput())
        assert reader.budget_exhausted


def test_provider_base_url_is_config_only_and_secret_repr_is_redacted() -> None:
    assert normalized_base_url("https://provider.test/") == "https://provider.test/v1"
    assert (
        normalized_base_url("https://provider.test/v1/") == "https://provider.test/v1"
    )
    with pytest.raises(ValueError):
        normalized_base_url("https://user:password@provider.test/v1")
    secret = ProviderSecret(
        base_url="https://provider.test", api_key="secret-must-not-leak", model="test"
    )
    assert "secret-must-not-leak" not in repr(secret)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", [None, "incomplete", "in_progress", "queued", "failed"]
)
async def test_partial_provider_response_cannot_execute_tools(
    status: str | None,
) -> None:
    secret = ProviderSecret(
        base_url="https://provider.test", api_key="test", model="test"
    )
    bound = build_model(secret)
    guard = CompletionGuard()
    guard.observe_response(
        None if status is None else {"status": status}, bound.spec, None
    )
    message = AssistantMessage(
        content=[
            ToolCall(
                id="t1", name="read_repository_file", arguments={"path": "src/main.rs"}
            )
        ],
        stop_reason="tool_use",
    )
    with pytest.raises(RuntimeError, match="provider_not_completed"):
        await guard.after_model_response(message, None)  # type: ignore[arg-type]
    await bound.provider._client.close()


@pytest.mark.asyncio
async def test_real_factory_honors_guard_before_tools() -> None:
    from cubeloop.providers.faux import FauxProvider

    provider = FauxProvider()
    provider.set_responses(
        [
            AssistantMessage(
                content=[
                    ToolCall(
                        id="t1",
                        name="read_repository_file",
                        arguments={"path": "src/main.rs"},
                    )
                ],
                stop_reason="tool_use",
            )
        ]
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(github_transport)
    ) as client:
        reader = GitHubReader(client)
        await reader.initialize()
        guard = CompletionGuard()
        agent = create_cubeplex_agent(
            bound_model=provider.model("test"), tools=reader.tools(), middleware=[guard]
        )
        await agent.prompt("Read source")
        assert agent.state.error_message == "provider_not_completed"
        assert reader.evidence == []
        assert guard.failure_code == "provider_not_completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("completed", [True, False])
async def test_real_responses_adapter_requires_completion(completed: bool) -> None:
    from openai import AsyncOpenAI

    rounds = 0

    def responses_http(request: httpx.Request) -> httpx.Response:
        nonlocal rounds
        assert request.url.path == "/v1/responses"
        payload = json.loads(request.content)
        assert payload["max_output_tokens"] == 4096
        assert [tool["name"] for tool in payload["tools"]] == [
            "list_repository_files",
            "read_repository_file",
        ]
        rounds += 1
        if rounds == 1:
            item = {
                "type": "function_call",
                "id": "fc_test",
                "call_id": "call_test",
                "name": "read_repository_file",
                "arguments": '{"path":"src/main.rs"}',
                "status": "completed",
            }
        else:
            assert any(
                part.get("type") == "function_call_output" for part in payload["input"]
            )
            item = {
                "type": "message",
                "id": "msg_test",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {
                        "type": "output_text",
                        "text": "src/main.rs:1-3 contains three lines.",
                        "annotations": [],
                    }
                ],
            }
        events = [
            {"type": "response.output_item.added", "output_index": 0, "item": item},
            {"type": "response.output_item.done", "output_index": 0, "item": item},
            {
                "type": "response.completed",
                "response": {
                    "id": f"resp_{rounds}",
                    "status": "completed",
                    "output": [item],
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 10,
                        "total_tokens": 20,
                    },
                },
            },
        ]
        if not completed:
            events = events[:-1]
        content = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, text=content
        )

    secret = ProviderSecret(
        base_url="https://provider.test", api_key="test", model="test"
    )
    bound = build_model(secret)
    await bound.provider._client.close()
    bound.provider._client = AsyncOpenAI(
        base_url="https://provider.test/v1",
        api_key="test",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(responses_http)),
    )
    guard = CompletionGuard()
    detach = bound.provider.subscribe_response(guard.observe_response)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(github_transport)
    ) as client:
        reader = GitHubReader(client)
        await reader.initialize()
        agent = create_cubeplex_agent(
            bound_model=bound, tools=reader.tools(), middleware=[guard]
        )
        await agent.prompt("Read source")
        if completed:
            assert agent.state.error_message is None
            assert agent.state.last_outcome == "complete"
            assert rounds == 2
            assert reader.evidence[0].path == "src/main.rs"
            assert reader.evidence[0].blob_sha == BLOB
        else:
            assert agent.state.error_message == "provider_not_completed"
            assert rounds == 1
            assert reader.evidence == []
    detach()
    await bound.provider._client.close()
