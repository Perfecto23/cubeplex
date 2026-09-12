"""AgentCore Runtime worker for durable CubePlex dispatches."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cubeplex.agentcore.dispatch import (
    AgentCoreDispatchError,
    AgentCoreStopUnknown,
    DispatchValidationError,
    active_dispatch_for_run,
    agentcore_session_id,
    claim_dispatch,
    mark_dispatch_finished,
    mark_dispatch_stop_unknown,
    parse_invocation_payload,
    request_context_from_payload,
)
from cubeplex.models.agentcore_dispatch import AgentCoreDispatch
from cubeplex.models.conversation import Conversation
from cubeplex.streams.run_events import (
    append_run_event,
    clear_active_run,
    expire_run_data,
    get_run_meta,
    update_run_meta,
)


@dataclass(frozen=True, slots=True)
class WorkerInvocationResult:
    dispatch_id: UUID
    status: str
    duplicate: bool = False
    error_code: str | None = None

    def model_dump(self) -> dict[str, object]:
        result: dict[str, object] = {
            "version": 1,
            "dispatch_id": str(self.dispatch_id),
            "status": self.status,
            "duplicate": self.duplicate,
        }
        if self.error_code is not None:
            result["error_code"] = self.error_code
        return result


ManagerFactory = Callable[[AgentCoreDispatch], Awaitable[Any] | Any]


class AgentCoreWorker:
    """Claims and executes exactly one persisted dispatch."""

    def __init__(
        self,
        *,
        session_maker: async_sessionmaker[Any],
        redis: Any,
        redis_key_prefix: str,
        manager_factory: ManagerFactory,
        heartbeat_seconds: float = 10.0,
        run_event_ttl_seconds: int = 43200,
    ) -> None:
        self._session_maker = session_maker
        self._redis = redis
        self._redis_key_prefix = redis_key_prefix
        self._manager_factory = manager_factory
        self._heartbeat_seconds = heartbeat_seconds
        self._run_event_ttl_seconds = run_event_ttl_seconds
        self._prepare_lock = asyncio.Lock()
        self._parser_ready = False
        # The SDK request may complete before the native run. Keep a strong
        # reference until the tracked task finishes so a disconnect cannot
        # collect the task or stop its heartbeat/control watchers.
        self._background_tasks: set[asyncio.Task[Any]] = set()

    async def invoke(
        self,
        payload: Mapping[str, object],
        *,
        session_id: str,
    ) -> WorkerInvocationResult:
        """Run one prepared dispatch inline and await its terminal result."""
        dispatch, early_result = await self._prepare_dispatch(payload, session_id=session_id)
        if early_result is not None:
            return early_result
        assert dispatch is not None
        return await self._execute_claimed(dispatch)

    async def accept(
        self,
        payload: Mapping[str, object],
        *,
        session_id: str,
        app: Any,
    ) -> WorkerInvocationResult:
        """Admit a dispatch and let it continue after the HTTP request returns.

        AgentCore invokes async entrypoints on its dedicated worker loop. The
        task is created on that loop and registered with the SDK so ``/ping``
        reports ``HealthyBusy`` while native execution is active.
        """
        dispatch, early_result = await self._prepare_dispatch(payload, session_id=session_id)
        if early_result is not None:
            return early_result
        assert dispatch is not None

        add_async_task = getattr(app, "add_async_task", None)
        complete_async_task = getattr(app, "complete_async_task", None)
        if not callable(add_async_task) or not callable(complete_async_task):
            await self._fail_native_run(dispatch, "worker_tracking_unavailable")
            await mark_dispatch_finished(
                self._session_maker,
                dispatch_id=dispatch.id,
                error_code="worker_tracking_unavailable",
            )
            return WorkerInvocationResult(
                dispatch_id=dispatch.id,
                status="finished",
                error_code="worker_tracking_unavailable",
            )

        task_id: int | None = None
        try:
            task_id = add_async_task(
                "cubeplex-agentcore-dispatch",
                {"dispatch_id": str(dispatch.id)},
            )
            task = asyncio.create_task(
                self._run_tracked(dispatch, app, task_id),
                name=f"agentcore-dispatch:{dispatch.id}",
            )
        except Exception as exc:
            if task_id is not None:
                with suppress(Exception):
                    complete_async_task(task_id)
            await self._fail_native_run(dispatch, "worker_start_failed")
            with suppress(Exception):
                await mark_dispatch_finished(
                    self._session_maker,
                    dispatch_id=dispatch.id,
                    error_code="worker_start_failed",
                    error_message=type(exc).__name__,
                )
            return WorkerInvocationResult(
                dispatch_id=dispatch.id,
                status="finished",
                error_code="worker_start_failed",
            )

        self._background_tasks.add(task)
        task.add_done_callback(self._background_task_done)
        return WorkerInvocationResult(dispatch_id=dispatch.id, status="accepted")

    def _background_task_done(self, task: asyncio.Task[Any]) -> None:
        """Drop the strong reference and consume unexpected task exceptions."""
        self._background_tasks.discard(task)
        with suppress(asyncio.CancelledError):
            task.exception()

    async def _prepare_dispatch(
        self,
        payload: Mapping[str, object],
        *,
        session_id: str,
    ) -> tuple[AgentCoreDispatch | None, WorkerInvocationResult | None]:
        """Validate, claim, and prepare a dispatch before native execution."""
        invocation = parse_invocation_payload(payload)
        expected_session = agentcore_session_id(invocation.dispatch_id)
        if session_id != expected_session:
            raise DispatchValidationError("agentcore_session_scope_mismatch")

        dispatch, owner = await claim_dispatch(
            self._session_maker,
            dispatch_id=invocation.dispatch_id,
        )
        if not owner:
            return (
                None,
                WorkerInvocationResult(
                    dispatch_id=invocation.dispatch_id,
                    status=dispatch.status,
                    duplicate=True,
                ),
            )

        scope_validated = False
        try:
            await self._validate_native_scope(dispatch)
            scope_validated = True
            await self._ensure_runtime_dependencies()
            if dispatch.stop_requested:
                await self._cancel_native_run(dispatch)
                await mark_dispatch_finished(
                    self._session_maker,
                    dispatch_id=dispatch.id,
                    error_code="cancelled",
                )
                return (
                    None,
                    WorkerInvocationResult(
                        dispatch_id=dispatch.id,
                        status="finished",
                        error_code="cancelled",
                    ),
                )
            return dispatch, None
        except DispatchValidationError:
            with suppress(Exception):
                await mark_dispatch_finished(
                    self._session_maker,
                    dispatch_id=dispatch.id,
                    error_code="dispatch_validation_failed",
                )
            raise
        except Exception as exc:
            if scope_validated:
                await self._fail_native_run(dispatch, "worker_start_failed")
            with suppress(Exception):
                await mark_dispatch_finished(
                    self._session_maker,
                    dispatch_id=dispatch.id,
                    error_code="worker_execution_failed",
                    error_message=type(exc).__name__,
                )
            return (
                None,
                WorkerInvocationResult(
                    dispatch_id=dispatch.id,
                    status="finished",
                    error_code="worker_execution_failed",
                ),
            )

    async def _execute_claimed(self, dispatch: AgentCoreDispatch) -> WorkerInvocationResult:
        """Run a previously prepared dispatch and persist its terminal state."""
        execution_started = False
        manager: Any | None = None
        stop_controls: Any | None = None
        stop_watcher: asyncio.Task[Any] | None = None
        heartbeat: asyncio.Task[Any] | None = None
        try:
            manager_value = self._manager_factory(dispatch)
            manager = await manager_value if inspect.isawaitable(manager_value) else manager_value
            start_controls = getattr(manager, "start_control_listeners", None)
            stop_controls = getattr(manager, "stop_control_listeners", None)
            if callable(start_controls):
                await start_controls()

            execution = asyncio.create_task(
                self._execute_manager_dispatch(manager, dispatch),
                name=f"agentcore-dispatch:{dispatch.id}",
            )
            execution_started = True
            stop_watcher = asyncio.create_task(
                self._watch_stop_request(dispatch.id, dispatch.run_id, manager, execution),
                name=f"agentcore-stop-watch:{dispatch.id}",
            )
            heartbeat = asyncio.create_task(
                self._heartbeat(
                    dispatch.id,
                    dispatch.run_id,
                    dispatch.conversation_id,
                    execution,
                ),
                name=f"agentcore-heartbeat:{dispatch.id}",
            )
            await execution
            # Persist the worker terminal state before stopping the RunManager
            # control listeners.  A listener may be waiting for cancel_run;
            # that wait must never be the prerequisite for this transition.
            finished = await mark_dispatch_finished(
                self._session_maker,
                dispatch_id=dispatch.id,
            )
            if not finished:
                async with self._session_maker() as latest_session:
                    latest = await latest_session.get(
                        AgentCoreDispatch,
                        dispatch.id,
                    )
                if latest is not None and latest.status == "stop_unknown":
                    return WorkerInvocationResult(
                        dispatch_id=dispatch.id,
                        status="stop_unknown",
                        error_code="stop_unknown",
                    )
            await self._publish_confirmed_cancel_ack(manager, dispatch)
            return WorkerInvocationResult(dispatch_id=dispatch.id, status="finished")
        except asyncio.CancelledError:
            # A control-plane stop cancels the native RunManager task.  Turn
            # that confirmed local teardown into a terminal dispatch record so
            # the API can observe cancellation; an unrequested Runtime kill
            # remains claimed/unknown and is reconciled by recovery.
            with suppress(Exception):
                async with self._session_maker() as session:
                    row = await session.get(AgentCoreDispatch, dispatch.id)
                    if row is not None and row.stop_requested:
                        finished = await mark_dispatch_finished(
                            self._session_maker,
                            dispatch_id=dispatch.id,
                            error_code="cancelled",
                        )
                        if not finished:
                            async with self._session_maker() as latest_session:
                                latest = await latest_session.get(
                                    AgentCoreDispatch,
                                    dispatch.id,
                                )
                            if latest is not None and latest.status == "stop_unknown":
                                return WorkerInvocationResult(
                                    dispatch_id=dispatch.id,
                                    status="stop_unknown",
                                    error_code="stop_unknown",
                                )
                        await self._publish_confirmed_cancel_ack(manager, dispatch)
                        return WorkerInvocationResult(
                            dispatch_id=dispatch.id,
                            status="finished",
                            error_code="cancelled",
                        )
            raise
        except AgentCoreStopUnknown:
            fenced = await mark_dispatch_stop_unknown(
                self._session_maker,
                dispatch_id=dispatch.id,
                error_message="native sandbox command teardown is unconfirmed",
            )
            if not fenced:
                async with self._session_maker() as session:
                    latest = await session.get(AgentCoreDispatch, dispatch.id)
                if latest is not None and latest.status == "finished":
                    return WorkerInvocationResult(
                        dispatch_id=dispatch.id,
                        status="finished",
                        error_code=latest.error_code,
                    )
            return WorkerInvocationResult(
                dispatch_id=dispatch.id,
                status="stop_unknown",
                error_code="stop_unknown",
            )
        except DispatchValidationError:
            with suppress(Exception):
                await mark_dispatch_finished(
                    self._session_maker,
                    dispatch_id=dispatch.id,
                    error_code="dispatch_validation_failed",
                )
            raise
        except Exception as exc:
            if not execution_started:
                await self._fail_native_run(dispatch, "worker_start_failed")
            with suppress(Exception):
                finished = await mark_dispatch_finished(
                    self._session_maker,
                    dispatch_id=dispatch.id,
                    error_code="worker_execution_failed",
                    error_message=type(exc).__name__,
                )
                if not finished:
                    async with self._session_maker() as session:
                        latest = await session.get(AgentCoreDispatch, dispatch.id)
                    if latest is not None and latest.status == "stop_unknown":
                        return WorkerInvocationResult(
                            dispatch_id=dispatch.id,
                            status="stop_unknown",
                            error_code="stop_unknown",
                        )
            return WorkerInvocationResult(
                dispatch_id=dispatch.id,
                status="finished",
                error_code="worker_execution_failed",
            )
        finally:
            for task in (stop_watcher, heartbeat):
                if task is not None:
                    task.cancel()
            for task in (stop_watcher, heartbeat):
                if task is not None:
                    with suppress(asyncio.CancelledError):
                        await task
            if callable(stop_controls):
                # The durable dispatch has already been fenced above. Listener
                # teardown is cleanup and must not delay or overwrite that
                # terminal state if Redis is slow during shutdown.
                with suppress(Exception):
                    await stop_controls()

    async def _publish_confirmed_cancel_ack(
        self,
        manager: Any,
        dispatch: AgentCoreDispatch,
    ) -> None:
        """ACK only after a requested dispatch is durably ``finished``."""
        try:
            async with self._session_maker() as session:
                row = await session.get(AgentCoreDispatch, dispatch.id)
                if row is None or row.status != "finished" or not row.stop_requested:
                    return
            publish_ack = getattr(manager, "_publish_ack", None)
            if callable(publish_ack):
                await publish_ack(dispatch.run_id)
        except Exception:
            # A missing ACK is safe: the API falls back to Runtime stop and
            # durable readback. Never turn a successful native stop into a
            # second execution or a false terminal state here.
            return

    async def _run_tracked(self, dispatch: AgentCoreDispatch, app: Any, task_id: int) -> None:
        """Keep native execution alive after the Runtime request completes."""
        try:
            await self._execute_claimed(dispatch)
        finally:
            with suppress(Exception):
                app.complete_async_task(task_id)

    async def _fail_native_run(self, dispatch: AgentCoreDispatch, error_code: str) -> None:
        """Publish an explicit terminal error before closing a pre-exec run."""
        from datetime import UTC, datetime

        from cubeplex.agents.schemas import DoneEvent, ErrorEvent

        message = "AgentCore worker failed before native execution started"
        await update_run_meta(
            self._redis,
            prefix=self._redis_key_prefix,
            run_id=dispatch.run_id,
            status="errored",
            error_code=error_code,
            error_message=message,
        )
        timestamp = datetime.now(UTC).isoformat()
        for event in (
            ErrorEvent(
                timestamp=timestamp,
                data={"error_code": error_code, "message": message, "details": message},
            ),
            DoneEvent(timestamp=datetime.now(UTC).isoformat(), data={}),
        ):
            await append_run_event(
                self._redis,
                prefix=self._redis_key_prefix,
                run_id=dispatch.run_id,
                conversation_id=dispatch.conversation_id,
                payload=event.model_dump(),
                ttl_seconds=self._run_event_ttl_seconds,
                maxlen=1000000,
            )
        await clear_active_run(
            self._redis,
            prefix=self._redis_key_prefix,
            conversation_id=dispatch.conversation_id,
            run_id=dispatch.run_id,
        )
        await expire_run_data(
            self._redis,
            prefix=self._redis_key_prefix,
            run_id=dispatch.run_id,
            ttl_seconds=self._run_event_ttl_seconds,
        )

    async def _cancel_native_run(self, dispatch: AgentCoreDispatch) -> None:
        """Close a run cancelled before native execution starts."""
        from datetime import UTC, datetime

        from cubeplex.agents.schemas import DoneEvent

        await update_run_meta(
            self._redis,
            prefix=self._redis_key_prefix,
            run_id=dispatch.run_id,
            status="cancelled",
        )
        await append_run_event(
            self._redis,
            prefix=self._redis_key_prefix,
            run_id=dispatch.run_id,
            conversation_id=dispatch.conversation_id,
            payload=DoneEvent(
                timestamp=datetime.now(UTC).isoformat(),
                data={"cancelled": True},
            ).model_dump(),
            ttl_seconds=self._run_event_ttl_seconds,
            maxlen=1000000,
        )
        await clear_active_run(
            self._redis,
            prefix=self._redis_key_prefix,
            conversation_id=dispatch.conversation_id,
            run_id=dispatch.run_id,
        )
        await expire_run_data(
            self._redis,
            prefix=self._redis_key_prefix,
            run_id=dispatch.run_id,
            ttl_seconds=self._run_event_ttl_seconds,
        )

    async def _validate_native_scope(self, dispatch: AgentCoreDispatch) -> None:
        context = request_context_from_payload(dispatch.request)
        expected = {
            "org_id": dispatch.org_id,
            "workspace_id": dispatch.workspace_id,
            "conversation_id": dispatch.conversation_id,
            "user_id": dispatch.user_id,
        }
        if any(context.get(key) != value for key, value in expected.items()):
            raise DispatchValidationError("agentcore_dispatch_context_scope_mismatch")

        async with self._session_maker() as session:
            await self._validate_conversation(session, dispatch)
        meta = await get_run_meta(
            self._redis,
            prefix=self._redis_key_prefix,
            run_id=dispatch.run_id,
        )
        if meta is None or meta.conversation_id != dispatch.conversation_id:
            raise DispatchValidationError("agentcore_native_run_not_found")
        if meta.status not in {"running", "paused_hitl"}:
            raise DispatchValidationError("agentcore_native_run_not_active")
        if dispatch.operation == "respond":
            await self._validate_respond_claim(dispatch)

    async def _validate_respond_claim(self, dispatch: AgentCoreDispatch) -> None:
        request_claim = dispatch.request.get("claim_token")
        if not isinstance(request_claim, str) or not request_claim:
            raise DispatchValidationError("agentcore_respond_claim_missing")
        from cubeplex.streams.run_events import _run_meta_key

        current_claim = await self._redis.hget(
            _run_meta_key(self._redis_key_prefix, dispatch.run_id),
            "claim_token",
        )
        if isinstance(current_claim, bytes):
            current_claim = current_claim.decode()
        if current_claim != request_claim:
            raise DispatchValidationError("agentcore_respond_claim_mismatch")

        from cubeplex.agents.checkpointer import shared_checkpointer

        async with shared_checkpointer() as checkpointer:
            pending = await checkpointer.load_pending_request(dispatch.conversation_id)
        expected_question = dispatch.request.get("question_id")
        if pending is None or pending.question_id != expected_question:
            raise DispatchValidationError("agentcore_respond_question_mismatch")

    async def _ensure_runtime_dependencies(self) -> None:
        """Discover parser entry points exactly once per worker process."""
        async with self._prepare_lock:
            if self._parser_ready:
                return
            from cubeplex.parsers import get_parser_registry

            registry = get_parser_registry()
            if not getattr(registry, "_parsers", None):
                await registry.discover()
            self._parser_ready = True

    @staticmethod
    async def _validate_conversation(session: AsyncSession, dispatch: AgentCoreDispatch) -> None:
        conversation = await session.get(Conversation, dispatch.conversation_id)
        if conversation is None:
            raise DispatchValidationError("agentcore_conversation_not_found")
        if (
            conversation.org_id != dispatch.org_id
            or conversation.workspace_id != dispatch.workspace_id
        ):
            raise DispatchValidationError("agentcore_conversation_scope_mismatch")

    async def _execute_manager_dispatch(self, manager: Any, dispatch: AgentCoreDispatch) -> None:
        execute = getattr(manager, "execute_agentcore_dispatch", None)
        if not callable(execute):
            raise AgentCoreDispatchError("agentcore_worker_manager_missing_dispatch_entry")
        await execute(dispatch)

    async def _heartbeat(
        self,
        dispatch_id: UUID,
        run_id: str,
        conversation_id: str,
        execution: asyncio.Task[Any],
    ) -> None:
        from cubeplex.streams.run_events import touch_run_heartbeat

        while not execution.done():
            await asyncio.sleep(self._heartbeat_seconds)
            if execution.done():
                return
            with suppress(Exception):
                await touch_run_heartbeat(
                    self._redis,
                    prefix=self._redis_key_prefix,
                    run_id=run_id,
                    conversation_id=conversation_id,
                    ttl_seconds=self._run_event_ttl_seconds,
                )
            async with self._session_maker() as session:
                row = await session.get(AgentCoreDispatch, dispatch_id)
                if row is None:
                    return
                row.touch_heartbeat()
                await session.commit()

    async def _watch_stop_request(
        self,
        dispatch_id: UUID,
        run_id: str,
        manager: Any,
        execution: asyncio.Task[Any],
    ) -> None:
        while not execution.done():
            await asyncio.sleep(0.5)
            if execution.done():
                return
            async with self._session_maker() as session:
                row = await session.get(AgentCoreDispatch, dispatch_id)
                requested = bool(row.stop_requested) if row is not None else False
            if not requested:
                continue
            cancel = getattr(manager, "cancel_run", None)
            if callable(cancel):
                await cancel(run_id)
            return


async def active_remote_dispatch(
    session_maker: async_sessionmaker[Any],
    *,
    run_id: str,
) -> AgentCoreDispatch | None:
    """Compatibility wrapper used by startup recovery and stop paths."""
    return await active_dispatch_for_run(session_maker, run_id=run_id)
