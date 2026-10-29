"""Linux-only, descriptor-anchored access to the configured shared workspace.

Never resolve an untrusted path then reopen it by name. Directories and sources
are pinned with file descriptors, and publication uses an exclusive hard link.
The mount, its ancestors, directory owners and same-UID processes must be
trusted: filesystem permissions cannot isolate another process running as the
plugin's own UID, or an actor moving pinned directories/mounts out of the root.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
from pathlib import Path
from typing import Any

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_CHUNK = 256 * 1024


class SafetyError(ValueError):
    """A rejected filesystem request, with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _parts(path: str, area: str) -> list[str]:
    if (
        not isinstance(path, str)
        or not path
        or len(path) > 4096
        or path.startswith("/")
        or "\\" in path
        or any(ord(char) < 32 or ord(char) == 127 for char in path)
    ):
        raise SafetyError("invalid_path", "Use a relative workspace path")
    parts = path.split("/")
    if len(parts) < 2 or len(parts) > 32 or parts[0] != area or any(part in ("", ".", "..") for part in parts):
        raise SafetyError("invalid_path", f"Expected {area}/ followed by a file path without dot segments")
    if parts[-1] != parts[-1].strip() or len(parts[-1]) > 255:
        raise SafetyError("invalid_name", "Filename must have at most 255 characters and no surrounding whitespace")
    return parts


def _regular(fd: int) -> os.stat_result:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise SafetyError("unsafe_file", "Only regular files with one hard link are accepted")
    return info


def _fingerprint(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)


def _copy(fd: int, output: Any, max_bytes: int) -> dict[str, Any]:
    if type(max_bytes) is not int or max_bytes <= 0:
        raise SafetyError("invalid_limit", "max_bytes must be a positive integer")
    before = _regular(fd)
    if before.st_size > max_bytes:
        raise SafetyError("too_large", "File exceeds the configured size limit")
    size = 0
    digest = hashlib.sha256()
    while chunk := os.read(fd, min(_CHUNK, max_bytes - size + 1)):
        size += len(chunk)
        if size > max_bytes:
            raise SafetyError("too_large", "File exceeds the configured size limit")
        output.write(chunk)
        digest.update(chunk)
    after = _regular(fd)
    if size != before.st_size or _fingerprint(before) != _fingerprint(after):
        raise SafetyError("source_changed", "Source changed while being copied; finish writing before handoff")
    output.flush()
    os.fsync(output.fileno())
    return {"size": size, "sha256": digest.hexdigest()}


class Workspace:
    """Use as a context manager; no shell, extraction or file execution occurs."""

    def __init__(self, root: str | Path) -> None:
        self.root = os.fspath(root)
        self._root_fd: int | None = None

    def __enter__(self) -> Workspace:  # noqa: PYI034 -- keep Python 3.10 compatibility
        if self._root_fd is not None:
            raise RuntimeError("Workspace is already open")
        parts = self.root.split("/")
        if (
            not self.root.startswith("/")
            or self.root == "/"
            or any(part in ("", ".", "..") for part in parts[1:])
            or "\\" in self.root
            or any(ord(char) < 32 or ord(char) == 127 for char in self.root)
        ):
            raise SafetyError(
                "invalid_root", "Workspace root must be a non-root absolute directory without dot segments"
            )
        fd = os.open("/", _DIR_FLAGS)
        try:
            for part in parts[1:]:
                next_fd = os.open(part, _DIR_FLAGS, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            self._root_fd = fd
        except BaseException:
            os.close(fd)
            raise
        return self

    def __exit__(self, *exc: object) -> None:
        if self._root_fd is not None:
            os.close(self._root_fd)
            self._root_fd = None

    def _parent(self, parts: list[str], *, create: bool = False) -> int:
        if self._root_fd is None:
            raise RuntimeError("Workspace must be opened with a context manager")
        fd = os.dup(self._root_fd)
        try:
            for part in parts[:-1]:
                if create:
                    try:
                        os.mkdir(part, mode=0o770, dir_fd=fd)
                    except FileExistsError:
                        pass
                next_fd = os.open(part, _DIR_FLAGS, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            return fd
        except BaseException:
            os.close(fd)
            raise

    def snapshot(self, relative: str, destination: Path, max_bytes: int) -> dict[str, Any]:
        """Copy an outbox file into a caller-owned private staging directory."""
        parts = _parts(relative, "outbox")
        parent = self._parent(parts)
        source = None
        created = False
        try:
            source = os.open(parts[-1], _READ_FLAGS, dir_fd=parent)
            _regular(source)  # Reject FIFOs/devices before creating the staging file.
            destination_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
            created = True
            with os.fdopen(destination_fd, "wb") as output:
                metadata = _copy(source, output, max_bytes)
            return {"name": parts[-1], **metadata}
        except BaseException:
            if created:
                destination.unlink(missing_ok=True)
            raise
        finally:
            if source is not None:
                os.close(source)
            os.close(parent)

    def validate_target(self, relative: str) -> None:
        """Reject unsafe/existing export targets before requesting a download.

        This is an early check only: publish repeats traversal and exclusively
        creates the final entry, so a racing writer cannot bypass no-overwrite.
        """
        parts = _parts(relative, "inbox")
        try:
            parent = self._parent(parts)
        except FileNotFoundError:
            return  # Missing directories can be created safely during publish.
        try:
            try:
                os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                return
            raise FileExistsError("Export target already exists")
        finally:
            os.close(parent)

    def publish(self, source: Path, relative: str, max_bytes: int) -> dict[str, Any]:
        """Publish a completed download into inbox atomically, without overwrite.

        Staging is private even when DSH has group write permission on inbox.
        Failed downloads should never be passed here; SDK staging is kept outside
        the shared workspace until download verification has completed.
        """
        parts = _parts(relative, "inbox")
        parent = self._parent(parts, create=True)
        stage_name = ".dsh-" + secrets.token_hex(16) + ".part"
        stage_fd = None
        source_fd = None
        stage_created = False
        try:
            os.mkdir(stage_name, mode=0o700, dir_fd=parent)
            stage_created = True
            stage_fd = os.open(stage_name, _DIR_FLAGS, dir_fd=parent)
            stage_info = os.fstat(stage_fd)
            if stage_info.st_uid != os.geteuid() or stat.S_IMODE(stage_info.st_mode) & 0o077:
                raise SafetyError("unsafe_staging", "Publication staging must be private and owned by the plugin")
            source_fd = os.open(source, _READ_FLAGS)
            output_fd = os.open("payload", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600, dir_fd=stage_fd)
            with os.fdopen(output_fd, "wb") as output:
                metadata = _copy(source_fd, output, max_bytes)
                os.fchmod(output.fileno(), 0o640)
            # link() is atomic and fails for any existing entry, including symlinks.
            os.link("payload", parts[-1], src_dir_fd=stage_fd, dst_dir_fd=parent, follow_symlinks=False)
            return {"path": relative, "name": parts[-1], **metadata}
        finally:
            if source_fd is not None:
                os.close(source_fd)
            try:
                if stage_fd is not None:
                    try:
                        os.unlink("payload", dir_fd=stage_fd)
                    except FileNotFoundError:
                        pass
                    finally:
                        os.close(stage_fd)
                if stage_created:
                    os.rmdir(stage_name, dir_fd=parent)
            finally:
                os.close(parent)
