"""Real request reader, Handoff ledger and snapshots with scoped file API doubles.

These are isolated integration checks, not real Host HTTP or QQ validation.
"""

import asyncio
import hashlib
import json
import os
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from handoff import Handoff
from request import MAX_REQUEST_BYTES, import_request

TASK = "a" * 32


class ScopedFiles:
    def __init__(self):
        self.scope = "original-chat"
        self.assets = {"original-chat": {}, "other-chat": {}}
        self.uploads = []
        self.lists = []
        self.sends = []
        self.upload_raises = False

    async def list(self, offset=0):
        if self.scope is None:
            raise PermissionError("No real Host file scope")
        self.lists.append((self.scope, offset))
        return list(self.assets[self.scope].values())[offset : offset + 50]

    async def upload(self, path, *, name, mime_type="application/octet-stream"):
        if self.scope is None:
            raise PermissionError("No real Host file scope")
        data = Path(path).read_bytes()
        self.uploads.append((self.scope, name, data))
        ref = f"{len(self.uploads):032x}"
        metadata = {
            "file_ref": ref,
            "name": name,
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "state": "ready",
        }
        self.assets[self.scope][ref] = metadata
        if self.upload_raises:
            raise TimeoutError("Upload may have succeeded remotely")
        return metadata

    async def send(self, file_ref):
        self.sends.append(file_ref)
        raise AssertionError("Controlled confirmation must never send")


@pytest.fixture
def request_setup(tmp_path):
    workspace, runtime, data = (tmp_path / name for name in ("shared", "runtime", "data"))
    requests = workspace / "outbox" / ".dsh-handoffs"
    requests.mkdir(parents=True)
    runtime.mkdir(mode=0o700)
    binary = b"\x00\xff\x80\x00binary"
    (workspace / "outbox" / "中文产物.bin").write_bytes(binary)
    manifest = {
        "type": "dsh_artifacts",
        "version": 1,
        "task_id": TASK,
        "files": [{"path": "outbox/中文产物.bin", "size": len(binary), "sha256": hashlib.sha256(binary).hexdigest()}],
    }
    files = ScopedFiles()
    setup = SimpleNamespace(
        workspace=workspace,
        runtime=runtime,
        data=data,
        files=files,
        manifest=manifest,
        request=requests / f"{TASK}.json",
        broker=Handoff(files, str(workspace), runtime, data, 1024, 8),
        binary=binary,
    )
    setup.sha = write_request(setup)
    return setup


def write_request(setup, payload=None):
    if payload is None:
        payload = json.dumps(setup.manifest, ensure_ascii=False, separators=(",", ":")).encode()
    setup.request.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


async def confirm(setup, sha=None, task_id=TASK):
    return await import_request(setup.broker, task_id, setup.sha if sha is None else sha)


async def test_real_handoff_receipt_preserves_binary_and_never_sends(request_setup):
    s = request_setup
    result = await confirm(s)
    assert result["success"] and result["tool_called"] is True
    assert result["registered"] == "confirmed" and result["delivery"] == "not_requested"
    assert result["items"][0]["file"]["file_ref"] == f"{1:032x}"
    assert result["items"][0]["file"]["sha256"] == s.manifest["files"][0]["sha256"]
    assert s.files.uploads == [("original-chat", "中文产物.bin", s.binary)]
    assert not s.files.sends and not list(s.runtime.iterdir())


@pytest.mark.parametrize("task_id", ["../x", "", "A" * 32, "a" * 31, None, 1])
async def test_invalid_task_cannot_select_paths(request_setup, task_id):
    s = request_setup
    result = await confirm(s, task_id=task_id)
    assert result["error"]["code"] == "invalid_task_id"
    assert not s.data.exists() and not s.files.lists and not s.files.uploads


@pytest.mark.parametrize("sha", ["", "F" * 64, "f" * 63, "../x", 1])
async def test_explicit_sha_is_required(request_setup, sha):
    s = request_setup
    result = await confirm(s, sha=sha)
    assert result["error"]["code"] == "invalid_request_sha256"
    assert not s.data.exists() and not s.files.uploads


async def test_missing_sha_is_not_optional(request_setup):
    with pytest.raises(TypeError):
        await import_request(request_setup.broker, TASK)


@pytest.mark.parametrize(
    "payload",
    [b"{", b"[]", b"null", b"\xff", b"\xef\xbb\xbf{}", b'{"x":NaN}', b'{"x":1,"x":2}'],
)
async def test_malformed_request_never_claims_or_uploads(request_setup, payload):
    s = request_setup
    result = await confirm(s, write_request(s, payload))
    assert not result["success"] and result["registered"] == "unconfirmed"
    assert not s.data.exists() and not s.files.uploads and not list(s.runtime.iterdir())


async def test_digest_checks_exact_bytes_before_import(request_setup):
    s = request_setup
    s.request.write_bytes(s.request.read_bytes() + b"\n")
    result = await confirm(s)
    assert result["error"]["code"] == "request_hash_mismatch"
    assert not s.data.exists() and not s.files.uploads


async def test_matching_hash_allows_whitespace_but_not_semantic_replacement(request_setup):
    s = request_setup
    payload = json.dumps(s.manifest, ensure_ascii=False, indent=2).encode() + b"\n"
    result = await confirm(s, write_request(s, payload))
    assert result["registered"] == "confirmed"


async def test_task_in_request_must_match_selected_task(request_setup):
    s = request_setup
    s.manifest["task_id"] = "b" * 32
    result = await confirm(s, write_request(s))
    assert result["error"]["code"] == "request_task_mismatch"
    assert not s.data.exists() and not s.files.uploads


@pytest.mark.parametrize("field", ["send", "target", "chat_id", "session_id"])
async def test_manifest_cannot_carry_delivery_or_routing(request_setup, field):
    s = request_setup
    s.manifest[field] = True
    result = await confirm(s, write_request(s))
    assert result["error"]["code"] == "invalid_manifest"
    assert not s.data.exists() and not s.files.uploads and not s.files.sends


@pytest.mark.parametrize("kind", ["symlink", "directory_symlink", "hardlink", "fifo"])
async def test_unsafe_request_files_are_rejected(request_setup, kind):
    s = request_setup
    if kind == "directory_symlink":
        directory = s.request.parent
        moved = directory.with_name("moved")
        directory.rename(moved)
        directory.symlink_to(moved, target_is_directory=True)
    else:
        original = s.request.with_suffix(".original")
        s.request.rename(original)
        if kind == "symlink":
            s.request.symlink_to(original)
        elif kind == "hardlink":
            os.link(original, s.request)
        else:
            os.mkfifo(s.request)
    result = await confirm(s)
    assert not result["success"] and not s.files.uploads and not s.data.exists()
    assert not list(s.runtime.iterdir())


async def test_request_limit_is_applied_before_json_parse(request_setup):
    s = request_setup
    payload = s.request.read_bytes().ljust(MAX_REQUEST_BYTES + 1, b" ")
    result = await confirm(s, write_request(s, payload))
    assert result["error"]["code"] == "too_large"
    assert not s.data.exists() and not s.files.uploads


async def test_configured_batch_limit_still_applies(request_setup):
    s = request_setup
    s.broker = Handoff(s.files, str(s.workspace), s.runtime, s.data, 1024, 1)
    (s.workspace / "outbox" / "empty").write_bytes(b"")
    s.manifest["files"].append({"path": "outbox/empty", "size": 0, "sha256": hashlib.sha256(b"").hexdigest()})
    result = await confirm(s, write_request(s))
    assert result["error"]["code"] == "invalid_manifest"
    assert result["state"] == result["registered"] == "unconfirmed"
    assert result["tool_called"] is True and result["delivery"] == "not_requested"
    assert not s.files.uploads and not s.data.exists()


async def test_artifact_content_tampering_is_caught_by_real_core(request_setup):
    s = request_setup
    (s.workspace / "outbox" / "中文产物.bin").write_bytes(b"tampered")
    result = await confirm(s)
    assert result["registered"] == "not_registered" and not result["success"]
    assert not s.files.uploads and not s.files.sends


async def test_scope_denial_cannot_be_replaced_by_request_hash(request_setup):
    s = request_setup
    s.files.scope = None
    result = await confirm(s)
    assert result["registered"] == "unconfirmed"
    assert not s.data.exists() and not s.files.uploads


async def test_duplicate_and_restart_share_existing_handoff_ledger(request_setup):
    s = request_setup
    first = await confirm(s)
    second = await confirm(s)
    assert first["registered"] == second["registered"] == "confirmed"
    assert second["cached"] is True
    s.broker = Handoff(s.files, str(s.workspace), s.runtime, s.data, 1024, 8)
    restarted = await confirm(s)
    assert restarted["cached"] is True and len(s.files.uploads) == 1
    s.request.unlink()
    status = await s.broker.status(TASK)
    assert status["registered"] == "confirmed" and status["cached"] is True
    assert len(s.files.uploads) == 1 and not s.files.sends


async def test_new_request_cannot_replace_frozen_task_manifest(request_setup):
    s = request_setup
    await confirm(s)
    changed = deepcopy(s.manifest["files"][0])
    changed["path"] = "outbox/other.bin"
    (s.workspace / changed["path"]).write_bytes(s.binary)
    s.manifest["files"] = [changed]
    result = await confirm(s, write_request(s))
    assert result["state"] == "conflict" and len(s.files.uploads) == 1
    assert result["tool_called"] is True and result["registered"] == "unconfirmed"
    assert result["delivery"] == "not_requested"


async def test_another_scope_cannot_read_cached_refs_or_cause_reupload(request_setup):
    s = request_setup
    first = await confirm(s)
    ref = first["items"][0]["file"]["file_ref"]
    s.files.scope = "other-chat"
    result = await confirm(s)
    assert result["registered"] == "unconfirmed" and ref not in repr(result)
    assert result["state"] == result["tool_called"] == result["delivery"] == "unconfirmed"
    assert len(s.files.uploads) == 1


async def test_unknown_upload_is_not_repeated_after_confirmation_or_restart(request_setup):
    s = request_setup
    s.files.upload_raises = True
    first = await confirm(s)
    assert first["registered"] == "unknown"
    s.files.upload_raises = False
    await confirm(s)
    s.broker = Handoff(s.files, str(s.workspace), s.runtime, s.data, 1024, 8)
    await confirm(s)
    assert len(s.files.uploads) == 1 and not s.files.sends


async def test_concurrent_confirmation_attempts_upload_once(request_setup):
    s = request_setup
    results = await asyncio.gather(confirm(s), confirm(s), confirm(s))
    assert any(result["registered"] == "confirmed" for result in results)
    assert len(s.files.uploads) == 1 and not s.files.sends


async def test_plain_manifest_and_request_use_same_dedup_ledger(request_setup):
    s = request_setup
    await s.broker.import_manifest(s.manifest, send=False)
    result = await confirm(s)
    assert result["cached"] is True and len(s.files.uploads) == 1


@pytest.mark.parametrize("kind", ["group_writable", "symlink", "ancestor_symlink"])
async def test_unsafe_runtime_is_rejected_before_any_shared_snapshot(request_setup, monkeypatch, kind):
    import request as request_module

    s = request_setup
    actual_runtime = s.runtime
    if kind == "group_writable":
        s.runtime.chmod(0o770)
    elif kind == "symlink":
        actual_runtime = s.runtime.with_name("real-runtime")
        s.runtime.rename(actual_runtime)
        s.runtime.symlink_to(actual_runtime, target_is_directory=True)
    else:
        parent = s.runtime.with_name("real-parent")
        parent.mkdir()
        actual_runtime = parent / "runtime"
        s.runtime.rename(actual_runtime)
        linked_parent = parent.with_name("linked-parent")
        linked_parent.symlink_to(parent, target_is_directory=True)
        s.runtime = linked_parent / "runtime"
        s.broker.runtime_dir = s.runtime
    snapshots = []

    def forbidden_snapshot(*args):
        snapshots.append(args)
        raise AssertionError("Unsafe runtime must be rejected before reading shared request bytes")

    monkeypatch.setattr(request_module.Workspace, "snapshot", forbidden_snapshot)
    result = await confirm(s)
    assert result["success"] is False
    assert result["state"] == result["registered"] == "unconfirmed"
    assert result["tool_called"] is True and result["delivery"] == "not_requested"
    assert not snapshots and not list(actual_runtime.iterdir())
    assert not s.data.exists() and not s.files.lists and not s.files.uploads and not s.files.sends


async def test_owner_writable_755_runtime_remains_usable(request_setup):
    s = request_setup
    s.runtime.chmod(0o755)
    result = await confirm(s)
    assert result["success"] and result["registered"] == "confirmed"
    assert s.files.uploads == [("original-chat", "中文产物.bin", s.binary)]
    assert not list(s.runtime.iterdir()) and not s.files.sends


@pytest.mark.parametrize("failure", ["missing", "permission"])
async def test_request_read_failure_has_no_staging_or_file_service_side_effects(request_setup, monkeypatch, failure):
    s = request_setup
    if failure == "missing":
        s.request.unlink()
    else:
        original_open = os.open

        def permission_denied(path, flags, *args, **kwargs):
            # Inject the kernel's permission failure at the actual anchored source
            # open, independent of whether the test runner has elevated privileges.
            if os.fspath(path) == f"{TASK}.json" and kwargs.get("dir_fd") is not None:
                raise PermissionError("not readable")
            return original_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", permission_denied)
    result = await confirm(s)
    assert result["error"]["code"] == "request_unavailable"
    assert result["state"] == result["registered"] == "unconfirmed"
    assert result["tool_called"] is True and result["delivery"] == "not_requested"
    assert not list(s.runtime.iterdir()) and not s.data.exists()
    assert not s.files.lists and not s.files.uploads and not s.files.sends


async def test_request_snapshot_cancellation_waits_for_worker_and_removes_stage(request_setup, monkeypatch):
    import threading

    import request as request_module

    s = request_setup
    started, release = threading.Event(), threading.Event()
    original_snapshot = request_module.Workspace.snapshot
    stage = []

    def paused_snapshot(self, relative, destination, max_bytes):
        metadata = original_snapshot(self, relative, destination, max_bytes)
        stage.append(destination.parent)
        started.set()
        if not release.wait(5):
            raise TimeoutError("Test did not release the snapshot worker")
        return metadata

    monkeypatch.setattr(request_module.Workspace, "snapshot", paused_snapshot)
    task = asyncio.create_task(confirm(s))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(started.wait, 3), 4)
        assert stage[0].is_dir()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "Cancellation must wait until the snapshot worker releases its stage"
        assert stage[0].is_dir()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 4)
    finally:
        release.set()
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    assert not stage[0].exists() and not list(s.runtime.iterdir())
    assert not s.data.exists() and not s.files.lists and not s.files.uploads and not s.files.sends


@pytest.mark.parametrize("change", ["deleted", "replaced"])
async def test_lost_or_replaced_successful_request_is_unconfirmed_but_status_remains_read_only(request_setup, change):
    s = request_setup
    first = await confirm(s)
    ref = first["items"][0]["file"]["file_ref"]
    assert first["registered"] == "confirmed" and len(s.files.uploads) == 1
    if change == "deleted":
        s.request.unlink()
    else:
        s.request.write_bytes(b'{"replaced":true}')
    lists_before = len(s.files.lists)
    result = await confirm(s)
    assert not result["success"]
    assert result["state"] == result["registered"] == "unconfirmed"
    assert result["tool_called"] is True and result["delivery"] == "not_requested"
    assert ref not in repr(result) and "not_registered" not in repr(result)
    assert len(s.files.lists) == lists_before
    status = await s.broker.status(TASK)
    assert status["registered"] == "confirmed" and status["cached"] is True
    assert status["items"][0]["file"]["file_ref"] == ref
    assert len(s.files.lists) > lists_before  # Status reauthorizes against the current real file scope.
    assert len(s.files.uploads) == 1 and not s.files.sends and not list(s.runtime.iterdir())


async def test_previously_sending_plain_manifest_conflicts_with_never_send_request(request_setup):
    from unittest.mock import AsyncMock

    s = request_setup
    s.files.send = AsyncMock(return_value={"status": "sent"})
    plain = await s.broker.import_manifest(s.manifest, send=True)
    assert plain["success"] and plain["delivery"] == "sent"
    result = await confirm(s)
    assert result["state"] == result["error"]["code"] == "conflict"
    assert result["tool_called"] is True
    assert result["registered"] == "unconfirmed" and result["delivery"] == "not_requested"
    assert len(s.files.uploads) == 1
    s.files.send.assert_awaited_once_with(plain["items"][0]["file"]["file_ref"])
    assert not list(s.runtime.iterdir())


async def test_request_then_sending_plain_manifest_conflicts_without_new_side_effects(request_setup):
    s = request_setup
    first = await confirm(s)
    assert first["success"] and first["delivery"] == "not_requested"
    ordinary = await s.broker.import_manifest(s.manifest, send=True)
    assert ordinary["state"] == ordinary["error"]["code"] == "conflict"
    assert len(s.files.uploads) == 1 and not s.files.sends
    repeated = await confirm(s)
    assert repeated["cached"] is True and repeated["registered"] == "confirmed"
    assert repeated["delivery"] == "unconfirmed", "Request wrapper must preserve conservative cached delivery state"
    assert len(s.files.uploads) == 1 and not s.files.sends


async def test_partial_request_import_retains_valid_reference_and_does_not_repeat(request_setup):
    s = request_setup
    missing = {"path": "outbox/missing.bin", "size": 0, "sha256": hashlib.sha256(b"").hexdigest()}
    s.manifest["files"].append(missing)
    s.sha = write_request(s)
    result = await confirm(s)
    assert result["state"] == "done" and result["registered"] == "partial"
    assert result["tool_called"] is True and result["delivery"] == "not_requested"
    assert not result["success"]
    assert [item["status"] for item in result["items"]] == ["imported", "failed"]
    assert result["items"][1]["error"]["code"] == "not_found"
    ref = result["items"][0]["file"]["file_ref"]
    repeated = await confirm(s)
    assert repeated["cached"] is True and repeated["registered"] == "partial"
    assert repeated["items"][0]["file"]["file_ref"] == ref
    assert len(s.files.uploads) == 1 and not s.files.sends and not list(s.runtime.iterdir())


async def test_request_artifact_exceeding_configured_file_size_is_not_uploaded(request_setup):
    s = request_setup
    s.broker = Handoff(s.files, str(s.workspace), s.runtime, s.data, len(s.binary) - 1, 8)
    result = await confirm(s)
    assert result["state"] == "done" and result["registered"] == "not_registered"
    assert result["tool_called"] is True and result["delivery"] == "not_requested"
    assert result["items"][0]["error"]["code"] == "too_large"
    assert s.broker.ledger.is_dir()  # A claimed failed task remains claimed rather than silently retryable.
    assert not s.files.uploads and not s.files.sends and not list(s.runtime.iterdir())


async def test_deeply_nested_request_json_is_rejected_without_ledger_or_upload(request_setup):
    s = request_setup
    payload = b"[" * 16000 + b"0" + b"]" * 16000
    assert len(payload) < MAX_REQUEST_BYTES
    result = await confirm(s, write_request(s, payload))
    assert result["error"]["code"] == "invalid_request"
    assert result["tool_called"] is True and result["registered"] == "unconfirmed"
    assert not s.data.exists() and not s.files.lists and not s.files.uploads and not s.files.sends
    assert not list(s.runtime.iterdir())


@pytest.mark.parametrize("count", [32, 33])
async def test_request_hard_batch_boundary_32_and_33(request_setup, count):
    s = request_setup
    s.broker = Handoff(s.files, str(s.workspace), s.runtime, s.data, 1024, 32)
    empty_sha = hashlib.sha256(b"").hexdigest()
    s.manifest["files"] = []
    for index in range(count):
        relative = f"outbox/empty-{index}.bin"
        (s.workspace / relative).write_bytes(b"")
        s.manifest["files"].append({"path": relative, "size": 0, "sha256": empty_sha})
    result = await confirm(s, write_request(s))
    if count == 32:
        assert result["success"] and result["registered"] == "confirmed"
        assert len(result["items"]) == len(s.files.uploads) == 32
        assert all(item["file"]["size"] == 0 for item in result["items"])
    else:
        assert result["error"]["code"] == "invalid_manifest"
        assert result["state"] == result["registered"] == "unconfirmed"
        assert not s.data.exists() and not s.files.lists and not s.files.uploads
    assert result["tool_called"] is True and result["delivery"] == "not_requested"
    assert not s.files.sends and not list(s.runtime.iterdir())
