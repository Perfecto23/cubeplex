"""CubeLoop Checkpointer implemented by the native control-plane API."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from cubeloop.checkpointer.base import CheckpointData
from cubeloop.hitl.types import HitlRequest
from cubeloop.providers.base import Message
from cubeloop.types import JsonObject, StructuredValue
from pydantic import TypeAdapter

from .client import NativeControlPlaneClient


_MESSAGE_ADAPTER: TypeAdapter[Message] = TypeAdapter(Message)


def _messages(value: Any) -> list[Message]:
    if not isinstance(value, list):
        raise ValueError("checkpoint messages must be a list")
    return [_MESSAGE_ADAPTER.validate_python(item) for item in value]


class RemoteCheckpointer:
    """Do not keep a second history source in the MicroVM."""

    def __init__(self, client: NativeControlPlaneClient, thread_id: str) -> None:
        self.client = client
        self.thread_id = thread_id

    async def load(self, thread_id: str) -> CheckpointData | None:
        if thread_id != self.thread_id:
            raise ValueError("thread_id_mismatch")
        result = await self.client.checkpoint("load")
        if result is None:
            return None
        if not isinstance(result, dict):
            raise ValueError("checkpoint_load_invalid")
        return CheckpointData(
            messages=_messages(result.get("messages", [])),
            extra=dict(result.get("extra") or {}),
            parent_thread_id=result.get("parent_thread_id"),
        )

    async def append(self, thread_id: str, messages: list[Message]) -> None:
        if thread_id != self.thread_id:
            raise ValueError("thread_id_mismatch")
        await self.client.checkpoint(
            "append",
            {"messages": [message.model_dump(mode="json") for message in messages]},
        )

    async def save_extra(self, thread_id: str, extra: JsonObject) -> None:
        if thread_id != self.thread_id:
            raise ValueError("thread_id_mismatch")
        await self.client.checkpoint("save_extra", {"extra": dict(extra)})

    async def save_pending_request(
        self,
        thread_id: str,
        request: HitlRequest | None,
        *,
        run_id: str | None = None,
    ) -> None:
        if thread_id != self.thread_id:
            raise ValueError("thread_id_mismatch")
        await self.client.checkpoint(
            "save_pending_request",
            {"request": request.model_dump(mode="json") if request is not None else None},
        )

    async def load_pending_request(self, thread_id: str) -> HitlRequest | None:
        if thread_id != self.thread_id:
            raise ValueError("thread_id_mismatch")
        result = await self.client.checkpoint("load_pending_request")
        if result is None:
            return None
        if not isinstance(result, dict):
            raise ValueError("pending_request_invalid")
        return HitlRequest.model_validate(result)

    async def load_pending(self, thread_id: str) -> tuple[HitlRequest, str | None] | None:
        if thread_id != self.thread_id:
            raise ValueError("thread_id_mismatch")
        result = await self.client.checkpoint("load_pending")
        if result is None:
            return None
        if not isinstance(result, dict) or not isinstance(result.get("request"), dict):
            raise ValueError("pending_request_invalid")
        return HitlRequest.model_validate(result["request"]), result.get("run_id")

    async def claim_run(self, thread_id: str, run_id: str) -> None:
        if thread_id != self.thread_id:
            raise ValueError("thread_id_mismatch")
        await self.client.checkpoint("claim_run")

    async def mark_run_complete(self, thread_id: str, run_id: str) -> None:
        if thread_id != self.thread_id:
            raise ValueError("thread_id_mismatch")
        await self.client.checkpoint("mark_run_complete")

    async def save_hitl_answer(
        self,
        thread_id: str,
        question_id: str,
        answer: StructuredValue,
        *,
        run_id: str | None = None,
    ) -> None:
        if thread_id != self.thread_id:
            raise ValueError("thread_id_mismatch")
        await self.client.checkpoint(
            "save_hitl_answer",
            {"question_id": question_id, "answer": answer},
        )

    async def load_hitl_answer(
        self,
        thread_id: str,
        question_id: str,
        *,
        run_id: str | None = None,
    ) -> StructuredValue | None:
        if thread_id != self.thread_id:
            raise ValueError("thread_id_mismatch")
        result = await self.client.checkpoint("load_hitl_answer", {"question_id": question_id})
        return result

    async def clear_hitl_answers(
        self,
        thread_id: str,
        question_ids: Iterable[str] | None = None,
        *,
        run_id: str | None = None,
    ) -> None:
        if thread_id != self.thread_id:
            raise ValueError("thread_id_mismatch")
        await self.client.checkpoint(
            "clear_hitl_answers",
            {"question_ids": list(question_ids) if question_ids is not None else None},
        )

    async def snapshot(self, thread_id: str, *, after_run_id: str) -> list[Message]:
        raise NotImplementedError("native control plane does not expose snapshot")

    async def fork(
        self,
        src_thread_id: str,
        new_thread_id: str,
        *,
        after_run_id: str,
        metadata: JsonObject | None = None,
    ) -> None:
        raise NotImplementedError("native control plane does not expose fork")
