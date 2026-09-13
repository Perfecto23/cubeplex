"""Real Postgres/Redis completion, pause and stopped-session boundaries."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy import delete, select

from cubeplex.agentcore.dispatch import agentcore_session_id
from cubeplex.agentcore.native_checkpoint import checkpoint_call
from cubeplex.agentcore.native_lifecycle import finish_native_dispatch
from cubeplex.agentcore.native_service import NativeTaskError, callback_transaction
from cubeplex.agents.checkpointer import close_shared_checkpointer
from cubeplex.config import config
from cubeplex.db.engine import async_session_maker
from cubeplex.models.agentcore_callback import AgentCoreCallback
from cubeplex.models.agentcore_dispatch import AgentCoreDispatch
from cubeplex.models.conversation import Conversation
from cubeplex.models.embedding_job import EmbeddingJob
from cubeplex.streams.run_events import _active_run_key, _run_events_key, _run_meta_key, create_run
from tests.e2e.im_fixtures import im_cleanup, im_seed_org_ws_user

pytestmark = pytest.mark.asyncio
Task = tuple[Redis, str, AgentCoreDispatch]


@pytest_asyncio.fixture
async def native_task() -> AsyncIterator[Task]:
    suffix = uuid4().hex[:8]
    org, ws, user, conv = (f"{p}-nl{suffix}" for p in ["org", "ws", "usr", "conv"])
    rid = str(uuid4())
    dispatch = AgentCoreDispatch(
        org_id=org,
        workspace_id=ws,
        conversation_id=conv,
        user_id=user,
        run_id=rid,
        operation="prompt",
        session_id="",
        status="claimed",
        request={
            "execution_mode": "native",
            "context": {
                "org_id": org,
                "workspace_id": ws,
                "user_id": user,
                "conversation_id": conv,
                "trigger": "interactive",
            },
        },
    )
    dispatch.session_id = agentcore_session_id(dispatch.id)
    async with async_session_maker() as session:
        await im_seed_org_ws_user(
            session, org_id=org, ws_id=ws, user_id=user, email=f"native-{suffix}@example.com"
        )
        session.add(
            Conversation(
                id=conv,
                org_id=org,
                workspace_id=ws,
                creator_user_id=user,
                title="Native lifecycle",
                reasoning={},
                attributes={},
            )
        )
        await session.commit()
        session.add(dispatch)
        await session.commit()
    redis = Redis.from_url(str(config.get("redis.url")), decode_responses=True)
    prefix = f"native-lifecycle-{suffix}"
    await create_run(
        redis,
        prefix=prefix,
        run_id=rid,
        conversation_id=conv,
        status="running",
        started_at=datetime.now(UTC).isoformat(),
        ttl_seconds=120,
    )
    try:
        yield redis, prefix, dispatch
    finally:
        from cubeloop.checkpointer.postgres.models import CubeloopThread

        await close_shared_checkpointer()
        keys = [key async for key in redis.scan_iter(match=prefix + ":*")]
        if keys:
            await redis.delete(*keys)
        await redis.aclose()
        async with async_session_maker() as session:
            await session.execute(
                delete(AgentCoreCallback).where(
                    AgentCoreCallback.dispatch_id.in_(
                        select(AgentCoreDispatch.id).where(
                            AgentCoreDispatch.conversation_id == conv
                        )
                    )
                )
            )
            await session.execute(
                delete(AgentCoreDispatch).where(AgentCoreDispatch.conversation_id == conv)
            )
            await session.execute(delete(CubeloopThread).where(CubeloopThread.thread_id == conv))
            await session.execute(delete(EmbeddingJob).where(EmbeddingJob.conversation_id == conv))
            await session.execute(delete(Conversation).where(Conversation.id == conv))
            await im_cleanup(session, ws_ids=[ws], user_ids=[user], org_ids=[org])
            await session.commit()


async def finish(task: Task, status: str, request_id: UUID | None = None) -> dict[str, Any]:
    redis, prefix, d = task
    return await finish_native_dispatch(
        redis,
        prefix=prefix,
        dispatch_id=d.id,
        request_id=request_id or uuid4(),
        status=status,
        error_code=None,
        ttl_seconds=120,
        maxlen=1000,
    )


async def workspace_saved(d: AgentCoreDispatch) -> None:
    async with callback_transaction(d.id, uuid4(), "workspace", {"files": []}) as tx:
        tx.complete({"sha256": "0" * 64, "file_count": 0, "object_key": "test"})


async def test_completion_requires_saved_workspace_and_native_run_completion(
    native_task: Task,
) -> None:
    redis, prefix, d = native_task
    with pytest.raises(NativeTaskError) as missing:
        await finish(native_task, "completed")
    assert missing.value.code == "native_workspace_not_saved"
    await workspace_saved(d)
    await checkpoint_call(d.id, uuid4(), "claim_run", {})
    await checkpoint_call(
        d.id,
        uuid4(),
        "append",
        {
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "hello"}],
                    "run_id": d.run_id,
                },
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "done"}],
                    "run_id": d.run_id,
                },
            ]
        },
    )
    await checkpoint_call(d.id, uuid4(), "mark_run_complete", {})
    req = uuid4()
    first = await finish(native_task, "completed", req)
    duplicate = await finish(native_task, "completed", req)
    assert duplicate == first
    assert await redis.xlen(_run_events_key(prefix, d.run_id)) == 1
    assert await redis.hget(_run_meta_key(prefix, d.run_id), "status") == "completed"
    assert await redis.get(_active_run_key(prefix, d.conversation_id)) is None
    with pytest.raises(NativeTaskError):
        await checkpoint_call(d.id, uuid4(), "save_extra", {"extra": {"late": True}})


@pytest.mark.parametrize("before_claim", [False, True])
async def test_stop_fences_late_completion_and_new_checkpoint(
    native_task: Task, before_claim: bool
) -> None:
    redis, prefix, d = native_task
    async with async_session_maker() as session:
        row = await session.get(AgentCoreDispatch, d.id)
        assert row is not None
        row.stop_requested = True
        row.status = "created" if before_claim else "claimed"
        await session.commit()
    with pytest.raises(NativeTaskError):
        await checkpoint_call(d.id, uuid4(), "claim_run", {})
    with pytest.raises(NativeTaskError) as stopped:
        await finish(native_task, "completed")
    assert stopped.value.code == "native_stop_requested"
    result = await finish(native_task, "cancelled")
    assert result["status"] == "cancelled"
    assert (await finish(native_task, "completed"))["status"] == "already_finished"
    assert await redis.xlen(_run_events_key(prefix, d.run_id)) == 1
    entries = await redis.xrange(_run_events_key(prefix, d.run_id))
    assert json.loads(entries[0][1]["payload"])["data"]["cancelled"] is True


async def test_pause_keeps_lock_and_pending_request(native_task: Task) -> None:
    redis, prefix, d = native_task
    await workspace_saved(d)
    await checkpoint_call(d.id, uuid4(), "claim_run", {})
    await checkpoint_call(
        d.id,
        uuid4(),
        "save_pending_request",
        {
            "request": {
                "question_id": "q1",
                "thread_id": d.conversation_id,
                "created_at": datetime.now(UTC).timestamp(),
                "payload": {"kind": "ask", "questions": [{"key": "pick", "prompt": "Choose"}]},
            }
        },
    )
    result = await finish(native_task, "paused_hitl")
    assert result["status"] == "paused_hitl"
    assert await redis.get(_active_run_key(prefix, d.conversation_id)) == d.run_id
    assert await redis.hget(_run_meta_key(prefix, d.run_id), "status") == "paused_hitl"
    pending = await checkpoint_call(d.id, uuid4(), "load_pending", {})
    assert pending["run_id"] == d.run_id and pending["request"]["question_id"] == "q1"


@pytest.mark.parametrize("aws_confirms_stop", [True, False])
async def test_cancel_releases_only_after_external_session_stop(
    native_task: Task, aws_confirms_stop: bool
) -> None:
    from cubeplex.agentcore.native_lifecycle import cancel_native_dispatch

    redis, prefix, d = native_task
    seen = []

    class ExternalRuntime:
        async def stop(self, dispatch_id: UUID) -> dict[str, object]:
            seen.append(dispatch_id)
            if not aws_confirms_stop:
                raise TimeoutError("network outcome unknown")
            return {"statusCode": 200}

    result = await cancel_native_dispatch(
        d,
        client=ExternalRuntime(),
        redis=redis,
        prefix=prefix,
        ttl_seconds=120,
        maxlen=1000,
    )
    assert seen == [d.id]
    async with async_session_maker() as session:
        row = await session.get(AgentCoreDispatch, d.id)
        assert row is not None and row.stop_requested
        assert row.status == ("finished" if aws_confirms_stop else "stop_unknown")
    assert result == ("cancelled" if aws_confirms_stop else "stop_unknown")
    active = await redis.get(_active_run_key(prefix, d.conversation_id))
    assert active == (None if aws_confirms_stop else d.run_id)


async def test_completed_marker_without_final_answer_is_not_success(native_task: Task) -> None:
    redis, prefix, d = native_task
    await workspace_saved(d)
    await checkpoint_call(d.id, uuid4(), "claim_run", {})
    await checkpoint_call(
        d.id,
        uuid4(),
        "append",
        {
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "answer me"}],
                    "run_id": d.run_id,
                }
            ]
        },
    )
    await checkpoint_call(d.id, uuid4(), "mark_run_complete", {})
    with pytest.raises(NativeTaskError) as missing:
        await finish(native_task, "completed")
    assert missing.value.code == "native_final_answer_missing"
    assert await redis.hget(_run_meta_key(prefix, d.run_id), "status") == "running"
    assert await redis.xlen(_run_events_key(prefix, d.run_id)) == 0
