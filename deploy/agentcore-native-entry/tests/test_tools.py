from __future__ import annotations

import json
from pathlib import Path

import pytest

from cubeplex_native.tools import NativeTools
from cubeplex_native.workspace import Workspace, WorkspacePathError


@pytest.mark.asyncio
async def test_cubeloop_tools_are_real_tool_objects(tmp_path: Path) -> None:
    tools = NativeTools(tmp_path).as_cubeloop_tools()
    assert [item.name for item in tools] == [
        "execute",
        "read_file",
        "write_file",
        "edit_file",
    ]
    assert all(item.to_definition().name == item.name for item in tools)
    write = next(item for item in tools if item.name == "write_file")
    params = write.parameters(path="answer.txt", content="42")
    result = await write.execute("call-1", params)
    assert json.loads(result.content[0].text)["path"] == "answer.txt"


@pytest.mark.asyncio
async def test_present_file_callback_gets_only_validated_regular_file(
    tmp_path: Path,
) -> None:
    seen: list[tuple[str, bytes, str]] = []

    async def present(
        path: str,
        name: str,
        content_b64: str,
        sha256: str,
        mime_type: str | None,
    ) -> dict[str, str | int | None]:
        seen.append((path, content_b64.encode(), mime_type or ""))
        return {
            "file": name,
            "sha256": sha256,
            "content_b64": content_b64,
            "mime_type": mime_type,
        }

    tools = NativeTools(Workspace(tmp_path), present_callback=present)
    tools.workspace.write_file("report.txt", "hello")
    result = await tools.present_file("report.txt")
    assert result["file"] == "report.txt"
    assert result["mime_type"] is None
    assert result["content_b64"] == "aGVsbG8="
    assert seen[0][0] == str(tmp_path / "report.txt")
    with pytest.raises(WorkspacePathError):
        await tools.present_file("../outside.txt")


@pytest.mark.asyncio
async def test_present_tool_is_only_exposed_with_callback(tmp_path: Path) -> None:
    assert [item.name for item in NativeTools(tmp_path).as_cubeloop_tools()] == [
        "execute",
        "read_file",
        "write_file",
        "edit_file",
    ]

    async def present(
        path: str,
        name: str,
        content_b64: str,
        sha256: str,
        mime_type: str | None,
    ) -> dict[str, str | None]:
        return {
            "path": path,
            "name": name,
            "content_b64": content_b64,
            "mime_type": mime_type,
        }

    tools_owner = NativeTools(tmp_path, present_callback=present)
    tools_owner.workspace.write_file("image.png", "pixels")
    tools = tools_owner.as_cubeloop_tools()
    assert [item.name for item in tools][-1] == "present_file"
    present_tool = tools[-1]
    result = await present_tool.execute(
        "call-present",
        present_tool.parameters(path="image.png", mime_type="image/png"),
    )
    assert json.loads(result.content[0].text)["mime_type"] == "image/png"
