"""DSH helper prepares verifiable metadata, without SDK, shell, network or upload."""

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[1] / "tools/dsh_handoff.py"


@pytest.fixture(scope="module")
def helper():
    spec = importlib.util.spec_from_file_location("dsh_helper_under_test", HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "shared"
    (root / "outbox/job").mkdir(parents=True)
    return root


def put(root, relative="outbox/job/报告.pdf", content=b"\x00\xff\x80binary"):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return relative


def manifest(helper, workspace):
    return helper.prepare_manifest(workspace, [put(workspace)], task_id="a" * 32)


def run_helper(*args, **kwargs):
    # -S makes installed SDK/site-packages unavailable: this must be stdlib-only.
    return subprocess.run(
        [sys.executable, "-S", str(HELPER), *map(str, args)],
        capture_output=True,
        check=False,
        **kwargs,
    )


def test_prepare_multiple_chinese_and_binary(helper, workspace):
    paths = [put(workspace), put(workspace, "outbox/job/第二 张.png", bytes(range(256)))]
    before = {path: (workspace / path).read_bytes() for path in paths}
    result = helper.prepare_manifest(workspace, paths, task_id="a" * 32)
    assert result == {
        "type": "dsh_artifacts",
        "version": 1,
        "task_id": "a" * 32,
        "files": [
            {"path": path, "size": len(before[path]), "sha256": hashlib.sha256(before[path]).hexdigest()}
            for path in paths
        ],
    }
    assert {path: (workspace / path).read_bytes() for path in paths} == before
    assert sorted(path.name for path in (workspace / "outbox/job").iterdir()) == ["报告.pdf", "第二 张.png"]
    assert "send" not in result and "target" not in result and "chat_id" not in result


def test_generated_task_ids_are_random_identifiers_not_authorization(helper, workspace):
    relative = put(workspace)
    first = helper.prepare_manifest(workspace, [relative])
    second = helper.prepare_manifest(workspace, [relative])
    assert first["task_id"] != second["task_id"]
    assert len(first["task_id"]) == 32
    assert set(first) == {"type", "version", "task_id", "files"}
    assert "token" not in json.dumps(first)


@pytest.mark.parametrize("task_id", ["", "a", "A" * 32, "g" * 32, "../chat", 42])
def test_prepare_rejects_invalid_task_id(helper, workspace, task_id):
    with pytest.raises(ValueError):
        helper.prepare_manifest(workspace, [put(workspace)], task_id=task_id)


@pytest.mark.parametrize("path", ["../secret", "/etc/passwd", "outbox/../a", "outbox/./a", "outbox//a", "inbox/a"])
def test_prepare_rejects_traversal(helper, workspace, path):
    with pytest.raises((ValueError, OSError)):
        helper.prepare_manifest(workspace, [path])


def test_prepare_rejects_file_and_directory_symlinks(helper, workspace, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_bytes(b"private")
    (workspace / "outbox/link").symlink_to(outside / "secret")
    (workspace / "outbox/linked-dir").symlink_to(outside, target_is_directory=True)
    for relative in ("outbox/link", "outbox/linked-dir/secret"):
        with pytest.raises((ValueError, OSError)):
            helper.prepare_manifest(workspace, [relative])


def test_prepare_missing_file(helper, workspace):
    with pytest.raises(FileNotFoundError):
        helper.prepare_manifest(workspace, ["outbox/job/missing"])


def test_prepare_enforces_file_size_and_batch_limits(helper, workspace):
    first = put(workspace, content=b"1234")
    second = put(workspace, "outbox/job/second", b"x")
    with pytest.raises(ValueError):
        helper.prepare_manifest(workspace, [first], max_file_bytes=3)
    with pytest.raises(ValueError):
        helper.prepare_manifest(workspace, [first, second], max_batch_files=1)
    with pytest.raises(ValueError):
        helper.prepare_manifest(workspace, [first, first])
    assert helper.prepare_manifest(workspace, [first], max_file_bytes=4)["files"][0]["size"] == 4


@pytest.mark.parametrize("limit", [0, -1, True, 1024**3 + 1])
def test_invalid_limits(helper, workspace, limit):
    with pytest.raises(ValueError):
        helper.prepare_manifest(workspace, [put(workspace)], max_file_bytes=limit)


def test_relay_generates_only_existing_agent_talk_arguments(helper, workspace):
    prepared = manifest(helper, workspace)
    relative = put(workspace, "outbox/job/handoff.json", json.dumps(prepared, ensure_ascii=False).encode())
    result = helper.relay_arguments(workspace, relative, "ORIGINAL_HOST_REPLY_TARGET")
    assert set(result) == {"target", "text"}
    assert result["target"] == "ORIGINAL_HOST_REPLY_TARGET"
    assert "dsh_import_handoff(manifest=" in result["text"]
    assert "send=false" in result["text"]
    assert "unknown" in result["text"]
    assert "真实 file_ref" in result["text"]
    assert "实际工具结果" in result["text"]
    instruction = json.loads(result["text"].split("\n", 1)[1])
    assert instruction == {"tool": "dsh_import_handoff", "arguments": {"manifest": prepared, "send": False}}
    assert "target" not in instruction["arguments"]
    assert "session_id" not in instruction["arguments"]


@pytest.mark.parametrize("target", ["", "  ", "chat\nother", "chat\x00", "chat\x7f", "x" * 2049, None])
def test_relay_rejects_invalid_reply_target(helper, workspace, target):
    with pytest.raises(ValueError):
        helper.relay_arguments(workspace, "outbox/job/handoff.json", target)


def test_relay_rejects_oversized_manifest(helper, workspace):
    relative = put(workspace, "outbox/job/handoff.json", b" " * (64 * 1024 + 1))
    with pytest.raises(ValueError):
        helper.relay_arguments(workspace, relative, "original-target")


@pytest.mark.parametrize(
    "content",
    [
        b'{"type":"dsh_artifacts","type":"dsh_artifacts"}',
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b"[]",
        b"null",
        b"{invalid}",
        b"\xff",
    ],
)
def test_relay_rejects_non_strict_json(helper, workspace, content):
    relative = put(workspace, "outbox/job/handoff.json", content)
    with pytest.raises((ValueError, UnicodeError)):
        helper.relay_arguments(workspace, relative, "original-target")


@pytest.mark.parametrize("change", ["extra_chat", "extra_send", "bad_hash", "boolean_size", "bad_version", "bad_path"])
def test_relay_validates_strict_manifest(helper, workspace, change):
    prepared = manifest(helper, workspace)
    if change == "extra_chat":
        prepared["session_id"] = "other-chat"
    elif change == "extra_send":
        prepared["send"] = True
    elif change == "bad_hash":
        prepared["files"][0]["sha256"] = "not-a-digest"
    elif change == "boolean_size":
        prepared["files"][0]["size"] = True
    elif change == "bad_version":
        prepared["version"] = 2
    else:
        prepared["files"][0]["path"] = "outbox/../secret"
    relative = put(workspace, "outbox/job/handoff.json", json.dumps(prepared).encode())
    with pytest.raises(ValueError):
        helper.relay_arguments(workspace, relative, "original-target")


def test_cli_prepare_stdout_is_utf8_json_without_sdk_or_success_claim(workspace):
    relative = put(workspace)
    environment = {**os.environ, "LC_ALL": "C", "PYTHONIOENCODING": "ascii"}
    result = run_helper("prepare", "--root", workspace, "--task-id", "b" * 32, relative, env=environment)
    assert result.returncode == 0, result.stderr.decode("utf-8")
    parsed = json.loads(result.stdout.decode("utf-8"))
    assert parsed["files"][0]["path"] == relative
    assert "报告.pdf" in result.stdout.decode("utf-8")
    assert set(parsed) == {"type", "version", "task_id", "files"}
    assert "prepared_only" in result.stderr.decode("utf-8")
    assert "尚未调用 agent_talk" in result.stderr.decode("utf-8")
    assert "delivered" not in result.stdout.decode("utf-8")
    assert "registered" not in result.stdout.decode("utf-8")


def test_cli_prepare_failure_never_emits_partial_manifest(workspace):
    relative = put(workspace)
    result = run_helper("prepare", "--root", workspace, relative, "outbox/job/missing")
    assert result.returncode == 1
    assert result.stdout == b""
    assert b"error:" in result.stderr
    assert not (workspace / "outbox/job/handoff.json").exists()


def test_cli_prepare_rejects_unsupported_output_option(workspace):
    result = run_helper("prepare", "--root", workspace, "--output", "outbox/handoff.json", put(workspace))
    assert result.returncode == 2
    assert result.stdout == b""
    assert not (workspace / "outbox/handoff.json").exists()


def test_cli_relay_outputs_parameters_but_does_not_call_agent_talk(helper, workspace):
    prepared = manifest(helper, workspace)
    relative = put(workspace, "outbox/job/handoff.json", json.dumps(prepared).encode())
    result = run_helper("relay", "--root", workspace, "--manifest", relative, "--target", "original-target")
    assert result.returncode == 0
    assert set(json.loads(result.stdout)) == {"target", "text"}
    assert "未执行 agent_talk" in result.stderr.decode("utf-8")
    assert "未登记或发送文件" in result.stderr.decode("utf-8")


def test_cli_relay_failure_has_no_stdout(workspace):
    relative = put(workspace, "outbox/job/broken.json", b"not JSON")
    result = run_helper("relay", "--root", workspace, "--manifest", relative, "--target", "original-target")
    assert result.returncode == 1
    assert result.stdout == b""
    assert b"error:" in result.stderr


def publish_request(helper, workspace, *, content=None):
    prepared = manifest(helper, workspace)
    raw = json.dumps(prepared, ensure_ascii=False).encode() if content is None else content
    put(workspace, f"outbox/.dsh-handoffs/{prepared['task_id']}.json", raw)
    return prepared["task_id"], hashlib.sha256(raw).hexdigest()


def test_request_relay_uses_exact_confirmed_bytes_without_sdk(helper, workspace):
    task, sha = publish_request(helper, workspace)
    result = run_helper(
        "relay-request", "--root", workspace, "--task-id", task, "--request-sha256", sha, "--target", "original-route"
    )
    assert result.returncode == 0, result.stderr
    relay = json.loads(result.stdout)
    assert set(relay) == {"target", "text"}
    assert relay["target"] == "original-route"
    instruction = json.loads(relay["text"].split("\n", 1)[1])
    assert instruction == {"tool": "dsh_import_request", "arguments": {"task_id": task, "request_sha256": sha}}
    assert "仅在这是本聊天发起的任务" in relay["text"]
    assert "不证明原聊天身份或用户授权" in relay["text"]
    assert "unknown/unconfirmed" in relay["text"]
    assert "prepared_only" in result.stderr.decode()
    assert "未执行 agent_talk，未登记或发送文件" in result.stderr.decode()
    # No Host ledger, receipt or upload artifact is created by this stdlib CLI.
    assert sorted(path.name for path in workspace.iterdir()) == ["outbox"]


@pytest.mark.parametrize("target", ["", " ", "route\nother", "route\x00", "route\x7f", "x" * 2049, None])
def test_request_relay_rejects_bad_route_without_reading_request(helper, workspace, target):
    with pytest.raises(ValueError):
        helper.request_relay_arguments(workspace, "a" * 32, "b" * 64, target)


@pytest.mark.parametrize("sha", ["", "A" * 64, "g" * 64, "a" * 63, None, "0" * 64])
def test_request_relay_never_recomputes_an_explicit_hash_to_make_it_pass(helper, workspace, sha):
    task, _ = publish_request(helper, workspace)
    with pytest.raises(ValueError):
        helper.request_relay_arguments(workspace, task, sha, "original-route")


def test_request_relay_does_not_infer_a_task_or_fall_back_when_missing(helper, workspace):
    publish_request(helper, workspace)
    with pytest.raises(FileNotFoundError):
        helper.request_relay_arguments(workspace, "b" * 32, "0" * 64, "original-route")
    result = run_helper("relay-request", "--root", workspace, "--target", "original-route")
    assert result.returncode == 2 and result.stdout == b""


@pytest.mark.parametrize("relative", ["../secret", "/etc/passwd", "outbox/../secret", "a", "A" * 32])
def test_request_relay_rejects_path_like_task_identifiers(helper, workspace, relative):
    with pytest.raises(ValueError):
        helper.request_relay_arguments(workspace, relative, "a" * 64, "original-route")


def test_request_relay_rejects_symlink_even_if_contents_match(helper, workspace):
    task, sha = publish_request(helper, workspace)
    request = workspace / f"outbox/.dsh-handoffs/{task}.json"
    source = workspace / "outbox/job/source.json"
    request.rename(source)
    request.symlink_to(source)
    result = run_helper(
        "relay-request", "--root", workspace, "--task-id", task, "--request-sha256", sha, "--target", "original-route"
    )
    assert result.returncode == 1 and result.stdout == b""
    assert "prepared_only" in result.stderr.decode()


@pytest.mark.parametrize(
    "content",
    [b" " * (64 * 1024 + 1), b'{"task_id":null,"task_id":null}', b'{"size":NaN}', b"[" * 2000, b"\xff"],
)
def test_request_relay_invalid_request_never_emits_parameters(helper, workspace, content):
    task, sha = publish_request(helper, workspace, content=content)
    result = run_helper(
        "relay-request", "--root", workspace, "--task-id", task, "--request-sha256", sha, "--target", "original-route"
    )
    assert result.returncode == 1 and result.stdout == b""
    assert "error:" in result.stderr.decode()
    assert "Traceback" not in result.stderr.decode()


def test_request_relay_task_mismatch_cannot_select_another_request(helper, workspace):
    task, sha = publish_request(helper, workspace)
    request = workspace / f"outbox/.dsh-handoffs/{task}.json"
    request.rename(request.with_name("b" * 32 + ".json"))
    with pytest.raises(ValueError):
        helper.request_relay_arguments(workspace, "b" * 32, sha, "original-route")


def test_relay_deep_json_is_a_clean_cli_error(workspace):
    relative = put(workspace, "outbox/job/nested.json", b"[" * 2000)
    result = run_helper("relay", "--root", workspace, "--manifest", relative, "--target", "original-route")
    assert result.returncode == 1 and result.stdout == b""
    assert b"Traceback" not in result.stderr
