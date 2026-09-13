"""Real native Worker to HTTP callbacks, PostgreSQL, Redis and RustFS.

Only the external model HTTPS response is scripted; the native Agent,
checkpoint protocol, human-input channel, file tools and storage are real.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
import respx
from cryptography.fernet import Fernet
from fastapi import FastAPI
from sqlalchemy import delete

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "deploy/agentcore-native-entry"))
from cubeplex_native.client import NativeControlPlaneClient  # noqa: E402
from cubeplex_native.worker import NativeWorker  # noqa: E402
from cubeplex_native.workspace import Workspace  # noqa: E402

from cubeplex.agentcore.native_auth import issue_capability  # noqa: E402
from cubeplex.agentcore.native_checkpoint import checkpoint_call  # noqa: E402
from cubeplex.agentcore.native_router import router  # noqa: E402
from cubeplex.config import config  # noqa: E402
from cubeplex.credentials.encryption import FernetBackend  # noqa: E402
from cubeplex.db.engine import async_session_maker  # noqa: E402
from cubeplex.models.agentcore_dispatch import AgentCoreDispatch  # noqa: E402
from cubeplex.models.credential import Credential  # noqa: E402
from cubeplex.models.presented_file import PresentedFile  # noqa: E402
from cubeplex.models.provider import Model, Provider  # noqa: E402
from cubeplex.streams.hitl_resume import claim_resume  # noqa: E402
from cubeplex.streams.run_events import _run_events_key, _run_meta_key  # noqa: E402
from cubeplex.streams.run_manager import RunContext, RunManager  # noqa: E402
from tests.e2e.test_agentcore_native_lifecycle import Task, native_task  # noqa: E402,F401

pytestmark = pytest.mark.asyncio


def sse(index: int, name: str | None, args: dict[str, Any] | str) -> str:
    events: list[dict[str, Any]] = []
    if name:
        item = {
            "id": f"fc_{index}",
            "type": "function_call",
            "call_id": f"call_{index}",
            "name": name,
            "arguments": "",
            "status": "in_progress",
        }
        events.append({"type": "response.output_item.added", "output_index": 0, "item": dict(item)})
        encoded = json.dumps(args)
        events.append(
            {
                "type": "response.function_call_arguments.delta",
                "output_index": 0,
                "item_id": item["id"],
                "delta": encoded,
            }
        )
        item.update(arguments=encoded, status="completed")
        events.append({"type": "response.output_item.done", "output_index": 0, "item": item})
    else:
        item = {
            "id": f"msg_{index}",
            "type": "message",
            "role": "assistant",
            "content": [],
            "status": "in_progress",
        }
        events.append({"type": "response.output_item.added", "output_index": 0, "item": dict(item)})
        events.append(
            {
                "type": "response.output_text.delta",
                "output_index": 0,
                "item_id": item["id"],
                "content_index": 0,
                "delta": args,
                "logprobs": [],
            }
        )
        item.update(
            content=[{"type": "output_text", "text": args, "annotations": [], "logprobs": []}],
            status="completed",
        )
        events.append({"type": "response.output_item.done", "output_index": 0, "item": item})
    events.append(
        {
            "type": "response.completed",
            "response": {
                "id": f"resp_{index}",
                "status": "completed",
                "object": "response",
                "created_at": 1,
                "model": "native-test",
                "output": [item],
                "usage": {
                    "input_tokens": 8,
                    "output_tokens": 8,
                    "total_tokens": 16,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens_details": {"reasoning_tokens": 0},
                },
            },
        }
    )
    return "".join(
        "data: " + json.dumps({**event, "sequence_number": i}) + "\n\n"
        for i, event in enumerate(events)
    )


async def test_http_worker_tools_pause_answer_and_fresh_workspace(
    native_task: Task,  # noqa: F811 -- imported shared fixture
    tmp_path: Path,
) -> None:
    redis, prefix, d = native_task
    old_secret = config.get("auth.jwt_secret")
    config.set("auth.jwt_secret", secrets.token_hex(32))
    encryption = FernetBackend([Fernet.generate_key()])
    credential = Credential(
        org_id=d.org_id,
        kind="provider_api_key",
        name="native-e2e",
        value_encrypted=await encryption.encrypt(b"local-provider-canary"),
    )
    provider = Provider(
        org_id=d.org_id,
        name="native-e2e",
        slug="native-e2e",
        provider_type="openai-responses",
        base_url="https://native-model.invalid/v1",
        credential_id=credential.id,
    )
    model = Model(
        org_id=d.org_id,
        provider_id=provider.id,
        model_id="native-test",
        display_name="native-test",
        context_window=32000,
        max_tokens=512,
        reasoning=True,
    )
    async with async_session_maker() as session:
        session.add(credential)
        await session.flush()
        session.add(provider)
        await session.flush()
        session.add(model)
        row = await session.get(AgentCoreDispatch, d.id)
        assert row is not None
        row.status = "created"
        row.request = {
            **row.request,
            "content": "write and present a file, then ask a question",
            "model_ref": "native-e2e/native-test",
            "runtime_arn": "arn:testing",
            "system_prompt": "Use the tools.",
            "model": {"id": "native-test", "max_output_tokens": 512, "context_window": 32000},
            "reasoning": {"effort": "low"},
        }
        await session.commit()
    app = FastAPI()
    app.state.redis, app.state.redis_key_prefix = redis, prefix
    app.state.encryption_backend = encryption
    app.include_router(router, prefix="/api/v1")
    calls: list[dict[str, Any]] = []
    steps = [
        ("execute", {"command": "printf 'native file' > report.txt"}),
        ("present_file", {"path": "report.txt"}),
        ("ask_user", {"questions": [{"key": "color", "prompt": "Choose a color"}]}),
        (None, "The selected color is blue; the saved file is intact."),
    ]

    def reply(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer local-provider-canary"
        body = json.loads(request.content)
        assert body["store"] is False
        calls.append(body)
        name, args = steps[len(calls) - 1]
        return httpx.Response(
            200, text=sse(len(calls), name, args), headers={"content-type": "text/event-stream"}
        )

    try:
        with respx.mock(assert_all_called=True) as mock:
            mock.post("https://native-model.invalid/v1/responses").mock(side_effect=reply)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
                client = NativeControlPlaneClient(
                    base_url="https://control.invalid",
                    dispatch_id=str(d.id),
                    capability=issue_capability(d.id),
                    http_client=http,
                )
                first = await asyncio.wait_for(
                    NativeWorker(client, workspace=Workspace(tmp_path / "vm1")).run(), 30
                )
                assert first["status"] == "paused_hitl", first
                pending = await checkpoint_call(d.id, uuid4(), "load_pending", {})
                assert pending is not None and pending["run_id"] == d.run_id
                history = await checkpoint_call(d.id, uuid4(), "load", {})
                assert len(calls) == 3
                records = [
                    json.loads(fields["payload"])
                    for _, fields in await redis.xrange(_run_events_key(prefix, d.run_id))
                ]
                assert any(event["type"] == "presented_file" for event in records)
                assert any(event["type"] == "ask_user_request" for event in records)
                claim = await claim_resume(
                    redis,
                    prefix=prefix,
                    conversation_id=d.conversation_id,
                    expected_run_id=d.run_id,
                    started_at="2026-09-13T00:00:00+00:00",
                    ttl_seconds=120,
                )
                assert claim.claim_token
                manager = RunManager(
                    app=app, redis=redis, key_prefix=prefix, run_event_ttl_seconds=120
                )
                respond = await manager._create_remote_respond_dispatch(
                    run_id=d.run_id,
                    conversation_id=d.conversation_id,
                    question_id=pending["request"]["question_id"],
                    answer={"color": "blue"},
                    claim_token=claim.claim_token,
                    ctx=RunContext(
                        user_id=d.user_id,
                        org_id=d.org_id,
                        workspace_id=d.workspace_id,
                        conversation_id=d.conversation_id,
                    ),
                )
                second_client = NativeControlPlaneClient(
                    base_url="https://control.invalid",
                    dispatch_id=str(respond.id),
                    capability=issue_capability(respond.id),
                    http_client=http,
                )
                second = await asyncio.wait_for(
                    NativeWorker(second_client, workspace=Workspace(tmp_path / "vm2")).run(), 30
                )
                assert second["status"] == "completed", second
                assert (tmp_path / "vm2" / "report.txt").read_text() == "native file"
                assert len(calls) == 4
                final = await checkpoint_call(respond.id, uuid4(), "load", {})
                assert final["messages"][: len(history["messages"])] == history["messages"]
                assert await redis.hget(_run_meta_key(prefix, d.run_id), "status") == "completed"
                assert await checkpoint_call(respond.id, uuid4(), "load_pending", {}) is None
    finally:
        config.set("auth.jwt_secret", old_secret)
        async with async_session_maker() as session:
            await session.execute(
                delete(PresentedFile).where(PresentedFile.conversation_id == d.conversation_id)
            )
            await session.execute(delete(Model).where(Model.provider_id == provider.id))
            await session.execute(delete(Provider).where(Provider.id == provider.id))
            await session.execute(delete(Credential).where(Credential.id == credential.id))
            await session.commit()
