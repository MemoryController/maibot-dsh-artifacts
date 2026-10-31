"""Real ledger and filesystem tests with a scope-checking official SDK mock."""

import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import threading
from copy import deepcopy
from pathlib import Path

import pytest

from handoff import Handoff, validate_manifest
from safe_files import SafetyError

TASK = "a" * 32
OTHER_TASK = "b" * 32


class RuntimeMock:
    """Only real active tool scope permits list/upload/send; no session selector."""

    def __init__(self):
        self.scope = "chat-a"
        self.assets = {"chat-a": {}, "chat-b": {}}
        self.uploads = []
        self.sends = []
        self.lists = []
        self.upload_error = None
        self.send_error = None
        self.receipt = "sent"
        self.before_upload = None
        self.before_send = None

    async def list(self, offset=0):
        self.lists.append(offset)
        if self.scope is None:
            raise PermissionError("outside actual tool scope")
        return list(self.assets[self.scope].values())[offset : offset + 50]

    async def upload(self, source, name, mime_type="application/octet-stream"):
        if self.scope is None:
            raise PermissionError("expired scope")
        if self.before_upload:
            self.before_upload()
        data = Path(source).read_bytes()
        self.uploads.append((name, data))
        if self.upload_error:
            raise self.upload_error
        ref = f"{len(self.uploads):032x}"
        metadata = {
            "file_ref": ref,
            "name": name,
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "mime_type": mime_type,
            "state": "ready",
            "url": "secret-signed-url",
            "session_id": self.scope,
        }
        self.assets[self.scope][ref] = metadata
        return dict(metadata)

    async def send(self, file_ref):
        if self.scope is None or file_ref not in self.assets[self.scope]:
            raise PermissionError("not accessible in current scope")
        if self.before_send:
            self.before_send()
        self.sends.append(file_ref)
        if self.send_error:
            raise self.send_error
        return {"status": self.receipt, "message_id": "private-original-message"}


@pytest.fixture
def setup(tmp_path):
    workspace = tmp_path / "shared"
    (workspace / "outbox").mkdir(parents=True)
    runtime, data = tmp_path / "runtime", tmp_path / "data"
    runtime.mkdir(mode=0o700)
    api = RuntimeMock()
    broker = Handoff(api, str(workspace), runtime, data, 1024, 8)
    manifest = {
        "type": "dsh_artifacts",
        "version": 1,
        "task_id": TASK,
        "files": [{"path": "outbox/报告.bin", "size": 8, "sha256": hashlib.sha256(b"\x00\xffbinary").hexdigest()}],
    }
    (workspace / manifest["files"][0]["path"]).write_bytes(b"\x00\xffbinary")
    return broker, api, manifest, workspace, runtime, data


def unconfirmed(result):
    assert result == {
        "success": False,
        "state": "unconfirmed",
        "error": {"code": "confirmation_unavailable"},
        "tool_called": "unconfirmed",
        "registered": "unconfirmed",
        "delivery": "unconfirmed",
    }


def test_validator_returns_independent_copy(setup):
    broker, api, manifest, workspace, runtime, data = setup
    copy = validate_manifest(manifest, 8)
    assert copy == manifest and copy is not manifest
    copy["files"][0]["path"] = "changed"
    assert manifest["files"][0]["path"] == "outbox/报告.bin"


@pytest.mark.parametrize("extra", ["session_id", "chat_id", "target", "send", "token"])
def test_validator_rejects_extra_keys(setup, extra):
    broker, api, manifest, workspace, runtime, data = setup
    with pytest.raises(SafetyError):
        validate_manifest({**manifest, extra: "value"}, 8)


@pytest.mark.parametrize(
    "change",
    [
        {"type": "other"},
        {"version": True},
        {"version": 2},
        {"task_id": "../task"},
        {"files": []},
        {"files": "outbox/file"},
    ],
)
def test_invalid_manifest_header(setup, change):
    broker, api, manifest, workspace, runtime, data = setup
    with pytest.raises(SafetyError):
        validate_manifest({**manifest, **change}, 8)


@pytest.mark.parametrize(
    "change",
    [
        {"path": "../outbox/x"},
        {"path": "/etc/passwd"},
        {"path": "outbox/../x"},
        {"path": "inbox/file"},
        {"path": "outbox/link\\x"},
        {"size": True},
        {"size": -1},
        {"size": "8"},
        {"sha256": "F" * 64},
        {"sha256": "a" * 63},
        {"extra": "x"},
    ],
)
def test_invalid_file_declaration(setup, change):
    broker, api, manifest, workspace, runtime, data = setup
    manifest["files"][0].update(change)
    with pytest.raises(SafetyError):
        validate_manifest(manifest, 8)


def test_duplicate_and_batch_limits(setup):
    broker, api, manifest, workspace, runtime, data = setup
    manifest["files"] *= 2
    with pytest.raises(SafetyError):
        validate_manifest(manifest, 8)
    manifest["files"] = [{**manifest["files"][0], "path": f"outbox/{i}"} for i in range(9)]
    with pytest.raises(SafetyError):
        validate_manifest(manifest, 8)


def test_constructor_has_no_io_and_rejects_overlap(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("unexpected filesystem I/O")

    monkeypatch.setattr(os, "open", forbidden)
    Handoff(RuntimeMock(), "/shared", tmp_path / "runtime", tmp_path / "data", 1024, 8)
    with pytest.raises(SafetyError):
        Handoff(RuntimeMock(), str(tmp_path), tmp_path / "runtime", tmp_path / "data", 1024, 8)
    with pytest.raises(SafetyError):
        Handoff(RuntimeMock(), str(tmp_path / "data" / "shared"), tmp_path / "runtime", tmp_path / "data", 1024, 8)


async def test_binary_import_preserves_name_and_metadata(setup):
    broker, api, manifest, workspace, runtime, data = setup
    result = await broker.import_manifest(manifest)
    assert result["success"] and result["tool_called"] is True
    assert result["registered"] == "confirmed" and result["delivery"] == "not_requested"
    assert result["items"][0]["file"] == {
        "file_ref": "0" * 31 + "1",
        "name": "报告.bin",
        "size": 8,
        "sha256": manifest["files"][0]["sha256"],
        "mime_type": "application/octet-stream",
    }
    assert api.uploads == [("报告.bin", b"\x00\xffbinary")]
    assert not api.sends and not list(runtime.iterdir())
    assert api.lists[0] == 0
    assert "secret-signed-url" not in json.dumps(result)
    assert broker.ledger.stat().st_mode & 0o777 == 0o700
    assert (broker.ledger / "ledger.sqlite3").stat().st_mode & 0o777 == 0o600


async def test_status_when_planner_never_called_does_not_create_ledger(setup):
    broker, api, manifest, workspace, runtime, data = setup
    unconfirmed(await broker.status(TASK))
    assert not data.exists() and not api.uploads and not api.sends
    assert not api.lists


async def test_scope_failure_precedes_claim_and_has_zero_side_effects(setup):
    broker, api, manifest, workspace, runtime, data = setup
    api.scope = None
    unconfirmed(await broker.import_manifest(manifest))
    assert not data.exists() and not api.uploads and not api.sends


async def test_cache_uses_current_host_metadata_not_original_receipts_or_paths(setup):
    broker, api, manifest, workspace, runtime, data = setup
    first = await broker.import_manifest(manifest, send=True)
    ref = first["items"][0]["file"]["file_ref"]
    (workspace / manifest["files"][0]["path"]).unlink()
    api.assets[api.scope][ref]["name"] = "current-name.bin"
    cached = await broker.import_manifest(manifest, send=True)
    assert cached["cached"] is True
    assert cached["success"] is False
    assert cached["items"][0]["file"]["name"] == "current-name.bin"
    assert cached["delivery"] == "unconfirmed"
    assert "path" not in cached["items"][0] and "send" not in cached["items"][0]
    assert "session_id" not in json.dumps(cached) and "private-original-message" not in json.dumps(cached)
    assert len(api.uploads) == len(api.sends) == 1


async def test_cross_chat_cache_and_status_do_not_disclose_any_record_details(setup):
    broker, api, manifest, workspace, runtime, data = setup
    await broker.import_manifest(manifest)
    api.scope = "chat-b"
    unconfirmed(await broker.status(TASK))
    unconfirmed(await broker.import_manifest(manifest))
    changed = deepcopy(manifest)
    changed["files"][0]["sha256"] = "f" * 64
    unconfirmed(await broker.import_manifest(changed))
    assert len(api.uploads) == 1 and not api.sends


async def test_legal_host_sharing_authorizes_current_metadata_without_original_details(setup):
    broker, api, manifest, workspace, runtime, data = setup
    first = await broker.import_manifest(manifest, send=True)
    ref = first["items"][0]["file"]["file_ref"]
    api.assets["chat-b"][ref] = {**api.assets["chat-a"][ref], "name": "shared-current.bin", "session_id": "chat-b"}
    api.scope = "chat-b"
    status = await broker.status(TASK)
    assert status["cached"] and status["items"][0]["file"]["name"] == "shared-current.bin"
    assert status["delivery"] == "unconfirmed" and "send" not in status["items"][0]
    assert "path" not in status["items"][0]
    assert len(api.uploads) == len(api.sends) == 1


@pytest.mark.parametrize("change", ["send", "sha256", "workspace", "limit"])
async def test_authorized_repeat_with_changed_parameters_conflicts(setup, change, tmp_path):
    broker, api, manifest, workspace, runtime, data = setup
    await broker.import_manifest(manifest)
    send = False
    if change == "send":
        send = True
    elif change == "sha256":
        manifest["files"][0]["sha256"] = "f" * 64
    else:
        broker = Handoff(
            api,
            str(workspace) if change == "limit" else str(tmp_path / "other-shared"),
            runtime,
            data,
            1 if change == "limit" else 1024,
            8,
        )
    result = await broker.import_manifest(manifest, send)
    assert result["error"]["code"] == "conflict"
    assert len(api.uploads) == 1 and not api.sends


@pytest.mark.parametrize("state", ["deleted", "failed", "remote", "uploading", "downloading"])
async def test_deleted_or_failed_current_ref_does_not_authorize_cached_details(setup, state):
    broker, api, manifest, workspace, runtime, data = setup
    first = await broker.import_manifest(manifest)
    ref = first["items"][0]["file"]["file_ref"]
    api.assets[api.scope][ref]["state"] = state
    unconfirmed(await broker.status(TASK))
    assert len(api.uploads) == 1


async def test_partial_batch_then_cached_failure_never_discloses_its_path_or_hash(setup):
    broker, api, manifest, workspace, runtime, data = setup
    manifest["files"].append({"path": "outbox/secret-missing", "size": 10, "sha256": "f" * 64})
    result = await broker.import_manifest(manifest)
    assert not result["success"] and result["registered"] == "partial"
    assert result["items"][1]["error"]["code"] == "not_found"
    cached = await broker.import_manifest(manifest)
    assert cached["items"][1] == {"status": "failed", "error": {"code": "not_found"}}
    assert "secret-missing" not in json.dumps(cached) and "f" * 64 not in json.dumps(cached)
    assert len(api.uploads) == 1


@pytest.mark.parametrize("failure", ["missing", "size", "hash", "oversize", "permission", "symlink"])
async def test_snapshot_failures_prevent_upload_and_cannot_be_automatically_retried(setup, failure, monkeypatch):
    broker, api, manifest, workspace, runtime, data = setup
    path = workspace / manifest["files"][0]["path"]
    if failure == "missing":
        path.unlink()
    elif failure == "size":
        manifest["files"][0]["size"] += 1
    elif failure == "hash":
        manifest["files"][0]["sha256"] = "f" * 64
    elif failure == "oversize":
        manifest["files"][0]["size"] = 1025
    elif failure == "symlink":
        path.unlink()
        path.symlink_to("/etc/passwd")
    else:

        def denied(*args):
            raise PermissionError("do not disclose this internal path")

        monkeypatch.setattr(broker, "_snapshot", denied)
    result = await broker.import_manifest(manifest)
    expected = {
        "missing": "not_found",
        "size": "size_mismatch",
        "hash": "hash_mismatch",
        "oversize": "too_large",
        "permission": "permission_denied",
        "symlink": "operation_failed",
    }[failure]
    assert result["items"][0]["error"]["code"] == expected
    assert result["items"][0]["status"] == "failed"
    assert result["registered"] == "not_registered"
    assert not api.uploads and not api.sends and not list(runtime.iterdir())
    unconfirmed(await broker.import_manifest(manifest))
    unconfirmed(await broker.status(TASK))


async def test_upload_exception_is_unknown_without_sensitive_text_or_automatic_retry(setup):
    broker, api, manifest, workspace, runtime, data = setup
    api.upload_error = RuntimeError("secret-signed-url")
    result = await broker.import_manifest(manifest)
    assert result["items"][0]["status"] == "unknown"
    assert "secret-signed-url" not in json.dumps(result)
    unconfirmed(await broker.import_manifest(manifest))
    unconfirmed(await broker.status(TASK))
    assert len(api.uploads) == 1


@pytest.mark.parametrize("receipt", ["unknown", "invalid", "exception"])
async def test_uncertain_send_is_never_automatically_repeated(setup, receipt):
    broker, api, manifest, workspace, runtime, data = setup
    if receipt == "exception":
        api.send_error = RuntimeError("secret transport details")
    else:
        api.receipt = receipt
    result = await broker.import_manifest(manifest, send=True)
    assert not result["success"] and result["delivery"] == "unknown"
    assert result["items"][0]["send"] == {"status": "unknown", "retry_safe": False}
    await broker.import_manifest(manifest, send=True)
    await broker.status(TASK)
    assert len(api.uploads) == len(api.sends) == 1


async def test_writeahead_committed_before_each_side_effect(setup):
    broker, api, manifest, workspace, runtime, data = setup

    def stored():
        with sqlite3.connect(broker.ledger / "ledger.sqlite3") as db:
            (value,) = db.execute("SELECT result FROM tasks WHERE task_id=?", (TASK,)).fetchone()
        return json.loads(value)["items"][0]

    def before_upload():
        assert stored()["operation"] == "upload" and stored()["status"] == "unknown"

    def before_send():
        assert stored()["file"]["file_ref"] == "0" * 31 + "1"
        assert stored()["send"]["status"] == "unknown"

    api.before_upload, api.before_send = before_upload, before_send
    assert (await broker.import_manifest(manifest, send=True))["success"]


@pytest.mark.parametrize("phase", ["upload", "send"])
async def test_cancellation_and_restart_does_not_resume_actions(setup, phase):
    broker, api, manifest, workspace, runtime, data = setup
    if phase == "upload":
        api.upload_error = asyncio.CancelledError()
    else:
        api.send_error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await broker.import_manifest(manifest, send=True)
    counts = len(api.uploads), len(api.sends)
    restarted = Handoff(api, str(workspace), runtime, data, 1024, 8)
    result = await restarted.import_manifest(manifest, send=True)
    if phase == "upload":
        unconfirmed(result)
    else:
        assert result["cached"] and result["state"] == "unknown"
    assert (len(api.uploads), len(api.sends)) == counts
    assert not list(runtime.iterdir())


async def test_concurrent_instances_claim_task_only_once(setup):
    broker, api, manifest, workspace, runtime, data = setup
    other = Handoff(api, str(workspace), runtime, data, 1024, 8)
    results = await asyncio.gather(broker.import_manifest(manifest), other.import_manifest(manifest))
    assert any(result.get("success") for result in results)
    assert len(api.uploads) == 1


async def test_status_paginates_and_requires_every_persisted_reference(setup):
    broker, api, manifest, workspace, runtime, data = setup
    manifest["files"].append({**manifest["files"][0], "path": "outbox/second"})
    (workspace / "outbox/second").write_bytes(b"\x00\xffbinary")
    first = await broker.import_manifest(manifest)
    refs = [item["file"]["file_ref"] for item in first["items"]]
    rows = [{"file_ref": f"{n + 100:032x}", "state": "ready"} for n in range(50)]
    rows.extend(api.assets[api.scope].values())

    async def paginated(offset=0):
        api.lists.append(offset)
        return rows[offset : offset + 50]

    api.list = paginated
    api.lists.clear()
    assert (await broker.status(TASK))["cached"]
    assert api.lists == [0, 50]
    rows.pop()
    unconfirmed(await broker.status(TASK))
    assert len(refs) == len(api.uploads) == 2


async def test_pagination_budget_and_list_failure_are_unconfirmed(setup):
    broker, api, manifest, workspace, runtime, data = setup
    await broker.import_manifest(manifest)
    calls = []

    async def endless(offset=0):
        calls.append(offset)
        return [{"file_ref": "f" * 32, "state": "ready"}] * 50

    api.list = endless
    unconfirmed(await broker.status(TASK))
    assert len(calls) == 500

    async def failed(offset=0):
        raise RuntimeError("secret list failure")

    api.list = failed
    unconfirmed(await broker.status(TASK))


@pytest.mark.parametrize("component", ["ledger.sqlite3", "ledger.lock"])
async def test_missing_ledger_component_is_fail_closed(setup, component):
    broker, api, manifest, workspace, runtime, data = setup
    await broker.import_manifest(manifest)
    (broker.ledger / component).unlink()
    unconfirmed(await broker.import_manifest(manifest))
    unconfirmed(await broker.status(TASK))
    assert len(api.uploads) == 1


async def test_io_cancellation_waits_for_worker_before_cleaning_private_stage(setup, monkeypatch):
    broker, api, manifest, workspace, runtime, data = setup
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = broker._snapshot

    def slow(item, stage):
        started.set()
        assert release.wait(5)
        result = original(item, stage)
        finished.set()
        return result

    monkeypatch.setattr(broker, "_snapshot", slow)
    pending = asyncio.create_task(broker.import_manifest(manifest))
    for _ in range(100):
        if started.is_set():
            break
        await asyncio.sleep(0.001)
    assert started.is_set()
    pending.cancel()
    await asyncio.sleep(0.005)
    pending.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert finished.is_set() and not api.uploads and not list(runtime.iterdir())
    unconfirmed(await broker.import_manifest(manifest))


async def test_replay_and_status_are_strictly_read_only(setup, monkeypatch):
    broker, api, manifest, workspace, runtime, data = setup
    await broker.import_manifest(manifest)
    before = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in broker.ledger.iterdir()}

    def forbidden(*args, **kwargs):
        raise AssertionError("replay attempted a write/claim")

    monkeypatch.setattr(broker, "_claim", forbidden)
    monkeypatch.setattr(broker, "_save", forbidden)
    assert (await broker.import_manifest(manifest))["cached"]
    assert (await broker.status(TASK))["cached"]
    after = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in broker.ledger.iterdir()}
    assert before == after and len(api.uploads) == 1


async def test_unknown_item_is_sanitized_when_another_item_authorizes_cache(setup):
    broker, api, manifest, workspace, runtime, data = setup
    manifest["files"].append({**manifest["files"][0], "path": "outbox/private-unknown"})
    (workspace / "outbox/private-unknown").write_bytes(b"\x00\xffbinary")
    original_upload = api.upload

    async def partially_failed(source, name, mime_type="application/octet-stream"):
        if name == "private-unknown":
            raise RuntimeError("secret failure details")
        return await original_upload(source, name, mime_type=mime_type)

    api.upload = partially_failed
    first = await broker.import_manifest(manifest)
    assert first["items"][1]["status"] == "unknown"
    cached = await broker.status(TASK)
    assert cached["items"][1] == {"status": "unknown", "error": {"code": "operation_failed"}}
    assert "private-unknown" not in json.dumps(cached)
    assert len(api.uploads) == 1


async def test_finished_failure_is_not_retried_after_file_is_fixed(setup):
    broker, api, manifest, workspace, runtime, data = setup
    path = workspace / manifest["files"][0]["path"]
    path.unlink()
    first = await broker.import_manifest(manifest)
    assert first["registered"] == "not_registered"
    path.write_bytes(b"\x00\xffbinary")
    unconfirmed(await broker.import_manifest(manifest))
    assert not api.uploads
    manifest["task_id"] = OTHER_TASK  # A new explicit task is separate work, never automatic recovery.
    assert (await broker.import_manifest(manifest))["success"]
    assert len(api.uploads) == 1


async def test_actual_oversize_is_rejected_even_if_manifest_lies(setup):
    broker, api, manifest, workspace, runtime, data = setup
    (workspace / manifest["files"][0]["path"]).write_bytes(b"x" * 1025)
    first = await broker.import_manifest(manifest)
    assert first["items"][0]["error"]["code"] == "too_large"
    assert not api.uploads and not list(runtime.iterdir())


async def test_status_unknown_task_and_invalid_task_do_not_claim_success_or_mutate(setup):
    broker, api, manifest, workspace, runtime, data = setup
    await broker.import_manifest(manifest)
    before = (broker.ledger / "ledger.sqlite3").read_bytes()
    unconfirmed(await broker.status(OTHER_TASK))
    unconfirmed(await broker.status("../invalid"))
    assert before == (broker.ledger / "ledger.sqlite3").read_bytes()
    assert len(api.uploads) == 1


@pytest.mark.parametrize(
    "failure", ["truncated", "marker", "database_symlink", "lock_symlink", "directory_permissions"]
)
async def test_corrupt_or_unsafe_ledger_is_never_recreated(setup, failure, tmp_path):
    broker, api, manifest, workspace, runtime, data = setup
    await broker.import_manifest(manifest)
    if failure == "truncated":
        (broker.ledger / "ledger.sqlite3").write_bytes(b"")
    elif failure == "marker":
        (broker.ledger / "ledger.lock").write_bytes(b"damaged")
    elif failure == "directory_permissions":
        broker.ledger.chmod(0o755)
    else:
        component = "ledger.sqlite3" if failure == "database_symlink" else "ledger.lock"
        original = broker.ledger / component
        outside = tmp_path / "preserved-outside"
        original.rename(outside)
        original.symlink_to(outside)
    unconfirmed(await broker.status(TASK))
    unconfirmed(await broker.import_manifest(manifest))
    assert len(api.uploads) == 1


async def test_changed_batch_limit_conflicts_with_an_existing_authorized_manifest(setup):
    broker, api, manifest, workspace, runtime, data = setup
    manifest["files"].append({**manifest["files"][0], "path": "outbox/second"})
    (workspace / "outbox/second").write_bytes(b"\x00\xffbinary")
    await broker.import_manifest(manifest)
    changed = Handoff(api, str(workspace), runtime, data, 1024, 1)
    assert (await changed.import_manifest(manifest))["error"]["code"] == "conflict"
    assert len(api.uploads) == 2


async def test_bad_scope_result_does_not_claim_task(setup):
    broker, api, manifest, workspace, runtime, data = setup

    async def bad_list(offset=0):
        return {"unexpected": "not an official list result"}

    api.list = bad_list
    unconfirmed(await broker.import_manifest(manifest))
    assert not data.exists() and not api.uploads


@pytest.mark.parametrize("send", ["true", 1, None])
async def test_send_requires_explicit_boolean_and_never_comes_from_manifest(setup, send):
    broker, api, manifest, workspace, runtime, data = setup
    assert (await broker.import_manifest(manifest, send))["error"]["code"] == "invalid_manifest"
    assert not data.exists() and not api.uploads and not api.lists


@pytest.mark.parametrize("removed", ["subdirectory", "both_components", "sentinel"])
async def test_independent_sentinel_prevents_accidental_history_reinitialization(setup, removed):
    broker, api, manifest, workspace, runtime, data = setup
    await broker.import_manifest(manifest)
    sentinel = data / ".handoff-initialized"
    assert sentinel.stat().st_mode & 0o777 == 0o600
    if removed == "subdirectory":
        shutil.rmtree(broker.ledger)
    elif removed == "both_components":
        for path in broker.ledger.iterdir():
            path.unlink()
    else:
        sentinel.unlink()
    unconfirmed(await broker.status(TASK))
    unconfirmed(await broker.import_manifest(manifest))
    manifest["task_id"] = OTHER_TASK
    unconfirmed(await broker.import_manifest(manifest))
    assert len(api.uploads) == 1
    if removed == "subdirectory":
        assert not broker.ledger.exists()
    elif removed == "both_components":
        assert not list(broker.ledger.iterdir())
    else:
        assert not sentinel.exists()


@pytest.mark.parametrize("bad", ["mode", "symlink", "corrupt"])
async def test_unsafe_initialization_sentinel_is_rejected(setup, bad, tmp_path):
    broker, api, manifest, workspace, runtime, data = setup
    await broker.import_manifest(manifest)
    sentinel = data / ".handoff-initialized"
    if bad == "mode":
        sentinel.chmod(0o644)
    elif bad == "symlink":
        original = tmp_path / "original-sentinel"
        sentinel.rename(original)
        sentinel.symlink_to(original)
    else:
        sentinel.write_bytes(b"corrupt")
    unconfirmed(await broker.status(TASK))
    unconfirmed(await broker.import_manifest(manifest))
    assert len(api.uploads) == 1


async def test_failure_to_commit_writeahead_prevents_upload(setup, monkeypatch):
    broker, api, manifest, workspace, runtime, data = setup

    async def cannot_commit(*args):
        raise OSError("storage unavailable")

    monkeypatch.setattr(broker, "_save", cannot_commit)
    unconfirmed(await broker.import_manifest(manifest, send=True))
    assert not api.uploads and not api.sends
    unconfirmed(await broker.status(TASK))


async def test_failure_to_commit_known_ref_does_not_continue_to_send(setup, monkeypatch):
    broker, api, manifest, workspace, runtime, data = setup
    save = broker._save
    count = 0

    async def fail_after_upload(result):
        nonlocal count
        count += 1
        if count >= 2:
            raise OSError("storage unavailable after upload")
        await save(result)

    monkeypatch.setattr(broker, "_save", fail_after_upload)
    unconfirmed(await broker.import_manifest(manifest, send=True))
    assert len(api.uploads) == 1 and not api.sends
    unconfirmed(await broker.import_manifest(manifest, send=True))
    assert len(api.uploads) == 1 and not api.sends
