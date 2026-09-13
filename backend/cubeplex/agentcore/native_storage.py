"""Scoped file bytes in the existing ObjectStore and PresentedFile tables."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import mimetypes
import re
from pathlib import PurePosixPath
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col

from cubeplex.agentcore.native_service import (
    NativeTaskError,
    callback_transaction,
    canonical,
    load_native_dispatch,
    payload_hash,
)
from cubeplex.config import config
from cubeplex.db.engine import async_session_maker
from cubeplex.models.agentcore_callback import AgentCoreCallback
from cubeplex.models.agentcore_dispatch import AgentCoreDispatch
from cubeplex.models.presented_file import PresentedFile
from cubeplex.objectstore import get_objectstore_client
from cubeplex.repositories.presented_file import PresentedFileRepository
from cubeplex.services.presented_files import presented_object_key

MAX_FILE = 1_048_576
MAX_PRESENT = 4_194_304
MAX_TOTAL = 8_388_608
MAX_SNAPSHOT = 12_582_912
_CREDENTIAL_PART = re.compile(
    r"(?:^|[-_.])(credential|credentials|secret|secrets|password|passwd|api[-_]?key|token|tokens)(?:$|[-_.])",
    re.IGNORECASE,
)


def safe_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or len(value) > 1024
        or path.is_absolute()
        or str(path) != value
        or "\\" in value
        or any(ord(c) < 32 for c in value)
        or any(
            part.startswith(".")
            or _CREDENTIAL_PART.search(part)
            or part.lower() in {"id_rsa", "id_dsa", "id_ed25519", "id_ecdsa", "known_hosts"}
            for part in path.parts
        )
    ):
        raise NativeTaskError("native_file_path_denied", 422)
    return value


def decode_file(value: dict[str, Any], *, maximum: int) -> bytes:
    safe_path(value["path"])
    encoded = value["content_b64"]
    if not isinstance(encoded, str) or len(encoded) > ((maximum + 2) // 3) * 4:
        raise NativeTaskError("native_file_too_large", 413)
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise NativeTaskError("native_file_encoding_invalid", 422) from exc
    if len(raw) > maximum:
        raise NativeTaskError("native_file_too_large", 413)
    if hashlib.sha256(raw).hexdigest() != value["sha256"]:
        raise NativeTaskError("native_file_hash_mismatch", 422)
    return raw


async def save_workspace(
    dispatch_id: UUID, request_id: UUID, files: list[dict[str, Any]]
) -> dict[str, Any]:
    if len(files) > 4096 or len(canonical(files)) > MAX_SNAPSHOT:
        raise NativeTaskError("native_workspace_too_large", 413)
    total = 0
    names: set[str] = set()
    for file in files:
        if set(file) != {"path", "content_b64", "sha256"} or file["path"] in names:
            raise NativeTaskError("native_workspace_invalid", 422)
        total += len(decode_file(file, maximum=MAX_FILE))
        names.add(file["path"])
    if total > MAX_TOTAL:
        raise NativeTaskError("native_workspace_too_large", 413)
    ordered = sorted(files, key=lambda file: file["path"])
    snapshot = {"files": ordered}
    sha = payload_hash(snapshot)
    async with callback_transaction(dispatch_id, request_id, "workspace", snapshot) as tx:
        if tx.replay:
            result = tx.result or {}
        else:
            d = tx.dispatch
            key = f"native-workspace/{d.org_id}/{d.workspace_id}/{d.conversation_id}/{sha}.json"
            # Content-addressed immutable bytes: a transaction rollback leaves
            # at most the same unreferenced object, never a new logical version.
            await get_objectstore_client().upload_file(key, canonical(snapshot), "application/json")
            result = {"sha256": sha, "file_count": len(files), "object_key": key}
            tx.complete(result)
        return {key: result[key] for key in ("sha256", "file_count")}


async def load_workspace(dispatch_id: UUID) -> dict[str, Any]:
    async with async_session_maker() as session:
        d = await load_native_dispatch(session, dispatch_id)
        if d.status == "created":
            raise NativeTaskError("native_task_not_claimed")
        saved = await session.scalar(
            select(AgentCoreCallback)
            .join(
                AgentCoreDispatch, col(AgentCoreDispatch.id) == col(AgentCoreCallback.dispatch_id)
            )
            .where(
                col(AgentCoreDispatch.org_id) == d.org_id,
                col(AgentCoreDispatch.workspace_id) == d.workspace_id,
                col(AgentCoreDispatch.conversation_id) == d.conversation_id,
                col(AgentCoreCallback.operation) == "workspace",
                col(AgentCoreCallback.status) == "done",
            )
            .order_by(col(AgentCoreCallback.completed_at).desc())
            .limit(1)
        )
        if saved is None:
            return {"files": [], "sha256": payload_hash({"files": []})}
        result = saved.response or {}
    raw, _ = await get_objectstore_client().download_file(result["object_key"])
    if len(raw) > MAX_SNAPSHOT or hashlib.sha256(raw).hexdigest() != result["sha256"]:
        raise NativeTaskError("native_workspace_corrupt", 500)
    value: dict[str, Any] = json.loads(raw)
    return {**value, "sha256": result["sha256"]}


async def present_file(dispatch_id: UUID, request_id: UUID, file: dict[str, Any]) -> dict[str, Any]:
    content = decode_file(file, maximum=MAX_PRESENT)
    filename = PurePosixPath(file["path"]).name
    # First native slice presents text deliverables; don't run image/document
    # parsers on untrusted VM bytes in the trusted process.
    mime = mimetypes.guess_type(filename)[0] or "text/plain"
    if mime not in {"text/plain", "text/markdown", "text/x-python", "application/json", "text/csv"}:
        raise NativeTaskError("native_present_type_denied", 422)
    try:
        content.decode("utf-8")
    except UnicodeError as exc:
        raise NativeTaskError("native_present_encoding_invalid", 422) from exc
    async with callback_transaction(dispatch_id, request_id, "present", file) as tx:
        if tx.replay:
            return tx.result or {}
        d = tx.dispatch
        repo = PresentedFileRepository(tx.session, org_id=d.org_id, workspace_id=d.workspace_id)
        current = await repo.sum_size(d.conversation_id)
        limit = int(config.get("presented_files.max_per_conversation_bytes", 524288000))
        if current + len(content) > limit:
            raise NativeTaskError("native_present_quota_exceeded", 413)
        row = PresentedFile(
            org_id=d.org_id,
            workspace_id=d.workspace_id,
            conversation_id=d.conversation_id,
            run_id=d.run_id,
            source_path="/workspace/" + file["path"],
            filename=filename,
            mime_type=mime,
            size_bytes=len(content),
            kind="document",
            object_key="",
            caption=file["name"][:1024],
        )
        row.object_key = presented_object_key(
            org_id=d.org_id,
            workspace_id=d.workspace_id,
            conversation_id=d.conversation_id,
            file_id=f"native-{d.id}-{request_id}",
            filename=filename,
        )
        await get_objectstore_client().upload_file(row.object_key, content, mime)
        tx.session.add(row)
        await tx.session.flush()
        result = row.to_dict()
        tx.complete(result)
        return result
