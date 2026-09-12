"""Startup recovery for runs stranded by a crashed / killed process.

Called once during app lifespan, after Redis and DB are ready but before
the app begins serving requests (before ``yield``).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from loguru import logger
from redis.asyncio import Redis

from cubeplex.streams.run_events import get_run_meta, mark_run_stale


async def recover_stranded_runs(redis: Redis, *, prefix: str) -> int:
    """Scan Redis for stranded active-run keys and clean them up.

    Returns the number of stranded runs recovered.
    """
    pattern = f"{prefix}:conversation_active_run:*"
    prefix_len = len(f"{prefix}:conversation_active_run:")
    recovered: list[tuple[str, str]] = []

    async for key in redis.scan_iter(match=pattern, count=200):
        run_id = await redis.get(key)
        if run_id is None:
            continue
        meta = await get_run_meta(redis, prefix=prefix, run_id=run_id)
        if meta is None or meta.status != "running":
            continue
        # A remote AgentCore worker may still own this run while the API
        # process is restarting.  Redis alone cannot distinguish that healthy
        # worker from a dead local task, so the durable dispatch row wins.  Do
        # not mark it stale or resubmit it; the worker continues publishing to
        # the same Redis stream and the next API instance can attach delivery.
        if await _has_active_remote_dispatch(run_id):
            logger.info(
                "Startup recovery preserved remote AgentCore run {}",
                run_id,
            )
            continue
        conversation_id = key[prefix_len:]
        await mark_run_stale(
            redis,
            prefix=prefix,
            run_id=run_id,
            conversation_id=conversation_id,
        )
        recovered.append((conversation_id, run_id))
        logger.info(
            "Recovered stranded run {} on conversation {}",
            run_id,
            conversation_id,
        )

    if not recovered:
        return 0

    await _stamp_cubeloop_runs(recovered)
    await _fail_stranded_scheduled_runs([rid for _, rid in recovered])
    await _repair_stranded_threads([cid for cid, _ in recovered])

    logger.info("Startup recovery: {} stranded run(s) cleaned up", len(recovered))
    return len(recovered)


async def _has_active_remote_dispatch(run_id: str) -> bool:
    """Reconcile remote ownership before local stale-run recovery.

    A recent dispatch heartbeat is healthy and remains attached.  A stale
    dispatch is fenced with StopRuntimeSession under a bounded wait; because
    Runtime teardown does not prove OpenSandbox command cancellation, the
    dispatch remains ``stop_unknown`` and continues blocking new work until a
    later operator/worker readback confirms terminal state.
    """
    try:
        from cubeplex.agentcore.dispatch import (
            active_dispatch_for_run,
            mark_dispatch_stop_unknown,
        )
        from cubeplex.config import config
        from cubeplex.db.engine import async_session_maker

        dispatch = await active_dispatch_for_run(async_session_maker, run_id=run_id)
        if dispatch is None:
            return False
        timeout_seconds = int(config.get("agentcore.dispatch_heartbeat_timeout_seconds", 180))
        heartbeat = dispatch.heartbeat_at or dispatch.claimed_at or dispatch.created_at
        age = (datetime.now(UTC) - heartbeat).total_seconds()
        if age <= timeout_seconds:
            return True

        try:
            from cubeplex.agentcore.client import AgentCoreClient

            runtime_arn = str(config.get("agentcore.runtime_arn", ""))
            region = str(config.get("agentcore.region", config.get("aws.region", "")) or "")
            client = AgentCoreClient(runtime_arn=runtime_arn, region_name=region or None)
            await asyncio.wait_for(client.stop(dispatch.id), timeout=10.0)
        except Exception as exc:
            logger.warning(
                "AgentCore stop reconciliation failed for run {}: {}",
                run_id,
                type(exc).__name__,
            )
        if await active_dispatch_for_run(async_session_maker, run_id=run_id) is None:
            return True
        await mark_dispatch_stop_unknown(
            async_session_maker,
            dispatch_id=dispatch.id,
            error_message="startup recovery could not confirm remote teardown",
        )
        return True
    except Exception as exc:
        if _agentcore_backend_enabled():
            # In remote mode a dispatch lookup failure is not evidence of a
            # dead local run. Fail startup closed instead of killing a healthy
            # remote worker or accepting a duplicate request.
            raise
        # Local-only deployments may start before this optional migration.
        logger.debug("Remote dispatch recovery lookup unavailable: {}", type(exc).__name__)
        return False


def _agentcore_backend_enabled() -> bool:
    from cubeplex.config import config

    return str(config.get("execution.backend", "local")) == "agentcore"


async def _stamp_cubeloop_runs(pairs: list[tuple[str, str]]) -> None:
    """Mark stranded cubepi_runs rows as completed so history is consistent."""
    from cubeplex.agents.checkpointer import shared_checkpointer

    try:
        async with shared_checkpointer() as cp:
            for thread_id, run_id in pairs:
                try:
                    await cp.mark_run_complete(thread_id, run_id)
                except Exception as exc:
                    logger.warning(
                        "Failed to stamp cubepi_runs for {}/{}: {}",
                        thread_id,
                        run_id,
                        exc,
                    )
    except Exception as exc:
        logger.warning("Could not open checkpointer for recovery: {}", exc)


async def _fail_stranded_scheduled_runs(run_ids: list[str]) -> None:
    from cubeplex.schedules.completion_hook import (
        record_scheduled_run_terminal_state,
    )

    for run_id in run_ids:
        try:
            await record_scheduled_run_terminal_state(run_id=run_id, run_status="cancelled")
        except Exception as exc:
            logger.warning(
                "Failed to mark scheduled run {} as failed: {}",
                run_id,
                exc,
            )


async def _repair_stranded_threads(conversation_ids: list[str]) -> None:
    from cubeplex.streams.run_manager import _repair_dangling_tool_calls

    for conv_id in conversation_ids:
        try:
            await _repair_dangling_tool_calls(conv_id)
        except Exception as exc:
            logger.warning(
                "Failed to repair dangling tool_calls for {}: {}",
                conv_id,
                exc,
            )
