"""Stock SDK handoff: scoped file calls, private snapshots and a durable ledger.

A task ID is an idempotency key, not authorization from ACP or a source chat.
Only the first real tool scope determines where a new task is uploaded. Cached
results disclose references only after the current Host scope lists every one.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import stat
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

if __package__:
    from .image_safety import detect_upload_mime
    from .safe_files import SafetyError, Workspace, _parts
else:
    from image_safety import detect_upload_mime
    from safe_files import SafetyError, Workspace, _parts

_TASK = re.compile(r"[0-9a-f]{32}")
_SHA = re.compile(r"[0-9a-f]{64}")
_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_MARKER = b"stock-dsh-handoff-v1"
_SENTINEL = b"stock-dsh-handoff-root-v1"
_ERRORS = {
    "invalid_path",
    "invalid_name",
    "unsafe_file",
    "source_changed",
    "too_large",
    "not_found",
    "permission_denied",
    "hash_mismatch",
    "size_mismatch",
    "operation_failed",
    "invalid_upload_result",
}


def validate_manifest(manifest: dict[str, Any], max_batch_files: int) -> dict[str, Any]:
    """Return an independent canonical manifest or raise SafetyError."""
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"type", "version", "task_id", "files"}
        or manifest.get("type") != "dsh_artifacts"
        or type(manifest.get("version")) is not int
        or manifest["version"] != 1
        or not isinstance(manifest.get("task_id"), str)
        or _TASK.fullmatch(manifest["task_id"]) is None
        or type(max_batch_files) is not int
        or max_batch_files < 1
        or not isinstance(manifest.get("files"), list)
        or not 1 <= len(manifest["files"]) <= max_batch_files
    ):
        raise SafetyError("invalid_manifest", "Expected a version 1 dsh_artifacts manifest")
    files = []
    seen = set()
    for item in manifest["files"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"path", "size", "sha256"}
            or type(item.get("size")) is not int
            or item["size"] < 0
            or not isinstance(item.get("sha256"), str)
            or _SHA.fullmatch(item["sha256"]) is None
        ):
            raise SafetyError("invalid_manifest", "Each file needs path, size and SHA-256")
        _parts(item["path"], "outbox")
        if item["path"] in seen:
            raise SafetyError("invalid_manifest", "Duplicate file paths are not accepted")
        seen.add(item["path"])
        files.append({"path": item["path"], "size": item["size"], "sha256": item["sha256"]})
    return {"type": "dsh_artifacts", "version": 1, "task_id": manifest["task_id"], "files": files}


async def _io(function: Callable[..., Any], *args: Any) -> Any:
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except (Exception, asyncio.CancelledError):
                pass
        if not task.cancelled():
            task.exception()
        raise


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _unconfirmed() -> dict[str, Any]:
    return {
        "success": False,
        "state": "unconfirmed",
        "error": {"code": "confirmation_unavailable"},
        "tool_called": "unconfirmed",
        "registered": "unconfirmed",
        "delivery": "unconfirmed",
    }


def _code(error: Exception) -> str:
    if isinstance(error, SafetyError):
        return error.code if error.code in _ERRORS else "operation_failed"
    if isinstance(error, FileNotFoundError):
        return "not_found"
    if isinstance(error, PermissionError):
        return "permission_denied"
    return "operation_failed"


class Handoff:
    def __init__(
        self,
        files_api: Any,
        workspace: str,
        runtime_dir: Path,
        data_dir: Path,
        max_file_bytes: int,
        max_batch_files: int,
    ) -> None:
        self.files = files_api
        self.workspace = workspace
        self.runtime_dir = Path(runtime_dir)
        self.data_dir = Path(data_dir)
        self.ledger = self.data_dir / "handoff"
        if (
            not isinstance(workspace, str)
            or not Path(workspace).is_absolute()
            or not self.runtime_dir.is_absolute()
            or not self.ledger.is_absolute()
            or type(max_file_bytes) is not int
            or not 1 <= max_file_bytes <= 1024**3
            or type(max_batch_files) is not int
            or not 1 <= max_batch_files <= 32
        ):
            raise SafetyError("invalid_config", "Invalid handoff configuration")
        root = os.path.abspath(workspace)
        for private in (self.data_dir, self.ledger, self.runtime_dir):
            location = os.path.abspath(private)
            if os.path.commonpath((root, location)) in {root, location}:
                raise SafetyError("invalid_config", "Keep private directories separate from the workspace")
        self.max_file_bytes, self.max_batch_files = max_file_bytes, max_batch_files
        self._configuration = {"workspace": root, "max_file_bytes": max_file_bytes, "max_batch_files": max_batch_files}

    @staticmethod
    def _directory(path: Path, create: bool, private: bool = False) -> None:
        parts = str(path).split("/")[1:]
        if not parts or any(part in ("", ".", "..") for part in parts):
            raise SafetyError("unsafe_state", "Unsafe private directory")
        fd = os.open("/", _DIR)
        try:
            for part in parts:
                if create:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                child = os.open(part, _DIR, dir_fd=fd)
                os.close(fd)
                fd = child
            info = os.fstat(fd)
            if info.st_uid != os.geteuid() or (stat.S_IMODE(info.st_mode) & (0o077 if private else 0o022)):
                raise SafetyError("unsafe_state", "Private directory ownership or permissions are unsafe")
        finally:
            os.close(fd)

    def _file(self, name: str, create: bool, writable: bool = False) -> int:
        flags = (os.O_RDWR if writable else os.O_RDONLY) | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        if create:
            try:
                fd = os.open(self.ledger / name, flags | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                fd = os.open(self.ledger / name, flags)
        else:
            fd = os.open(self.ledger / name, flags)
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            os.close(fd)
            raise SafetyError("unsafe_state", "Unsafe ledger file")
        return fd

    def _initialized(self) -> bool:
        try:
            self._directory(self.data_dir, False)
            fd = os.open(self.data_dir / ".handoff-initialized", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return False
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or os.read(fd, len(_SENTINEL) + 1) != _SENTINEL
            ):
                raise SafetyError("unsafe_state", "Unsafe initialization sentinel")
            return True
        finally:
            os.close(fd)

    def _initialize_sentinel(self) -> None:
        fd = os.open(
            self.data_dir / ".handoff-initialized", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        try:
            os.write(fd, _SENTINEL)
            os.fsync(fd)
        finally:
            os.close(fd)
        parent = os.open(self.data_dir, _DIR)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)

    def _database(self, operation: Callable[[sqlite3.Connection], Any], create: bool) -> Any:
        initialized = self._initialized()
        self._directory(self.ledger, create and not initialized, private=True)
        lock = self._file("ledger.lock", create and not initialized, writable=create)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX if create else fcntl.LOCK_SH)
            initialized = self._initialized()  # Another first claimant may have finished while we waited.
            marker = os.read(lock, 128)
            present = os.path.lexists(self.ledger / "ledger.sqlite3")
            fresh = create and not initialized and marker == b"" and not present
            if not fresh and (not initialized or marker != _MARKER or not present):
                raise SafetyError("unsafe_state", "Incomplete ledger; do not recreate its history")
            fd = self._file("ledger.sqlite3", fresh, writable=create)
            os.close(fd)
            uri = (self.ledger / "ledger.sqlite3").as_uri() + ("?mode=rw" if create else "?mode=ro")
            with closing(sqlite3.connect(uri, uri=True, timeout=10)) as db:
                db.row_factory = sqlite3.Row
                if fresh:
                    with db:
                        db.execute(
                            "CREATE TABLE tasks(task_id TEXT PRIMARY KEY, "
                            "fingerprint TEXT NOT NULL, result TEXT NOT NULL)"
                        )
                    os.write(lock, _MARKER)
                    os.fsync(lock)
                    self._initialize_sentinel()
                elif (
                    db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'").fetchone() is None
                ):
                    raise SafetyError("unsafe_state", "Incomplete ledger")
                with db:
                    if create:
                        db.execute("BEGIN IMMEDIATE")
                    return operation(db)
        finally:
            os.close(lock)

    def _claim(self, manifest: dict[str, Any], send: bool) -> tuple[dict[str, Any], bool, bool]:
        fingerprint = self._fingerprint(manifest, send)

        def claim(db: sqlite3.Connection) -> tuple[dict[str, Any], bool, bool]:
            row = db.execute("SELECT * FROM tasks WHERE task_id=?", (manifest["task_id"],)).fetchone()
            if row is not None:
                return json.loads(row["result"]), False, row["fingerprint"] == fingerprint
            result = {
                "success": False,
                "task_id": manifest["task_id"],
                "state": "unknown",
                "tool_called": True,
                "registered": "unknown",
                "delivery": "unknown" if send else "not_requested",
                "items": [{"path": item["path"], "status": "unknown"} for item in manifest["files"]],
            }
            if send:
                for item in result["items"]:
                    item["send"] = {"status": "unknown", "retry_safe": False}
            db.execute("INSERT INTO tasks VALUES(?,?,?)", (manifest["task_id"], fingerprint, _json(result)))
            return result, True, True

        return self._database(claim, True)

    def _fingerprint(self, manifest: dict[str, Any], send: bool) -> str:
        return hashlib.sha256(_json({"manifest": manifest, "send": send, **self._configuration}).encode()).hexdigest()

    def _read(self, task_id: str) -> dict[str, Any] | None:
        def read(db: sqlite3.Connection) -> dict[str, Any] | None:
            row = db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            return {"fingerprint": row["fingerprint"], "result": json.loads(row["result"])} if row else None

        return self._database(read, False)

    async def _save(self, result: dict[str, Any]) -> None:
        def save(db: sqlite3.Connection) -> None:
            db.execute("UPDATE tasks SET result=? WHERE task_id=?", (_json(result), result["task_id"]))

        await _io(self._database, save, True)

    async def _cached(self, result: dict[str, Any], first_page: Any = None) -> dict[str, Any]:
        refs = {
            item["file"]["file_ref"]
            for item in result.get("items", [])
            if isinstance(item.get("file"), dict) and isinstance(item["file"].get("file_ref"), str)
        }
        if not refs:
            return _unconfirmed()
        visible: dict[str, Any] = {}
        try:
            for page_number in range(500):
                page = (
                    first_page
                    if page_number == 0 and first_page is not None
                    else await self.files.list(offset=page_number * 50)
                )
                if not isinstance(page, list) or len(page) > 50:
                    return _unconfirmed()
                for file in page:
                    if isinstance(file, dict) and file.get("file_ref") in refs and file.get("state") == "ready":
                        if file["file_ref"] not in visible or file["state"] == "ready":
                            visible[file["file_ref"]] = file
                if refs <= visible.keys():
                    break
                if len(page) < 50:
                    return _unconfirmed()
            else:
                return _unconfirmed()
        except Exception:
            return _unconfirmed()
        items = []
        for item in result["items"]:
            cached: dict[str, Any] = {"status": item["status"]}
            if isinstance(item.get("file"), dict):
                file = visible[item["file"]["file_ref"]]
                cached["file"] = {
                    key: file[key] for key in ("file_ref", "name", "size", "sha256", "mime_type") if key in file
                }
            if isinstance(item.get("error"), dict):
                code = item["error"].get("code")
                cached["error"] = {"code": code if code in _ERRORS else "operation_failed"}
            items.append(cached)
        registered = all(item["status"] == "imported" for item in items)
        return {
            "success": registered and result["state"] == "done" and result["delivery"] == "not_requested",
            "cached": True,
            "task_id": result["task_id"],
            "state": result["state"],
            "tool_called": True,
            "registered": "confirmed" if registered else "partial",
            "delivery": "unconfirmed",
            "items": items,
        }

    def _snapshot(self, item: dict[str, Any], stage: Path) -> dict[str, Any]:
        self._directory(self.runtime_dir, False)
        stage.mkdir(mode=0o700)
        if item["size"] > self.max_file_bytes:
            raise SafetyError("too_large", "Manifest size exceeds the configured limit")
        with Workspace(self.workspace) as workspace:
            local = workspace.snapshot(item["path"], stage / "payload", self.max_file_bytes)
        if local["size"] != item["size"]:
            raise SafetyError("size_mismatch", "Snapshot size differs from manifest")
        if local["sha256"] != item["sha256"]:
            raise SafetyError("hash_mismatch", "Snapshot hash differs from manifest")
        return local

    async def import_manifest(self, manifest: dict[str, Any], send: bool = False) -> dict[str, Any]:
        try:
            manifest = validate_manifest(manifest, 32)
            if type(send) is not bool:
                raise SafetyError("invalid_manifest", "send must be an explicit boolean")
        except Exception as error:
            return {
                "success": False,
                "error": {"code": error.code if isinstance(error, SafetyError) else "invalid_manifest"},
            }
        try:
            first_page = await self.files.list(offset=0)  # A real current tool scope is required before claiming.
            if not isinstance(first_page, list) or len(first_page) > 50:
                return _unconfirmed()
        except Exception:
            return _unconfirmed()
        try:
            try:
                previous = await _io(self._read, manifest["task_id"])
            except FileNotFoundError:
                previous = None  # A first claim still checks initialization markers atomically.
            if previous is not None:
                cached = await self._cached(previous["result"], first_page)
                if cached.get("cached") and previous["fingerprint"] != self._fingerprint(manifest, send):
                    return {"success": False, "state": "conflict", "error": {"code": "conflict"}}
                return cached
            if len(manifest["files"]) > self.max_batch_files:
                return {"success": False, "error": {"code": "invalid_manifest"}}
            result, fresh, matches = await _io(self._claim, manifest, send)
            if not fresh:
                cached = await self._cached(result, first_page)
                if cached.get("cached") and not matches:
                    return {"success": False, "state": "conflict", "error": {"code": "conflict"}}
                return cached
            for item, declaration in zip(result["items"], manifest["files"], strict=True):
                stage = self.runtime_dir / ("dsh-handoff-" + secrets.token_hex(16))
                try:
                    local = await _io(self._snapshot, declaration, stage)
                    local["mime_type"] = await _io(detect_upload_mime, stage / "payload")
                    item["operation"] = "upload"
                    await self._save(result)
                    remote = await self.files.upload(
                        stage / "payload", name=local["name"], mime_type=local["mime_type"]
                    )
                    if (
                        not isinstance(remote, dict)
                        or not isinstance(remote.get("file_ref"), str)
                        or _TASK.fullmatch(remote["file_ref"]) is None
                    ):
                        raise SafetyError("invalid_upload_result", "Upload did not return a valid file_ref")
                    item.update(status="imported", file={**local, "file_ref": remote["file_ref"]})
                    item.pop("operation", None)
                    await self._save(result)
                    if send:
                        item["send"] = {"status": "unknown", "retry_safe": False}
                        await self._save(result)
                        receipt = await self.files.send(remote["file_ref"])
                        status = receipt.get("status") if isinstance(receipt, dict) else "unknown"
                        item["send"]["status"] = status if status in {"sent", "unknown"} else "unknown"
                except Exception as error:
                    item["error"] = {"code": _code(error)}
                    if "operation" not in item and item["status"] != "imported":
                        item["status"] = "failed"
                finally:
                    await _io(shutil.rmtree, stage, True)
                await self._save(result)
            imported = sum(item["status"] == "imported" for item in result["items"])
            result["registered"] = (
                "confirmed"
                if imported == len(result["items"])
                else "partial"
                if imported
                else "unknown"
                if any(item["status"] == "unknown" for item in result["items"])
                else "not_registered"
            )
            result["delivery"] = (
                "not_requested"
                if not send
                else "sent"
                if all(item.get("send", {}).get("status") == "sent" for item in result["items"])
                else "unknown"
            )
            result["success"] = (
                imported == len(result["items"])
                and all(not item.get("error") for item in result["items"])
                and result["delivery"] != "unknown"
            )
            result["state"] = "done"
            await self._save(result)
            return result
        except Exception:
            return _unconfirmed()

    async def status(self, task_id: str) -> dict[str, Any]:
        if not isinstance(task_id, str) or _TASK.fullmatch(task_id) is None:
            return _unconfirmed()

        try:
            previous = await _io(self._read, task_id)
            return await self._cached(previous["result"]) if previous is not None else _unconfirmed()
        except Exception:
            return _unconfirmed()
