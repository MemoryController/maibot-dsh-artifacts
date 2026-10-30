"""Host-owned task capabilities and persistent, at-most-once DSH handoff.

Only Host calls issue/grant. Agent input never selects a chat. A request is
durably claimed before work, and each side effect has a write-ahead progress
record. Replaying interrupted work reports uncertainty without repeating it.
Trust the Host data directory and other processes sharing its operating UID.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import stat
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .artifact_safe_files import SafetyError, Workspace

_TASK = re.compile(r"[0-9a-f]{32}")
_REQUEST = re.compile(r"[A-Za-z0-9_-]{1,128}")
_TOKEN = re.compile(r"dshh_[0-9a-f]{64}")
_FILE = re.compile(r"[0-9a-f]{32}")
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_INITIALIZED = b"dsh-handoff-initialized-v1"
_REDACTED = "[redacted-handoff-token]"


def redact_tokens(value: Any) -> Any:
    """Return a recursively copied value without printable handoff tokens."""
    if isinstance(value, str):
        return _TOKEN.sub(_REDACTED, value)
    if isinstance(value, dict):
        return {redact_tokens(key): redact_tokens(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_tokens(item) for item in value]
    return value


class StreamRedactor:
    """Redact natural ACP chunks without releasing a partial token prefix.

    One instance belongs to one ordered stream. The longest incomplete token
    occupies 68 characters; complete tokens are 69 characters including dshh_.
    Call flush when the stream ends, including an interrupted turn.
    """

    def __init__(self) -> None:
        self._pending = ""

    def feed(self, text: str) -> str:
        value = _TOKEN.sub(_REDACTED, self._pending + text)
        self._pending = ""
        for length in range(min(68, len(value)), 0, -1):
            suffix = value[-length:]
            if (length < 5 and "dshh_".startswith(suffix)) or (
                length >= 5 and suffix.startswith("dshh_") and all(char in "0123456789abcdef" for char in suffix[5:])
            ):
                self._pending = suffix
                return value[:-length]
        return value

    def flush(self) -> str:
        value = _REDACTED if len(self._pending) >= 5 else self._pending
        self._pending = ""
        return value


class HandoffError(ValueError):
    """Stable public failure; never include backend exception text."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class HandoffConfig:
    workspace: str
    max_file_bytes: int = 52_428_800
    max_batch_files: int = 8
    ttl_seconds: int = 3600
    allow_send: bool = False

    def __post_init__(self) -> None:
        if (
            not isinstance(self.workspace, str)
            or not self.workspace.startswith("/")
            or type(self.max_file_bytes) is not int
            or not 1 <= self.max_file_bytes <= 1024**3
            or type(self.max_batch_files) is not int
            or not 1 <= self.max_batch_files <= 32
            or type(self.ttl_seconds) is not int
            or not 1 <= self.ttl_seconds <= 86_400
            or type(self.allow_send) is not bool
        ):
            raise HandoffError("invalid_config")


async def _io(function: Callable[..., Any], *args: Any) -> Any:
    """Finish file/SQLite workers before propagating cancellation."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except (Exception, asyncio.CancelledError):
                pass
        # Retrieve any worker exception so it is not logged as unhandled.
        if not task.cancelled():
            task.exception()
        raise


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def _code(error: Exception) -> str:
    if isinstance(error, (HandoffError, SafetyError)):
        return error.code
    if isinstance(error, FileNotFoundError):
        return "not_found"
    if isinstance(error, PermissionError):
        return "permission_denied"
    return "operation_failed"


class HandoffBroker:
    def __init__(
        self,
        state_dir: Path,
        agent_id: str,
        config: HandoffConfig,
        backend: Any,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.state_dir = Path(state_dir)
        if not self.state_dir.is_absolute() or not isinstance(agent_id, str) or not agent_id:
            raise HandoffError("invalid_config")
        state_root, workspace_root = os.path.abspath(self.state_dir), os.path.abspath(config.workspace)
        if os.path.commonpath((state_root, workspace_root)) in {state_root, workspace_root}:
            raise HandoffError("invalid_config")
        self.agent_id, self.config, self.backend, self.clock = agent_id, config, backend, clock
        self._config_hash = hashlib.sha256(_json(asdict(config)).encode()).hexdigest()

    def _private_directory(self) -> None:
        # Walk every component without following symlinks, including parents.
        parts = str(self.state_dir).split("/")[1:]
        if not parts or any(part in ("", ".", "..") for part in parts):
            raise HandoffError("unsafe_state")
        fd = os.open("/", _DIRECTORY)
        try:
            for part in parts:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                child = os.open(part, _DIRECTORY, dir_fd=fd)
                os.close(fd)
                fd = child
            info = os.fstat(fd)
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise HandoffError("unsafe_state")
        finally:
            os.close(fd)

    def _private_file(self, name: str, initial: bytes = b"") -> bytes:
        path = self.state_dir / name
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        try:
            fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
        except FileExistsError:
            fd = os.open(path, flags)
            created = False
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise HandoffError("unsafe_state")
            if created and initial:
                os.write(fd, initial)
                os.fsync(fd)
            os.lseek(fd, 0, os.SEEK_SET)
            return os.read(fd, 33) if name == "handoff.key" else b""
        finally:
            os.close(fd)

    def _db(self, operation: Callable[[sqlite3.Connection, bytes], Any]) -> Any:
        self._private_directory()
        # Serialize initialization as well as SQLite access across broker instances.
        self._private_file("handoff.lock")
        lock = os.open(self.state_dir / "handoff.lock", os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            marker = os.read(lock, 128)
            present = [(self.state_dir / name).exists() for name in ("handoff.key", "handoff.sqlite3")]
            fresh = marker == b"" and not any(present)
            if not fresh and (marker != _INITIALIZED or not all(present)):
                raise HandoffError("state_incomplete")
            key = self._private_file("handoff.key", secrets.token_bytes(32))
            if len(key) != 32:
                raise HandoffError("unsafe_state")
            self._private_file("handoff.sqlite3")
            with closing(sqlite3.connect(self.state_dir / "handoff.sqlite3", timeout=10)) as connection:
                connection.row_factory = sqlite3.Row
                if (
                    not fresh
                    and connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'"
                    ).fetchone()
                    is None
                ):
                    raise HandoffError("state_incomplete")
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        "CREATE TABLE IF NOT EXISTS tasks (task_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, "
                        "agent_id TEXT NOT NULL, expires_at REAL NOT NULL, send_allowed INTEGER NOT NULL, "
                        "config_hash TEXT, request_id TEXT, payload_hash TEXT, state TEXT, response TEXT)"
                    )
                    if "config_hash" not in {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}:
                        # Old grants cannot silently authorize a new workspace/configuration.
                        connection.execute("ALTER TABLE tasks ADD COLUMN config_hash TEXT")
                if fresh:
                    os.lseek(lock, 0, os.SEEK_SET)
                    os.write(lock, _INITIALIZED)
                    os.fsync(lock)
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    return operation(connection, key)
        finally:
            os.close(lock)

    def _task(self, db: sqlite3.Connection, task_id: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None or row["agent_id"] != self.agent_id:
            raise HandoffError("unauthorized")
        if row["config_hash"] != self._config_hash:
            raise HandoffError("config_changed")
        if self.clock() >= row["expires_at"]:
            raise HandoffError("expired")
        return row

    @staticmethod
    def _token(row: sqlite3.Row, key: bytes) -> str:
        claims = {
            name: row[name] for name in ("task_id", "chat_id", "agent_id", "expires_at", "send_allowed", "config_hash")
        }
        return "dshh_" + hmac.new(key, _json(claims).encode(), hashlib.sha256).hexdigest()

    async def issue(self, task_id: str, chat_id: str, send_allowed: bool = False) -> None:
        if (
            not isinstance(task_id, str)
            or _TASK.fullmatch(task_id) is None
            or not isinstance(chat_id, str)
            or not chat_id
            or type(send_allowed) is not bool
        ):
            raise HandoffError("invalid_arguments")
        if send_allowed and not self.config.allow_send:
            raise HandoffError("send_forbidden")
        try:
            await self.backend.require_chat(chat_id)
        except Exception:
            raise HandoffError("chat_unavailable") from None

        def insert(db: sqlite3.Connection, key: bytes) -> None:
            existing = db.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            allowed = bool(send_allowed and self.config.allow_send)
            if existing:
                if (existing["chat_id"], existing["agent_id"], bool(existing["send_allowed"])) != (
                    chat_id,
                    self.agent_id,
                    allowed,
                ):
                    raise HandoffError("conflict")
                if existing["config_hash"] != self._config_hash:
                    raise HandoffError("config_changed")
                return
            db.execute(
                "INSERT INTO tasks(task_id,chat_id,agent_id,expires_at,send_allowed,config_hash) VALUES(?,?,?,?,?,?)",
                (
                    task_id,
                    chat_id,
                    self.agent_id,
                    self.clock() + self.config.ttl_seconds,
                    int(allowed),
                    self._config_hash,
                ),
            )

        await _io(self._db, insert)

    async def grant(self, task_id: str) -> dict[str, Any]:
        def read(db: sqlite3.Connection, key: bytes) -> dict[str, Any]:
            row = self._task(db, task_id)
            return {
                "task_id": task_id,
                "token": self._token(row, key),
                "expires_at": row["expires_at"],
                "outbox_prefix": f"outbox/{task_id}/",
                "send_allowed": bool(row["send_allowed"]),
            }

        return await _io(self._db, read)

    def _validate(self, args: dict[str, Any], caller: str) -> dict[str, Any]:
        if (
            not isinstance(args, dict)
            or set(args) - {"task_id", "token", "request_id", "paths", "send"}
            or caller != self.agent_id
        ):
            raise HandoffError("unauthorized")
        task, token, request, paths, send = (
            args.get(name) for name in ("task_id", "token", "request_id", "paths", "send")
        )
        send = False if "send" not in args else send
        if (
            not isinstance(task, str)
            or _TASK.fullmatch(task) is None
            or not isinstance(token, str)
            or _TOKEN.fullmatch(token) is None
            or not isinstance(request, str)
            or _REQUEST.fullmatch(request) is None
            or _TOKEN.search(request) is not None
            or not isinstance(paths, list)
            or not 1 <= len(paths) <= self.config.max_batch_files
            or any(not isinstance(path, str) for path in paths)
            or len(set(paths)) != len(paths)
            or type(send) is not bool
        ):
            raise HandoffError("invalid_arguments")
        prefix = f"outbox/{task}/"
        for path in paths:
            if (
                not path.startswith(prefix)
                or _TOKEN.search(path) is not None
                or len(path) > 4096
                or len(path.split("/")) > 32
                or "\\" in path
                or any(ord(char) < 32 or ord(char) == 127 for char in path)
                or any(part in ("", ".", "..") for part in path.split("/"))
            ):
                raise HandoffError("invalid_path")
        return {"task_id": task, "request_id": request, "paths": paths, "send": send}

    def _claim(self, args: dict[str, Any], payload: dict[str, Any]) -> tuple[str, dict[str, Any], bool]:
        def claim(db: sqlite3.Connection, key: bytes) -> tuple[str, dict[str, Any], bool]:
            row = self._task(db, payload["task_id"])
            if not hmac.compare_digest(args["token"], self._token(row, key)):
                raise HandoffError("unauthorized")
            if payload["send"] and not (self.config.allow_send and row["send_allowed"]):
                raise HandoffError("send_forbidden")
            digest = hashlib.sha256(_json(payload).encode()).hexdigest()
            if row["request_id"] is not None:
                if row["request_id"] != payload["request_id"] or row["payload_hash"] != digest:
                    raise HandoffError("conflict")
                return row["chat_id"], json.loads(row["response"]), False
            response = {
                "success": False,
                "task_id": payload["task_id"],
                "request_id": payload["request_id"],
                "state": "unknown",
                "items": [{"path": path, "status": "unknown"} for path in payload["paths"]],
            }
            if payload["send"]:
                for item in response["items"]:
                    item["send"] = {"status": "unknown", "retry_safe": False}
            db.execute(
                "UPDATE tasks SET request_id=?,payload_hash=?,state='running',response=? WHERE task_id=?",
                (payload["request_id"], digest, _json(response), payload["task_id"]),
            )
            return row["chat_id"], response, True

        return self._db(claim)

    async def _save(self, response: dict[str, Any], *, done: bool = False, live: bool = False) -> None:
        def save(db: sqlite3.Connection, key: bytes) -> None:
            if live:
                self._task(db, response["task_id"])
            db.execute(
                "UPDATE tasks SET response=?,state=? WHERE task_id=? AND request_id=?",
                (_json(response), "done" if done else "running", response["task_id"], response["request_id"]),
            )

        await _io(self._db, save)

    def _snapshot(self, path: str, stage: Path) -> dict[str, Any]:
        stage.mkdir(mode=0o700)
        with Workspace(self.config.workspace) as workspace:
            return workspace.snapshot(path, stage / "payload", self.config.max_file_bytes)

    async def handoff(self, arguments: dict[str, Any], caller_agent_id: str) -> dict[str, Any]:
        try:
            payload = self._validate(arguments, caller_agent_id)
            chat, response, fresh = await _io(self._claim, arguments, payload)
            if not fresh:
                return response
            for index, path in enumerate(payload["paths"]):
                item = response["items"][index]
                stage = self.state_dir / (".stage-" + secrets.token_hex(16))
                try:
                    local = await _io(self._snapshot, path, stage)
                    await self.backend.require_chat(chat)
                    item.update(status="unknown", operation="create")
                    await self._save(response, live=True)  # Durable before the non-idempotent operation.
                    remote = await self.backend.create(chat, local["name"], stage / "payload")
                    if not isinstance(remote, dict) or not isinstance(remote.get("file_ref"), str):
                        raise HandoffError("invalid_backend_result")
                    if _FILE.fullmatch(remote["file_ref"]) is None:
                        raise HandoffError("invalid_backend_result")
                    file = {**local, "file_ref": remote["file_ref"]}
                    item.update(status="imported", file=file)
                    item.pop("operation", None)
                    await self._save(response)
                    if payload["send"]:
                        await self.backend.require_chat(chat)
                        item["send"] = {"status": "unknown", "retry_safe": False}
                        await self._save(response, live=True)
                        receipt = await self.backend.send(file["file_ref"], chat)
                        status = receipt.get("status") if isinstance(receipt, dict) else "unknown"
                        item["send"]["status"] = status if status in {"sent", "unknown"} else "unknown"
                except Exception as error:
                    item["error"] = {"code": _code(error)}
                    if "operation" not in item and item["status"] != "imported":
                        item["status"] = "failed"
                finally:
                    await _io(shutil.rmtree, stage, True)
                await self._save(response)
            response["state"] = "done"
            response["success"] = all(
                item["status"] == "imported"
                and not item.get("error")
                and item.get("send", {}).get("status", "sent") == "sent"
                for item in response["items"]
            )
            await self._save(response, done=True)
            return response
        except Exception as error:
            return {"success": False, "error": {"code": _code(error)}}
