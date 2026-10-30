"""Controlled request-file confirmation within a real Host file-tool scope.

The request digest is an exact-content confirmation, not a chat credential.
Only the caller's current Host scope chooses where Handoff imports the files.
There is deliberately no watcher, background upload, target selector or send.
"""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from typing import Any

if __package__:
    from .handoff import Handoff, _io, validate_manifest
    from .safe_files import SafetyError, Workspace
else:
    from handoff import Handoff, _io, validate_manifest
    from safe_files import SafetyError, Workspace

MAX_REQUEST_BYTES = 64 * 1024
_TASK = re.compile(r"[0-9a-f]{32}")
_SHA = re.compile(r"[0-9a-f]{64}")


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SafetyError("invalid_request", "Duplicate JSON keys are forbidden")
        result[key] = value
    return result


def _constant(value: str) -> Any:
    raise SafetyError("invalid_request", "Non-finite JSON values are forbidden")


def load_request(workspace: str, runtime_dir: Path, task_id: str, request_sha256: str) -> dict[str, Any]:
    """Read one bounded private snapshot; never reopen a shared path after checking."""
    if not isinstance(task_id, str) or _TASK.fullmatch(task_id) is None:
        raise SafetyError("invalid_task_id", "Expected a lowercase 32-hex task ID")
    if not isinstance(request_sha256, str) or _SHA.fullmatch(request_sha256) is None:
        raise SafetyError("invalid_request_sha256", "An explicit lowercase SHA-256 is required")
    relative = f"outbox/.dsh-handoffs/{task_id}.json"
    with tempfile.TemporaryDirectory(prefix="dsh-request-", dir=runtime_dir) as stage:
        snapshot = Path(stage) / "request.json"
        with Workspace(workspace) as shared:
            metadata = shared.snapshot(relative, snapshot, MAX_REQUEST_BYTES)
        if metadata["sha256"] != request_sha256:
            raise SafetyError("request_hash_mismatch", "Request differs from the explicitly confirmed bytes")
        try:
            manifest = json.loads(
                snapshot.read_text(encoding="utf-8"), object_pairs_hook=_object, parse_constant=_constant
            )
        except (ValueError, UnicodeError, RecursionError) as error:
            raise SafetyError("invalid_request", "Request is not strict UTF-8 JSON") from error
    manifest = validate_manifest(manifest, 32)
    if manifest["task_id"] != task_id:
        raise SafetyError("request_task_mismatch", "Request task ID differs from the explicitly selected task")
    return manifest


async def import_request(handoff: Handoff, task_id: str, request_sha256: str) -> dict[str, Any]:
    """Confirm exactly one request in the current actual tool scope; never send.

    Call this from the plugin tool while the Host-issued scope is alive. It does
    not create, extend, extract or accept a file scope or model-supplied chat ID.
    Handoff owns claiming, private artifact snapshots, upload and deduplication.
    """
    try:
        manifest = await _io(load_request, handoff.workspace, handoff.runtime_dir, task_id, request_sha256)
    except (ValueError, OSError, UnicodeError, RecursionError) as error:
        code = error.code if isinstance(error, SafetyError) else "request_unavailable"
        return {
            "success": False,
            "state": "unconfirmed",
            "tool_called": True,
            "registered": "unconfirmed",
            "delivery": "not_requested",
            "error": {"code": code},
        }
    return await handoff.import_manifest(manifest, send=False)
