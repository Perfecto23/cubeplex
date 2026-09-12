"""Offline contracts for the durable AgentCore dispatch boundary."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from cubeplex.agentcore.client import AgentCoreClient
from cubeplex.agentcore.dispatch import (
    DispatchValidationError,
    agentcore_session_id,
    build_prompt_request,
    build_respond_request,
    invocation_payload,
    parse_invocation_payload,
    request_context_from_payload,
)


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(
        user_id="u1",
        org_id="o1",
        workspace_id="w1",
        conversation_id="c1",
        trigger="interactive",
        topic_id="t1",
        is_group_chat=False,
        sender_display_name="Perfecto",
        sandbox_mode=None,
        topic_creator_user_id=None,
        conversation_creator_user_id=None,
    )


def test_invocation_contains_only_version_and_dispatch_id() -> None:
    dispatch_id = uuid4()
    payload = invocation_payload(dispatch_id)
    assert payload == {"version": 1, "dispatch_id": str(dispatch_id)}
    assert parse_invocation_payload(payload).dispatch_id == dispatch_id
    assert agentcore_session_id(dispatch_id).startswith("cubeplex-agentcore-")


@pytest.mark.parametrize(
    "payload",
    [
        {"version": 1, "dispatch_id": str(uuid4()), "org_id": "o1"},
        {"version": 2, "dispatch_id": str(uuid4())},
        {"version": 1, "dispatch_id": "not-a-uuid"},
    ],
)
def test_worker_payload_rejects_scope_tampering(payload: dict[str, object]) -> None:
    with pytest.raises(DispatchValidationError):
        parse_invocation_payload(payload)


def test_prompt_and_respond_requests_roundtrip_server_scope() -> None:
    ctx = _ctx()
    prompt = build_prompt_request(
        content="inspect the incident",
        attachments=["atch-1"],
        ctx=ctx,
        model_key="provider:model",
        reasoning=SimpleNamespace(mode="auto", effort="low", summary="none"),
    )
    respond = build_respond_request(
        question_id="q1",
        answer={"approved": True},
        claim_token="claim-1",
        ctx=ctx,
    )
    assert request_context_from_payload(prompt)["conversation_id"] == "c1"
    assert request_context_from_payload(respond)["org_id"] == "o1"
    assert prompt["content"] == "inspect the incident"
    assert respond["claim_token"] == "claim-1"


@pytest.mark.asyncio
async def test_boto_client_sends_only_dispatch_wire_payload() -> None:
    class Body:
        def read(self, _limit: int) -> bytes:
            return b'{"status":"finished"}'

        def close(self) -> None:
            return None

    class Client:
        def __init__(self) -> None:
            self.kwargs: dict[str, object] | None = None

        def invoke_agent_runtime(self, **kwargs: object) -> dict[str, object]:
            self.kwargs = kwargs
            return {"response": Body()}

    dispatch_id = uuid4()
    client = Client()
    result = await AgentCoreClient(runtime_arn="arn:test", client=client).invoke(dispatch_id)
    assert result == {"status": "finished"}
    assert client.kwargs is not None
    assert client.kwargs["runtimeSessionId"] == agentcore_session_id(dispatch_id)
    assert (
        client.kwargs["payload"]
        == ('{"version":1,"dispatch_id":"' + str(dispatch_id) + '"}').encode()
    )
