"""CubeLoop tools backed by a local MicroVM workspace.

The functions in this module contain no HTTP protocol or CubePlex imports.  A
control-plane adapter can provide ``present_callback`` while the worker keeps
file validation and process lifecycle local to the VM.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from cubeplex_native.workspace import Workspace, WorkspaceError

PresentFileCallback = Callable[[str, str, str, str, str | None], Awaitable[Any]]


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _error(exc: WorkspaceError) -> Any:
    from cubeloop import AgentToolResult, TextContent

    return AgentToolResult(
        content=[TextContent(text=_json({"error": str(exc)}))],
        is_error=True,
    )


class NativeTools:
    """Local execute/read/write/edit tools for one task workspace."""

    def __init__(
        self,
        workspace: Workspace | str | Path = "/workspace",
        *,
        present_callback: PresentFileCallback | None = None,
    ) -> None:
        self.workspace = workspace if isinstance(workspace, Workspace) else Workspace(workspace)
        self.present_callback = present_callback

    async def execute(
        self,
        command: str,
        timeout_seconds: float = 120.0,
        *,
        cancel: asyncio.Event | None = None,
        signal: asyncio.Event | None = None,
    ) -> dict[str, Any]:
        """Run ``bash -c`` from the workspace root."""

        return await self.workspace.execute(
            command,
            timeout_seconds=timeout_seconds,
            cancel=cancel or signal,
        )

    async def read_file(self, path: str, max_bytes: int | None = None) -> dict[str, Any]:
        """Read a bounded UTF-8 file."""

        return self.workspace.read_file(path, max_bytes=max_bytes)

    async def write_file(
        self,
        path: str,
        content: str,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Atomically write a bounded UTF-8 file."""

        return self.workspace.write_file(path, content, overwrite=overwrite)

    async def edit_file(
        self,
        path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> dict[str, Any]:
        """Replace a unique text span in a bounded UTF-8 file."""

        return self.workspace.edit_file(
            path,
            old_string,
            new_string,
            replace_all=replace_all,
        )

    async def present_file(
        self,
        path: str,
        mime_type: str | None = None,
    ) -> Any:
        """Validate and pass one ordinary file to the supplied callback."""

        if self.present_callback is None:
            raise WorkspaceError("present_callback_required")
        relative, target = self.workspace._target(path, existing=True)
        if self.workspace._is_snapshot_forbidden(relative):
            raise WorkspaceError("protected_file")
        data = self.workspace._read_bounded(
            target,
            max_bytes=self.workspace.limits.max_present_bytes,
        )
        callback_result = self.present_callback(
            str(target),
            relative.name,
            base64.b64encode(data).decode("ascii"),
            hashlib.sha256(data).hexdigest(),
            mime_type,
        )
        if inspect.isawaitable(callback_result):
            return await callback_result
        return callback_result

    def as_cubeloop_tools(self) -> list[Any]:
        """Build actual ``cubeloop.tool`` objects for the native Agent."""

        from cubeloop import tool

        @tool(
            name="execute",
            description=(
                "Run a bounded bash command in /workspace. Commands are killed as a "
                "process group on timeout or cancellation."
            ),
            execution_mode="sequential",
        )
        async def execute_tool(
            command: str,
            timeout_seconds: float = 120.0,
            signal: asyncio.Event | None = None,
        ) -> str | Any:
            try:
                return _json(await self.execute(command, timeout_seconds, signal=signal))
            except WorkspaceError as exc:
                return _error(exc)

        @tool(
            name="read_file",
            description="Read a bounded UTF-8 file under /workspace.",
            execution_mode="sequential",
        )
        async def read_file_tool(path: str, max_bytes: int | None = None) -> str | Any:
            try:
                return _json(await self.read_file(path, max_bytes))
            except WorkspaceError as exc:
                return _error(exc)

        @tool(
            name="write_file",
            description=(
                "Atomically create a bounded UTF-8 file under /workspace. Existing files "
                "require overwrite=true."
            ),
            execution_mode="sequential",
        )
        async def write_file_tool(
            path: str,
            content: str,
            overwrite: bool = False,
        ) -> str | Any:
            try:
                return _json(await self.write_file(path, content, overwrite))
            except WorkspaceError as exc:
                return _error(exc)

        @tool(
            name="edit_file",
            description=(
                "Replace one unique text span in a UTF-8 file under /workspace; set "
                "replace_all=true for an explicit multi-match edit."
            ),
            execution_mode="sequential",
        )
        async def edit_file_tool(
            path: str,
            old_string: str,
            new_string: str,
            replace_all: bool = False,
        ) -> str | Any:
            try:
                return _json(await self.edit_file(path, old_string, new_string, replace_all))
            except WorkspaceError as exc:
                return _error(exc)

        tools: list[Any] = [
            execute_tool,
            read_file_tool,
            write_file_tool,
            edit_file_tool,
        ]
        if self.present_callback is not None:

            @tool(
                name="present_file",
                description=(
                    "Pass one validated ordinary workspace file to the control-plane "
                    "presentation callback."
                ),
                execution_mode="sequential",
            )
            async def present_file_tool(
                path: str,
                mime_type: str | None = None,
            ) -> str | Any:
                try:
                    return _json(await self.present_file(path, mime_type))
                except WorkspaceError as exc:
                    return _error(exc)

            tools.append(present_file_tool)
        return tools
