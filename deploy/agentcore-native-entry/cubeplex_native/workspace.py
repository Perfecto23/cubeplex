"""Local MicroVM workspace primitives.

The native worker deliberately keeps filesystem policy in this module.  Tool
wrappers can therefore expose the same rules to CubeLoop and to local tests
without introducing an HTTP or CubePlex control-plane dependency.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import errno
import hashlib
import json
import os
import re
import signal as signal_module
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


class WorkspaceError(RuntimeError):
    """Base error for a rejected workspace operation."""


class WorkspacePathError(WorkspaceError):
    """The requested path is outside the workspace or is not a regular file."""


class WorkspaceLimitError(WorkspaceError):
    """A command, file, or snapshot exceeded its configured bound."""


class WorkspaceSnapshotError(WorkspaceError):
    """A snapshot failed validation or could not be restored safely."""


@dataclass(frozen=True)
class WorkspaceLimits:
    """Hard bounds for local tools and recovery data."""

    max_command_bytes: int = 16_384
    max_timeout_seconds: float = 900.0
    max_output_bytes: int = 32_000
    max_file_bytes: int = 1_048_576
    max_present_bytes: int = 4_194_304
    max_snapshot_files: int = 4_096
    max_snapshot_total_bytes: int = 8_388_608
    max_snapshot_bytes: int = 12_582_912


@dataclass(frozen=True)
class _SnapshotFile:
    path: str
    content: bytes


_CREDENTIAL_BASENAMES = frozenset(
    {
        ".aws",
        ".docker",
        ".env",
        ".netrc",
        ".npmrc",
        ".pypirc",
        ".ssh",
        ".token",
        ".tokens",
        ".kube",
        "credentials",
        "credentials.json",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
        "known_hosts",
        "secrets",
        "secrets.json",
    }
)
_CREDENTIAL_PART = re.compile(
    r"(?:^|[-_.])(credential|credentials|secret|secrets|password|passwd|api[-_]?key)"
    r"(?:$|[-_.])",
    re.IGNORECASE,
)
_SENSITIVE_SUFFIXES = (".pem", ".p12", ".pfx", ".key", ".keystore")


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _json_size(value: Mapping[str, Any]) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


class Workspace:
    """Operate on a bounded, task-local directory.

    ``root`` represents ``/workspace`` inside the MicroVM.  The default is the
    production path; tests can pass a temporary directory.  No method follows
    a link or writes outside this root.
    """

    def __init__(
        self,
        root: str | os.PathLike[str] = "/workspace",
        *,
        limits: WorkspaceLimits | None = None,
    ) -> None:
        self.root = Path(root).expanduser()
        if self.root.exists():
            if self.root.is_symlink():
                raise WorkspacePathError("workspace_root_is_symlink")
            if not self.root.is_dir():
                raise WorkspacePathError("workspace_root_not_directory")
        else:
            self.root.mkdir(parents=True, exist_ok=True)
        self.root = self.root.resolve()
        self.limits = limits or WorkspaceLimits()
        self._validate_limits()

    def _validate_limits(self) -> None:
        values = (
            self.limits.max_command_bytes,
            self.limits.max_output_bytes,
            self.limits.max_file_bytes,
            self.limits.max_present_bytes,
            self.limits.max_snapshot_files,
            self.limits.max_snapshot_total_bytes,
            self.limits.max_snapshot_bytes,
        )
        if any(value <= 0 for value in values) or self.limits.max_timeout_seconds <= 0:
            raise ValueError("workspace limits must be positive")

    @property
    def workspace_path(self) -> str:
        """Return the canonical path passed to presentation callbacks."""

        return str(self.root)

    def _relative_path(self, value: str | os.PathLike[str]) -> PurePosixPath:
        if not isinstance(value, (str, os.PathLike)):
            raise WorkspacePathError("path_invalid")
        raw = os.fspath(value)
        if not isinstance(raw, str) or not raw or "\x00" in raw:
            raise WorkspacePathError("path_invalid")
        if "\\" in raw:
            raise WorkspacePathError("path_invalid")

        candidate = Path(raw)
        if candidate.is_absolute():
            absolute = candidate
            if not _is_relative_to(absolute, self.root):
                raise WorkspacePathError("path_outside_workspace")
            relative = absolute.relative_to(self.root)
        else:
            relative = candidate
        if not relative.parts or relative == Path("."):
            raise WorkspacePathError("path_is_workspace_root")
        if any(part in {"", ".", ".."} for part in relative.parts):
            raise WorkspacePathError("path_invalid")
        return PurePosixPath(*relative.parts)

    def _target(
        self,
        value: str | os.PathLike[str],
        *,
        existing: bool = False,
        directory: bool = False,
    ) -> tuple[PurePosixPath, Path]:
        relative = self._relative_path(value)
        target = self.root.joinpath(*relative.parts)
        resolved = target.resolve(strict=False)
        if not _is_relative_to(resolved, self.root):
            raise WorkspacePathError("path_symlink_escape")
        if existing:
            self._assert_existing_parents(target.parent)
            self._assert_regular_or_directory(target, directory=directory)
        else:
            self._assert_safe_parents(target.parent)
        return relative, target

    def _assert_safe_parents(self, parent: Path) -> None:
        current = parent
        missing: list[Path] = []
        while current != self.root:
            if not _is_relative_to(current, self.root):
                raise WorkspacePathError("path_outside_workspace")
            try:
                mode = current.lstat().st_mode
            except FileNotFoundError:
                missing.append(current)
                current = current.parent
                continue
            if stat.S_ISLNK(mode):
                raise WorkspacePathError("path_parent_is_symlink")
            if not stat.S_ISDIR(mode):
                raise WorkspacePathError("path_parent_not_directory")
            current = current.parent
        for directory in reversed(missing):
            directory.mkdir(mode=0o700)

    def _assert_existing_parents(self, parent: Path) -> None:
        """Reject symlinked or non-directory parents without creating anything."""

        current = parent
        while current != self.root:
            if not _is_relative_to(current, self.root):
                raise WorkspacePathError("path_outside_workspace")
            try:
                info = current.lstat()
            except FileNotFoundError as exc:
                raise WorkspacePathError("file_not_found") from exc
            if stat.S_ISLNK(info.st_mode):
                raise WorkspacePathError("path_parent_is_symlink")
            if not stat.S_ISDIR(info.st_mode):
                raise WorkspacePathError("path_parent_not_directory")
            current = current.parent

    def _assert_regular_or_directory(self, path: Path, *, directory: bool) -> os.stat_result:
        try:
            info = path.lstat()
        except FileNotFoundError as exc:
            raise WorkspacePathError("file_not_found") from exc
        if stat.S_ISLNK(info.st_mode):
            raise WorkspacePathError("symlink_not_allowed")
        if directory:
            if not stat.S_ISDIR(info.st_mode):
                raise WorkspacePathError("workspace_cwd_not_directory")
            return info
        if not stat.S_ISREG(info.st_mode):
            raise WorkspacePathError("regular_file_required")
        if info.st_nlink != 1:
            raise WorkspacePathError("hardlink_not_allowed")
        return info

    def _is_protected(self, relative: PurePosixPath) -> bool:
        parts = relative.parts
        if any(part.startswith(".agentcore-") for part in parts):
            return True
        lowered = [part.casefold() for part in parts]
        if any(part in _CREDENTIAL_BASENAMES for part in lowered):
            return True
        for part in lowered:
            if part.endswith(_SENSITIVE_SUFFIXES) or _CREDENTIAL_PART.search(part):
                return True
        if len(parts) >= 2 and parts[0] == ".git":
            if parts[1] in {
                "config",
                "config.worktree",
                "credentials",
                "hooks",
                "logs",
            }:
                return True
        return False

    def _is_snapshot_forbidden(self, relative: PurePosixPath) -> bool:
        """Return whether a path is outside the ordinary-file snapshot contract."""

        return any(part.startswith(".") for part in relative.parts) or self._is_protected(relative)

    def _iter_snapshot_files(self) -> list[tuple[PurePosixPath, Path]]:
        result: list[tuple[PurePosixPath, Path]] = []
        for current, directories, filenames in os.walk(self.root, topdown=True, followlinks=False):
            current_path = Path(current)
            safe_directories: list[str] = []
            for name in sorted(directories):
                if name.startswith("."):
                    continue
                directory = current_path / name
                info = directory.lstat()
                if stat.S_ISLNK(info.st_mode):
                    raise WorkspaceSnapshotError("symlink_not_allowed")
                if not stat.S_ISDIR(info.st_mode):
                    raise WorkspaceSnapshotError("directory_entry_invalid")
                safe_directories.append(name)
            directories[:] = safe_directories
            for name in sorted(filenames):
                if name.startswith("."):
                    continue
                path = current_path / name
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode):
                    raise WorkspaceSnapshotError("symlink_not_allowed")
                if not stat.S_ISREG(info.st_mode):
                    raise WorkspaceSnapshotError("regular_file_required")
                if info.st_nlink != 1:
                    raise WorkspaceSnapshotError("hardlink_not_allowed")
                relative = PurePosixPath(path.relative_to(self.root).as_posix())
                if self._is_snapshot_forbidden(relative):
                    continue
                result.append((relative, path))
        return sorted(result, key=lambda item: item[0].as_posix())

    def _read_bounded(self, path: Path, *, max_bytes: int) -> bytes:
        if max_bytes < 0:
            raise WorkspaceLimitError("file_too_large")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise WorkspacePathError("symlink_not_allowed") from exc
            if exc.errno == errno.ENOENT:
                raise WorkspacePathError("file_not_found") from exc
            raise WorkspacePathError("file_open_failed") from exc
        try:
            info = os.fstat(fd)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise WorkspacePathError("regular_file_required")
            if info.st_nlink != 1:
                raise WorkspacePathError("hardlink_not_allowed")
            if info.st_size > max_bytes:
                raise WorkspaceLimitError("file_too_large")
            data = bytearray()
            while len(data) <= max_bytes:
                chunk = os.read(fd, min(65_536, max_bytes + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > max_bytes:
                    raise WorkspaceLimitError("file_too_large")
            return bytes(data)
        finally:
            os.close(fd)

    def read_file(self, path: str, *, max_bytes: int | None = None) -> dict[str, Any]:
        """Read one validated UTF-8 file from the workspace."""

        relative, target = self._target(path, existing=True)
        if self._is_protected(relative):
            raise WorkspacePathError("protected_file")
        data = self._read_bounded(
            target,
            max_bytes=self.limits.max_file_bytes if max_bytes is None else max_bytes,
        )
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WorkspaceError("file_not_utf8") from exc
        return {
            "path": relative.as_posix(),
            "content": content,
            "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }

    def _atomic_write(self, target: Path, data: bytes, *, overwrite: bool) -> None:
        self._assert_safe_parents(target.parent)
        if target.exists() or target.is_symlink():
            info = target.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise WorkspacePathError("symlink_not_allowed")
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise WorkspacePathError("regular_file_required")
            if not overwrite:
                raise WorkspaceError("file_exists")
            mode = stat.S_IMODE(info.st_mode)
        else:
            mode = 0o600
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=target.parent, prefix=f".{target.name}.", delete=False
            ) as temporary:
                temporary_name = temporary.name
                temporary.write(data)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.chmod(temporary_name, mode)
            os.replace(temporary_name, target)
        finally:
            if temporary_name is not None:
                Path(temporary_name).unlink(missing_ok=True)

    def write_file(
        self,
        path: str,
        content: str | bytes,
        *,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Atomically write one non-protected workspace file."""

        relative, target = self._target(path)
        if self._is_protected(relative):
            raise WorkspacePathError("protected_file")
        data = content.encode("utf-8") if isinstance(content, str) else bytes(content)
        if len(data) > self.limits.max_file_bytes:
            raise WorkspaceLimitError("file_too_large")
        existed = target.exists() or target.is_symlink()
        self._atomic_write(target, data, overwrite=overwrite)
        return {
            "path": relative.as_posix(),
            "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "overwritten": overwrite and existed,
        }

    def edit_file(
        self,
        path: str,
        old_string: str,
        new_string: str,
        *,
        replace_all: bool = False,
    ) -> dict[str, Any]:
        """Replace a unique text span, or all spans when explicitly requested."""

        relative, target = self._target(path, existing=True)
        if self._is_protected(relative):
            raise WorkspacePathError("protected_file")
        data = self._read_bounded(target, max_bytes=self.limits.max_file_bytes)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WorkspaceError("file_not_utf8") from exc
        if not old_string:
            raise WorkspaceError("edit_old_string_empty")
        count = text.count(old_string)
        if count == 0:
            raise WorkspaceError("edit_match_not_found")
        if count != 1 and not replace_all:
            raise WorkspaceError("edit_match_not_unique")
        updated = text.replace(old_string, new_string, -1 if replace_all else 1)
        updated_bytes = updated.encode("utf-8")
        if len(updated_bytes) > self.limits.max_file_bytes:
            raise WorkspaceLimitError("file_too_large")
        self._atomic_write(target, updated_bytes, overwrite=True)
        return {
            "path": relative.as_posix(),
            "replacements": count,
            "size_bytes": len(updated_bytes),
            "sha256": hashlib.sha256(updated_bytes).hexdigest(),
        }

    def snapshot(self) -> dict[str, Any]:
        """Capture a deterministic, bounded base64 file manifest."""

        files: list[dict[str, str]] = []
        total = 0
        entries = self._iter_snapshot_files()
        if len(entries) > self.limits.max_snapshot_files:
            raise WorkspaceLimitError("snapshot_file_count_exceeded")
        for relative, path in entries:
            data = self._read_bounded(path, max_bytes=self.limits.max_file_bytes)
            total += len(data)
            if total > self.limits.max_snapshot_total_bytes:
                raise WorkspaceLimitError("snapshot_total_bytes_exceeded")
            files.append(
                {
                    "path": relative.as_posix(),
                    "content_b64": base64.b64encode(data).decode("ascii"),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            )
        snapshot: dict[str, Any] = {
            "schema_version": 1,
            "files": files,
            "total_bytes": total,
        }
        if _json_size(snapshot) > self.limits.max_snapshot_bytes:
            raise WorkspaceLimitError("snapshot_bytes_exceeded")
        return snapshot

    def restore_snapshot(self, snapshot: Mapping[str, Any] | str | bytes) -> dict[str, Any]:
        """Validate every entry, then atomically restore the allowed files.

        Validation is complete before the first destination is replaced.  A
        snapshot cannot overwrite credentials, ``.agentcore-*`` files, or the
        Git runtime configuration; those files remain owned by the VM/control
        plane.
        """

        if isinstance(snapshot, bytes):
            try:
                decoded: Any = json.loads(snapshot.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise WorkspaceSnapshotError("snapshot_json_invalid") from exc
        elif isinstance(snapshot, str):
            try:
                decoded = json.loads(snapshot)
            except json.JSONDecodeError as exc:
                raise WorkspaceSnapshotError("snapshot_json_invalid") from exc
        else:
            decoded = snapshot
        if not isinstance(decoded, Mapping):
            raise WorkspaceSnapshotError("snapshot_invalid")
        if decoded.get("schema_version") != 1:
            raise WorkspaceSnapshotError("snapshot_schema_invalid")
        if _json_size(decoded) > self.limits.max_snapshot_bytes:
            raise WorkspaceLimitError("snapshot_bytes_exceeded")
        entries = decoded.get("files")
        if not isinstance(entries, list):
            raise WorkspaceSnapshotError("snapshot_files_invalid")
        if len(entries) > self.limits.max_snapshot_files:
            raise WorkspaceLimitError("snapshot_file_count_exceeded")

        validated: list[_SnapshotFile] = []
        seen: set[str] = set()
        total = 0
        for entry in entries:
            if not isinstance(entry, Mapping):
                raise WorkspaceSnapshotError("snapshot_entry_invalid")
            path_value = entry.get("path")
            encoded = entry.get("content_b64")
            digest = entry.get("sha256")
            if not isinstance(path_value, str) or not isinstance(encoded, str):
                raise WorkspaceSnapshotError("snapshot_entry_invalid")
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise WorkspaceSnapshotError("snapshot_hash_invalid")
            relative = self._relative_path(path_value)
            normalized = relative.as_posix()
            if normalized in seen:
                raise WorkspaceSnapshotError("snapshot_duplicate_path")
            seen.add(normalized)
            if self._is_snapshot_forbidden(relative):
                raise WorkspaceSnapshotError("snapshot_protected_path")
            try:
                content = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise WorkspaceSnapshotError("snapshot_encoding_invalid") from exc
            if len(content) > self.limits.max_file_bytes:
                raise WorkspaceLimitError("file_too_large")
            total += len(content)
            if total > self.limits.max_snapshot_total_bytes:
                raise WorkspaceLimitError("snapshot_total_bytes_exceeded")
            if hashlib.sha256(content).hexdigest() != digest:
                raise WorkspaceSnapshotError("snapshot_hash_mismatch")
            _, target = self._target(normalized)
            if target.exists() or target.is_symlink():
                info = target.lstat()
                if stat.S_ISLNK(info.st_mode):
                    raise WorkspacePathError("symlink_not_allowed")
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise WorkspacePathError("restore_target_not_regular")
            validated.append(_SnapshotFile(path=normalized, content=content))

        if isinstance(decoded.get("total_bytes"), int) and decoded["total_bytes"] != total:
            raise WorkspaceSnapshotError("snapshot_total_mismatch")
        # Every parent and destination was checked before any replacement.
        for item in validated:
            _, target = self._target(item.path)
            self._atomic_write(target, item.content, overwrite=True)
        return {"restored_files": len(validated), "total_bytes": total}

    @staticmethod
    def _kill_group(proc: asyncio.subprocess.Process, sig: int) -> None:
        try:
            os.killpg(proc.pid, sig)
        except (PermissionError, ProcessLookupError):
            # A very short-lived shell can exit between the reader and waiter
            # tasks.  Fall back to the exact child PID; never signal an
            # unrelated process group after the group has disappeared.
            if proc.returncode is None:
                if sig == signal_module.SIGKILL:
                    proc.kill()
                else:
                    proc.terminate()

    async def _terminate_and_wait(
        self,
        proc: asyncio.subprocess.Process,
        *,
        collector: asyncio.Task[None] | None = None,
        grace_seconds: float = 2.0,
    ) -> None:
        self._kill_group(proc, signal_module.SIGTERM)
        try:
            await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=grace_seconds)
        except asyncio.TimeoutError:
            self._kill_group(proc, signal_module.SIGKILL)
            await asyncio.shield(proc.wait())
        # Reap the leader first, then signal the original group again.  A
        # shell can exit while a background child still owns stdout; checking
        # proc.returncode before killpg would leave that child running.
        self._kill_group(proc, signal_module.SIGKILL)
        if collector is None or collector.done():
            return
        try:
            await asyncio.wait_for(asyncio.shield(collector), timeout=grace_seconds)
        except asyncio.TimeoutError:
            self._kill_group(proc, signal_module.SIGKILL)
            try:
                await asyncio.wait_for(asyncio.shield(collector), timeout=grace_seconds)
            except asyncio.TimeoutError:
                collector.cancel()
                try:
                    await collector
                except asyncio.CancelledError:
                    pass

    async def execute(
        self,
        command: str,
        *,
        cwd: str | os.PathLike[str] | None = None,
        timeout_seconds: float = 120.0,
        cancel: asyncio.Event | None = None,
    ) -> dict[str, Any]:
        """Run ``bash -c`` in an independent process group.

        ``cancel`` is the CubeLoop cancellation event.  Both timeout and
        cancellation terminate the whole process group and wait for it before
        returning or propagating cancellation.
        """

        if not isinstance(command, str) or not command.strip():
            raise WorkspaceError("command_empty")
        if len(command.encode()) > self.limits.max_command_bytes:
            raise WorkspaceLimitError("command_too_large")
        if timeout_seconds <= 0 or timeout_seconds > self.limits.max_timeout_seconds:
            raise WorkspaceLimitError("timeout_out_of_range")
        working_directory = (
            self.root if cwd is None else self._target(cwd, existing=True, directory=True)[1]
        )
        process = await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            command,
            cwd=working_directory,
            env=os.environ.copy(),
            start_new_session=True,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        assert process.stdout is not None
        output = bytearray()
        output_capped = False

        async def collect() -> None:
            nonlocal output_capped
            stdout = process.stdout
            assert stdout is not None
            while True:
                chunk = await stdout.read(8192)
                if not chunk:
                    return
                remaining = self.limits.max_output_bytes - len(output)
                if remaining > 0:
                    output.extend(chunk[:remaining])
                if len(chunk) > max(remaining, 0):
                    output_capped = True
                    return

        collector = asyncio.create_task(collect())
        waiter = asyncio.create_task(process.wait())
        cancel_waiter = asyncio.create_task(cancel.wait()) if cancel is not None else None
        timed_out = False
        cancelled = False
        try:
            deadline = asyncio.get_running_loop().time() + timeout_seconds
            while True:
                if cancel_waiter is not None and cancel_waiter.done() and cancel_waiter.result():
                    cancelled = True
                    await self._terminate_and_wait(process, collector=collector)
                    break
                if waiter.done():
                    # A completed shell must not leave an asynchronous child
                    # running with inherited stdout/stderr handles.
                    timed_out = not collector.done()
                    await self._terminate_and_wait(process, collector=collector)
                    break
                if collector.done() and output_capped:
                    await self._terminate_and_wait(process, collector=collector)
                    break
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    timed_out = True
                    await self._terminate_and_wait(process, collector=collector)
                    break
                pending = {waiter, collector}
                if cancel_waiter is not None:
                    pending.add(cancel_waiter)
                done, _ = await asyncio.wait(
                    pending,
                    timeout=remaining,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    timed_out = True
                    await self._terminate_and_wait(process, collector=collector)
                    break
            return {
                "exit_code": process.returncode,
                "stdout": output.decode("utf-8", errors="replace"),
                "timed_out": timed_out,
                "cancelled": cancelled,
                "output_capped": output_capped,
            }
        except asyncio.CancelledError:
            await asyncio.shield(self._terminate_and_wait(process, collector=collector))
            raise
        finally:
            if cancel_waiter is not None and not cancel_waiter.done():
                cancel_waiter.cancel()
            if cancel_waiter is not None:
                try:
                    await cancel_waiter
                except asyncio.CancelledError:
                    pass
            if not collector.done():
                collector.cancel()
                try:
                    await collector
                except asyncio.CancelledError:
                    pass
            if not waiter.done():
                await waiter
