"""The Host broker is tested without depending on MaiBot imports or credentials."""

import asyncio
import json
import os
import sqlite3
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from integrations.maibot_1_3_18.artifact_handoff import (
    HandoffBroker,
    HandoffConfig,
    HandoffError,
    StreamRedactor,
    redact_tokens,
)

TASK = "1" * 32
OTHER_TASK = "2" * 32
REF = "a" * 32


class Backend:
    def __init__(self):
        self.chats = {"real-chat", "other-chat"}
        self.created = []
        self.sent = []
        self.send_status = "sent"
        self.create_error = None
        self.send_error = None

    async def require_chat(self, chat):
        if chat not in self.chats:
            raise PermissionError("sensitive backend details")

    async def create(self, chat, name, source):
        self.created.append((chat, name, source.read_bytes()))
        if self.create_error:
            raise self.create_error
        return {"file_ref": REF, "url": "secret-signed-url"}

    async def send(self, file_ref, chat):
        self.sent.append((file_ref, chat))
        if self.send_error:
            raise self.send_error
        return {"status": self.send_status, "message_id": "message"}


@pytest.fixture
async def setup(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / "outbox" / TASK).mkdir(parents=True)
    backend = Backend()
    now = [1000.0]
    config = HandoffConfig(str(workspace), allow_send=True)
    state = tmp_path / "state"
    broker = HandoffBroker(state, "dsh", config, backend, clock=lambda: now[0])
    await broker.issue(TASK, "real-chat", send_allowed=True)
    grant = await broker.grant(TASK)
    args = {
        "task_id": TASK,
        "token": grant["token"],
        "request_id": "req-1",
        "paths": [f"outbox/{TASK}/报告.bin"],
        "send": False,
    }
    (workspace / args["paths"][0]).write_bytes(b"\x00\xffbinary")
    return broker, backend, args, workspace, state, now


def test_constructor_has_no_io(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("constructor attempted I/O")

    monkeypatch.setattr(os, "open", forbidden)
    HandoffBroker(tmp_path / "state", "dsh", HandoffConfig("/workspace"), Backend())


async def test_issue_grant_and_normal_binary_import(setup):
    broker, backend, args, workspace, state, now = setup
    result = await broker.handoff(args, "dsh")
    assert result["success"] is True
    assert result["state"] == "done"
    assert result["items"][0]["file"]["file_ref"] == REF
    assert backend.created == [("real-chat", "报告.bin", b"\x00\xffbinary")]
    assert not backend.sent
    assert args["token"] not in json.dumps(result)
    assert "secret-signed-url" not in json.dumps(result)
    assert state.stat().st_mode & 0o777 == 0o700
    for name in ("handoff.key", "handoff.sqlite3", "handoff.lock"):
        assert (state / name).stat().st_mode & 0o777 == 0o600
        assert args["token"].encode() not in (state / name).read_bytes()
    assert not list(state.glob(".stage-*"))
    grant = await broker.grant(TASK)
    assert grant["outbox_prefix"] == f"outbox/{TASK}/"
    assert grant["send_allowed"] is True
    assert grant["expires_at"] == 4600
    assert grant["token"].startswith("dshh_")


async def test_cache_survives_restart_and_does_not_read_sources(setup):
    broker, backend, args, workspace, state, now = setup
    args["send"] = True
    first = await broker.handoff(args, "dsh")
    (workspace / args["paths"][0]).unlink()
    restarted = HandoffBroker(state, "dsh", broker.config, backend, clock=lambda: now[0])
    assert await restarted.handoff(args, "dsh") == first
    assert len(backend.created) == len(backend.sent) == 1


@pytest.mark.parametrize("change", ["request_id", "paths", "send"])
async def test_task_only_accepts_one_payload_and_request(setup, change):
    broker, backend, args, workspace, state, now = setup
    await broker.handoff(args, "dsh")
    new = dict(args)
    new[change] = {"request_id": "req-2", "paths": [f"outbox/{TASK}/other"], "send": True}[change]
    result = await broker.handoff(new, "dsh")
    assert result["error"]["code"] == "conflict"
    assert len(backend.created) == 1


@pytest.mark.parametrize("key", ["chat_id", "session_id", "target", "filename", "url"])
async def test_cannot_select_chat_or_inject_unknown_parameters(setup, key):
    broker, backend, args, workspace, state, now = setup
    assert not (await broker.handoff({**args, key: "other-chat"}, "dsh"))["success"]
    assert not backend.created
    assert (await broker.handoff(args, "dsh"))["success"]


async def test_wrong_agent_or_forged_token_has_no_effect(setup):
    broker, backend, args, workspace, state, now = setup
    assert (await broker.handoff(args, "other-agent"))["error"]["code"] == "unauthorized"
    forged = {**args, "token": "dshh_" + "0" * 64}
    assert (await broker.handoff(forged, "dsh"))["error"]["code"] == "unauthorized"
    assert not backend.created


async def test_task_token_cannot_be_reused_for_other_real_chat(setup):
    broker, backend, args, workspace, state, now = setup
    await broker.issue(OTHER_TASK, "other-chat", send_allowed=True)
    result = await broker.handoff({**args, "task_id": OTHER_TASK, "paths": [f"outbox/{OTHER_TASK}/x"]}, "dsh")
    assert result["error"]["code"] == "unauthorized"
    assert not backend.created


@pytest.mark.parametrize("cached", [False, True])
async def test_expiration_rejects_even_cached_result(setup, cached):
    broker, backend, args, workspace, state, now = setup
    if cached:
        await broker.handoff(args, "dsh")
    now[0] = 4600
    result = await broker.handoff(args, "dsh")
    assert result["error"]["code"] == "expired"
    with pytest.raises(HandoffError, match="expired"):
        await broker.grant(TASK)
    assert len(backend.created) == int(cached)


@pytest.mark.parametrize(
    "path",
    ["/etc/passwd", f"outbox/{TASK}/../secret", f"outbox/{OTHER_TASK}/x", f"outbox/{TASK}/x\\y"],
)
async def test_task_directory_scope(setup, path):
    broker, backend, args, workspace, state, now = setup
    assert (await broker.handoff({**args, "paths": [path]}, "dsh"))["error"]["code"] == "invalid_path"
    assert not backend.created


@pytest.mark.parametrize("send_status", ["sent", "unknown", "unexpected"])
async def test_send_receipt_no_retries(setup, send_status):
    broker, backend, args, workspace, state, now = setup
    args["send"] = True
    backend.send_status = send_status
    first = await broker.handoff(args, "dsh")
    assert first["items"][0]["send"] == {
        "status": "sent" if send_status == "sent" else "unknown",
        "retry_safe": False,
    }
    assert first["success"] == (send_status == "sent")
    assert await broker.handoff(args, "dsh") == first
    assert len(backend.sent) == 1


@pytest.mark.parametrize("allow_config,allow_task", [(False, True), (True, False), (False, False)])
async def test_send_requires_both_host_and_task_authorization(tmp_path, allow_config, allow_task):
    backend = Backend()
    broker = HandoffBroker(
        tmp_path / "state",
        "dsh",
        HandoffConfig(str(tmp_path / "workspace"), allow_send=allow_config),
        backend,
    )
    if allow_task and not allow_config:
        with pytest.raises(HandoffError, match="send_forbidden"):
            await broker.issue(TASK, "real-chat", send_allowed=allow_task)
        return
    await broker.issue(TASK, "real-chat", send_allowed=allow_task)
    grant = await broker.grant(TASK)
    result = await broker.handoff(
        {"task_id": TASK, "token": grant["token"], "request_id": "r", "paths": [f"outbox/{TASK}/x"], "send": True},
        "dsh",
    )
    assert result["error"]["code"] == "send_forbidden"
    assert not backend.created and not backend.sent


async def test_partial_batch_failure_continues_and_is_cached(setup):
    broker, backend, args, workspace, state, now = setup
    args["paths"].insert(0, f"outbox/{TASK}/missing")
    result = await broker.handoff(args, "dsh")
    assert result["items"][0]["error"]["code"] == "not_found"
    assert result["items"][0]["status"] == "failed"
    assert result["items"][1]["status"] == "imported"
    assert not result["success"]
    assert await broker.handoff(args, "dsh") == result
    assert len(backend.created) == 1


async def test_create_failure_is_unknown_and_never_retried(setup):
    broker, backend, args, workspace, state, now = setup
    backend.create_error = RuntimeError("secret URL token=do-not-leak")
    result = await broker.handoff(args, "dsh")
    assert result["items"][0]["status"] == "unknown"
    assert result["items"][0]["error"]["code"] == "operation_failed"
    assert "do-not-leak" not in json.dumps(result)
    assert await broker.handoff(args, "dsh") == result
    assert len(backend.created) == 1


async def test_send_failure_retains_ref_and_is_unknown(setup):
    broker, backend, args, workspace, state, now = setup
    backend.send_error = RuntimeError("secret")
    args["send"] = True
    result = await broker.handoff(args, "dsh")
    assert result["items"][0]["file"]["file_ref"] == REF
    assert result["items"][0]["send"]["status"] == "unknown"
    assert not result["success"]
    assert await broker.handoff(args, "dsh") == result
    assert len(backend.created) == len(backend.sent) == 1


async def test_real_chat_rechecked_before_create(setup):
    broker, backend, args, workspace, state, now = setup
    backend.chats.remove("real-chat")
    result = await broker.handoff(args, "dsh")
    assert result["items"][0]["error"]["code"] == "permission_denied"
    assert not backend.created


async def test_writeahead_visible_before_create_and_send(setup):
    broker, backend, args, workspace, state, now = setup
    args["send"] = True
    original_create, original_send = backend.create, backend.send

    def read_progress():
        with sqlite3.connect(state / "handoff.sqlite3") as db:
            (response,) = db.execute("SELECT response FROM tasks WHERE task_id=?", (TASK,)).fetchone()
        return json.loads(response)["items"][0]

    async def create(chat, name, source):
        progress = read_progress()
        assert progress["status"] == "unknown" and progress["operation"] == "create"
        return await original_create(chat, name, source)

    async def send(file_ref, chat):
        progress = read_progress()
        assert progress["file"]["file_ref"] == REF and progress["send"]["status"] == "unknown"
        return await original_send(file_ref, chat)

    backend.create, backend.send = create, send
    assert (await broker.handoff(args, "dsh"))["success"]


@pytest.mark.parametrize("phase", ["create", "send"])
async def test_interrupted_side_effect_never_replayed_after_restart(setup, phase):
    broker, backend, args, workspace, state, now = setup
    args["send"] = True
    if phase == "create":
        backend.create_error = asyncio.CancelledError()
    else:
        backend.send_error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await broker.handoff(args, "dsh")
    before = len(backend.created), len(backend.sent)
    restarted = HandoffBroker(state, "dsh", broker.config, backend, clock=lambda: now[0])
    result = await restarted.handoff(args, "dsh")
    assert result["state"] == "unknown" and not result["success"]
    if phase == "send":
        assert result["items"][0]["file"]["file_ref"] == REF
    else:
        assert result["items"][0]["status"] == "unknown"
    assert (len(backend.created), len(backend.sent)) == before
    assert not list(state.glob(".stage-*"))


async def test_concurrent_same_request_creates_at_most_once(setup):
    broker, backend, args, workspace, state, now = setup
    restarted = HandoffBroker(state, "dsh", broker.config, backend, clock=lambda: now[0])
    results = await asyncio.gather(broker.handoff(args, "dsh"), restarted.handoff(args, "dsh"))
    assert any(result["success"] for result in results)
    assert len(backend.created) == 1


async def test_copy_worker_does_not_block_loop_and_finishes_before_cancel_cleanup(setup, monkeypatch):
    broker, backend, args, workspace, state, now = setup
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    original_snapshot = broker._snapshot

    def delayed(path, stage):
        started.set()
        assert release.wait(5)
        result = original_snapshot(path, stage)
        finished.set()
        return result

    monkeypatch.setattr(broker, "_snapshot", delayed)
    pending = asyncio.create_task(broker.handoff(args, "dsh"))
    for _ in range(100):
        if started.is_set():
            break
        await asyncio.sleep(0.001)
    assert started.is_set()
    pending.cancel()
    await asyncio.sleep(0.01)
    pending.cancel()  # Even repeated cancellation cannot abandon a running I/O worker.
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert finished.is_set() and not list(state.glob(".stage-*"))
    assert not backend.created
    assert not (await broker.handoff(args, "dsh"))["success"]
    assert not backend.created


@pytest.mark.parametrize("unsafe", ["directory_mode", "key_mode", "key_symlink", "db_symlink"])
async def test_unsafe_state_rejected(tmp_path, unsafe):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    if unsafe == "directory_mode":
        state.chmod(0o755)
    elif unsafe == "key_mode":
        (state / "handoff.key").write_bytes(b"x" * 32)
        (state / "handoff.key").chmod(0o644)
    else:
        target = tmp_path / "outside"
        target.write_bytes(b"x" * 32)
        (state / ("handoff.key" if unsafe == "key_symlink" else "handoff.sqlite3")).symlink_to(target)
    broker = HandoffBroker(state, "dsh", HandoffConfig(str(tmp_path / "workspace")), Backend())
    with pytest.raises((HandoffError, OSError)):
        await broker.issue(TASK, "real-chat")


def test_redaction_is_recursive_and_does_not_mutate():
    token = "dshh_" + "a" * 64
    value = {"x": [f"before {token} after", {"token": token}, 4, None]}
    redacted = redact_tokens(value)
    assert token not in json.dumps(redacted)
    assert token in json.dumps(value)
    assert redacted["x"][2:] == [4, None]


@pytest.mark.parametrize(
    "change",
    [
        {"workspace": "/a-different-workspace"},
        {"allow_send": False},
        {"max_file_bytes": 10},
        {"max_batch_files": 3},
        {"ttl_seconds": 60},
    ],
)
async def test_configuration_changes_revoke_existing_grants(setup, change):
    broker, backend, args, workspace, state, now = setup
    changed = HandoffBroker(state, "dsh", replace(broker.config, **change), backend, clock=lambda: now[0])
    result = await changed.handoff(args, "dsh")
    assert result["error"]["code"] == "config_changed"
    with pytest.raises(HandoffError, match="config_changed"):
        await changed.grant(TASK)
    assert not backend.created


async def test_issue_does_not_renew_expiration_or_change_chat_binding(setup):
    broker, backend, args, workspace, state, now = setup
    grant = await broker.grant(TASK)
    now[0] += 50
    await broker.issue(TASK, "real-chat", send_allowed=True)
    assert (await broker.grant(TASK))["expires_at"] == grant["expires_at"]
    with pytest.raises(HandoffError, match="conflict"):
        await broker.issue(TASK, "other-chat", send_allowed=True)
    with pytest.raises(HandoffError, match="chat_unavailable"):
        await broker.issue(OTHER_TASK, "not-a-real-chat")


async def test_permission_error_during_copy_is_sanitized(setup, monkeypatch):
    broker, backend, args, workspace, state, now = setup

    def denied(*args):
        raise PermissionError("secret host filename and credentials")

    monkeypatch.setattr(broker, "_snapshot", denied)
    result = await broker.handoff(args, "dsh")
    assert result["items"][0]["error"]["code"] == "permission_denied"
    assert "secret" not in json.dumps(result)
    assert not backend.created


async def test_source_size_limit_prevents_creation(setup):
    broker, backend, args, workspace, state, now = setup
    # A fresh task binds the smaller limit; old grants are invalidated by changes.
    smaller = HandoffBroker(state, "dsh", replace(broker.config, max_file_bytes=1), backend, clock=lambda: now[0])
    await smaller.issue(OTHER_TASK, "real-chat")
    grant = await smaller.grant(OTHER_TASK)
    relative = f"outbox/{OTHER_TASK}/big"
    (workspace / relative).parent.mkdir()
    (workspace / relative).write_bytes(b"too large")
    result = await smaller.handoff({**args, "task_id": OTHER_TASK, "token": grant["token"], "paths": [relative]}, "dsh")
    assert result["items"][0]["error"]["code"] == "too_large"
    assert not backend.created


async def test_symlink_source_never_read(setup, tmp_path):
    broker, backend, args, workspace, state, now = setup
    (workspace / args["paths"][0]).unlink()
    secret = tmp_path / "outside"
    secret.write_bytes(b"secret")
    (workspace / args["paths"][0]).symlink_to(secret)
    result = await broker.handoff(args, "dsh")
    assert not result["success"] and not backend.created
    assert not list(state.glob(".stage-*"))


@pytest.mark.parametrize("invalid", ["request_id", "duplicate", "batch", "send", "token"])
async def test_invalid_arguments_rejected_without_claim(setup, invalid):
    broker, backend, args, workspace, state, now = setup
    bad = dict(args)
    if invalid == "request_id":
        bad["request_id"] = "x/../bad"
    elif invalid == "duplicate":
        bad["paths"] = args["paths"] * 2
    elif invalid == "batch":
        bad["paths"] = [f"outbox/{TASK}/{n}" for n in range(9)]
    elif invalid == "send":
        bad["send"] = "true"
    else:
        bad["token"] = "not-a-token"
    assert (await broker.handoff(bad, "dsh"))["error"]["code"] == "invalid_arguments"
    assert not backend.created
    assert (await broker.handoff(args, "dsh"))["success"]


async def test_expiry_during_copy_prevents_side_effect(setup, monkeypatch):
    broker, backend, args, workspace, state, now = setup
    original = broker._snapshot

    def expire(path, stage):
        result = original(path, stage)
        now[0] = 4600
        return result

    monkeypatch.setattr(broker, "_snapshot", expire)
    result = await broker.handoff(args, "dsh")
    assert result["items"][0]["error"]["code"] == "expired"
    assert not backend.created


@pytest.mark.parametrize("component", ["handoff.key", "handoff.sqlite3", "handoff.lock"])
async def test_missing_persistent_component_fails_closed_and_never_replays(setup, component):
    broker, backend, args, workspace, state, now = setup
    assert (await broker.handoff(args, "dsh"))["success"]
    (state / component).unlink()
    with pytest.raises(HandoffError, match="state_incomplete"):
        await broker.issue(OTHER_TASK, "real-chat")
    with pytest.raises(HandoffError, match="state_incomplete"):
        await broker.grant(TASK)
    assert (await broker.handoff(args, "dsh"))["error"]["code"] == "state_incomplete"
    assert len(backend.created) == 1
    if component != "handoff.lock":
        assert not (state / component).exists()


async def test_truncated_ledger_is_not_silently_reinitialized(setup):
    broker, backend, args, workspace, state, now = setup
    await broker.handoff(args, "dsh")
    (state / "handoff.sqlite3").write_bytes(b"")
    result = await broker.handoff(args, "dsh")
    assert result["error"]["code"] == "state_incomplete"
    assert len(backend.created) == 1


@pytest.mark.parametrize("field", ["paths", "request_id"])
async def test_token_cannot_be_echoed_or_persisted_through_other_fields(setup, field):
    broker, backend, args, workspace, state, now = setup
    bad = dict(args)
    bad[field] = [f"outbox/{TASK}/{args['token']}"] if field == "paths" else args["token"]
    result = await broker.handoff(bad, "dsh")
    assert not result["success"] and args["token"] not in json.dumps(result)
    assert args["token"].encode() not in (state / "handoff.sqlite3").read_bytes()
    assert not backend.created


@pytest.mark.parametrize("cut", range(70))
def test_stream_redacts_every_token_split_without_releasing_prefix(cut):
    token = "dshh_" + "a" * 64
    redactor = StreamRedactor()
    first = redactor.feed("before " + token[:cut])
    assert first == "before " + ("[redacted-handoff-token]" if cut == 69 else "")
    rest = redactor.feed(token[cut:] + " after")
    assert first + rest + redactor.flush() == "before [redacted-handoff-token] after"


def test_stream_single_character_chunks_and_multiple_tokens():
    text = "中文 " + "dshh_" + "b" * 64 + " between " + "dshh_" + "1" * 64 + " done"
    redactor = StreamRedactor()
    output = []
    for char in text:
        output.append(redactor.feed(char))
        assert len(redactor._pending) <= 68
        assert "dshh_" not in "".join(output)
    assert "".join(output) + redactor.flush() == "中文 [redacted-handoff-token] between [redacted-handoff-token] done"
    assert redactor.flush() == ""


@pytest.mark.parametrize("prefix", ["dshh_", "dshh_a", "dshh_" + "9" * 63])
def test_stream_flush_conservatively_hides_incomplete_token(prefix):
    redactor = StreamRedactor()
    assert redactor.feed("ordinary " + prefix) == "ordinary "
    assert redactor.flush() == "[redacted-handoff-token]"
    assert redactor.feed("more text") + redactor.flush() == "more text"


@pytest.mark.parametrize("prefix", ["d", "ds", "dsh", "dshh"])
def test_stream_flush_preserves_short_ordinary_prefixes(prefix):
    redactor = StreamRedactor()
    assert redactor.feed(prefix) == ""
    assert redactor.flush() == prefix


@pytest.mark.parametrize("text", ["normal body", "dshh_z", "ds hi", "dshh_aX", "", "mdshh Z"])
def test_stream_preserves_non_token_text(text):
    redactor = StreamRedactor()
    result = "".join(redactor.feed(char) for char in text) + redactor.flush()
    assert result == text


@pytest.mark.parametrize(
    "state,workspace",
    [
        ("/data/shared/state", "/data/shared"),
        ("/data/state", "/data/state/shared"),
        ("/data/equal", "/data/equal"),
        ("/data/shared/../private", "/data/private/workspace"),
    ],
)
def test_constructor_rejects_workspace_and_state_directory_overlap(state, workspace):
    with pytest.raises(HandoffError, match="invalid_config"):
        HandoffBroker(Path(state), "dsh", HandoffConfig(workspace), Backend())


def test_constructor_allows_separate_sibling_directories():
    HandoffBroker(Path("/data/workspace-state"), "dsh", HandoffConfig("/data/workspace"), Backend())
