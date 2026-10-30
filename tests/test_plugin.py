"""Tool contract tests using real SDK declarations and mocked Host file scope.

The chat authorization fake checks that this plugin delegates to the official
capability; these are not MaiBot Host/Adapter integration tests.
"""

import asyncio
import hashlib
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.asyncio
REF = "a" * 32


async def test_import_binary_snapshot_and_metadata(bridge):
    content = bytes(range(256))
    (bridge.root / "outbox" / "archive.bin").write_bytes(content)
    result = await bridge.plugin.dsh_import(["outbox/archive.bin"])
    assert result["success"] is True
    item = result["items"][0]
    assert item["status"] == "imported"
    assert item["file"]["file_ref"] == "0" * 31 + "1"
    assert item["file"]["name"] == "archive.bin"
    assert item["file"]["size"] == 256
    assert item["file"]["sha256"] == hashlib.sha256(content).hexdigest()
    assert bridge.uploads[0][2] == content
    assert bridge.uploads[0][0].is_relative_to(bridge.runtime)
    assert not bridge.uploads[0][0].exists()
    assert "url" not in item["file"]
    bridge.files.send.assert_not_awaited()
    assert list(bridge.runtime.iterdir()) == []


async def test_batch_and_chinese_names(bridge):
    paths = ["outbox/报告.pdf", "outbox/图片.png", "outbox/空 格.txt"]
    for i, relative in enumerate(paths):
        (bridge.root / relative).write_bytes(bytes([i, 255]))
    result = await bridge.plugin.dsh_import(paths)
    assert result["success"] is True
    assert [item["path"] for item in result["items"]] == paths
    assert [item["file"]["name"] for item in result["items"]] == ["报告.pdf", "图片.png", "空 格.txt"]
    assert bridge.files.upload.await_count == 3


@pytest.mark.parametrize(
    "paths,send",
    [
        ([], False),
        ("outbox/a", False),
        ([None], False),
        (["outbox/a", "outbox/a"], False),
        (["outbox/a"], "true"),
        ([f"outbox/{i}" for i in range(9)], False),
    ],
)
async def test_invalid_batch_arguments_no_side_effects(bridge, paths, send):
    result = await bridge.plugin.dsh_import(paths, send=send)
    assert result["error"]["code"] == "invalid_arguments"
    bridge.files.upload.assert_not_awaited()
    bridge.files.send.assert_not_awaited()


@pytest.mark.parametrize("relative", ["../secret", "/etc/passwd", "outbox/../inbox/a", "inbox/a"])
async def test_import_invalid_path_never_uploads(bridge, relative):
    result = await bridge.plugin.dsh_import([relative])
    assert result["success"] is False
    assert result["items"][0]["status"] == "failed"
    bridge.files.upload.assert_not_awaited()


async def test_missing_file(bridge):
    result = await bridge.plugin.dsh_import(["outbox/missing"])
    assert result["items"][0]["error"]["code"] == "not_found"
    bridge.files.upload.assert_not_awaited()


async def test_import_symlink(bridge, tmp_path):
    source = tmp_path / "outside"
    source.write_bytes(b"secret")
    (bridge.root / "outbox" / "link").symlink_to(source)
    result = await bridge.plugin.dsh_import(["outbox/link"])
    assert result["success"] is False
    bridge.files.upload.assert_not_awaited()


async def test_import_size_limit(bridge):
    bridge.plugin.set_plugin_config(
        {
            "plugin": {"config_version": "1.0.0"},
            "workspace": {"root": str(bridge.root), "max_file_bytes": 3},
        }
    )
    (bridge.root / "outbox" / "large").write_bytes(b"1234")
    result = await bridge.plugin.dsh_import(["outbox/large"])
    assert result["success"] is False
    bridge.files.upload.assert_not_awaited()
    assert list(bridge.runtime.iterdir()) == []


async def test_import_permission_denied(bridge, plugin_module, monkeypatch):
    def denied(*args):
        raise PermissionError("sensitive private path")

    monkeypatch.setattr(plugin_module.Workspace, "snapshot", denied)
    result = await bridge.plugin.dsh_import(["outbox/a"])
    assert result["items"][0]["error"]["code"] == "permission_denied"
    assert "sensitive" not in repr(result)
    bridge.files.upload.assert_not_awaited()


async def test_upload_failure_no_send_no_sensitive_error(bridge):
    (bridge.root / "outbox" / "a").write_bytes(b"hello")
    bridge.files.upload.side_effect = RuntimeError("https://host.invalid/transfer?token=secret")
    result = await bridge.plugin.dsh_import(["outbox/a"], send=True)
    assert result["success"] is False
    assert result["items"][0]["retry_safe"] is False
    assert "secret" not in repr(result)
    assert "host.invalid" not in repr(result)
    bridge.files.upload.assert_awaited_once()
    bridge.files.send.assert_not_awaited()
    assert list(bridge.runtime.iterdir()) == []


async def test_partial_batch_preserves_success_and_continues(bridge):
    (bridge.root / "outbox" / "a").write_bytes(b"a")
    (bridge.root / "outbox" / "b").write_bytes(b"b")
    result = await bridge.plugin.dsh_import(["outbox/a", "outbox/missing", "outbox/b"])
    assert result["success"] is False
    assert [item["status"] for item in result["items"]] == ["imported", "failed", "imported"]
    assert bridge.files.upload.await_count == 2


async def test_send_sent_once(bridge):
    (bridge.root / "outbox" / "a").write_bytes(b"a")
    result = await bridge.plugin.dsh_import(["outbox/a"], send=True)
    assert result["success"] is True
    assert result["items"][0]["send"] == {"status": "sent", "message_id": "message-1", "retry_safe": False}
    bridge.files.send.assert_awaited_once_with("0" * 31 + "1")


@pytest.mark.parametrize("receipt", [{"status": "unknown"}, {"status": "failed"}, {}])
async def test_uncertain_send_never_retries_or_loses_file_ref(bridge, receipt):
    (bridge.root / "outbox" / "a").write_bytes(b"a")
    bridge.files.send.return_value = receipt
    result = await bridge.plugin.dsh_import(["outbox/a"], send=True)
    assert result["success"] is False
    item = result["items"][0]
    assert item["status"] == "imported"
    assert item["file"]["file_ref"]
    assert item["send"] == {"status": "unknown", "retry_safe": False}
    bridge.files.upload.assert_awaited_once()
    bridge.files.send.assert_awaited_once()


async def test_send_exception_is_unknown_without_retry(bridge):
    (bridge.root / "outbox" / "a").write_bytes(b"a")
    bridge.files.send.side_effect = TimeoutError("signed URL")
    result = await bridge.plugin.dsh_import(["outbox/a"], send=True)
    assert result["items"][0]["send"]["status"] == "unknown"
    assert "signed URL" not in repr(result)
    bridge.files.send.assert_awaited_once()


async def test_export_binary_with_chinese_destination(bridge):
    result = await bridge.plugin.dsh_export(REF, "inbox/task/中文.zip")
    assert result["success"] is True
    assert result["path"] == "inbox/task/中文.zip"
    assert result["file"]["file_ref"] == REF
    assert (bridge.root / "inbox/task/中文.zip").read_bytes() == b"\x00\xffdownload"
    args, kwargs = bridge.files.download.call_args
    assert args[0] == REF and not kwargs
    assert Path(args[1]).is_relative_to(bridge.runtime)
    assert list(bridge.runtime.iterdir()) == []


@pytest.mark.parametrize("ref", ["", "abc", "../a", "A" * 32, "g" * 32, None, 123, "a" * 33])
async def test_invalid_file_ref_never_downloads(bridge, ref):
    result = await bridge.plugin.dsh_export(ref, "inbox/a")
    assert result["error"]["code"] == "invalid_file_ref"
    bridge.files.download.assert_not_awaited()


async def test_valid_format_but_missing_file_ref_delegated_to_host(bridge):
    bridge.files.download.side_effect = FileNotFoundError("missing")
    result = await bridge.plugin.dsh_export(REF, "inbox/a")
    assert result["error"]["code"] == "not_found"
    bridge.files.download.assert_awaited_once()
    assert not (bridge.root / "inbox/a").exists()


@pytest.mark.parametrize("target", ["/etc/passwd", "inbox/../a", "outbox/a", "../inbox/a"])
async def test_invalid_export_path_rejected_before_download(bridge, target):
    result = await bridge.plugin.dsh_export(REF, target)
    assert result["success"] is False
    bridge.files.download.assert_not_awaited()


async def test_existing_export_target_not_overwritten(bridge):
    target = bridge.root / "inbox/existing"
    target.write_bytes(b"keep")
    result = await bridge.plugin.dsh_export(REF, "inbox/existing")
    assert result["error"]["code"] == "already_exists"
    assert target.read_bytes() == b"keep"
    bridge.files.download.assert_not_awaited()


async def test_download_failure_cleans_stage_and_never_publishes(bridge):
    async def broken(ref, destination):
        Path(destination).write_bytes(b"partial")
        raise ValueError("checksum mismatch at https://private.invalid/token")

    bridge.files.download.side_effect = broken
    result = await bridge.plugin.dsh_export(REF, "inbox/a")
    assert result["success"] is False
    assert "private.invalid" not in repr(result)
    assert not (bridge.root / "inbox/a").exists()
    assert list(bridge.runtime.iterdir()) == []


async def test_cancelled_download_cleans_and_propagates(bridge):
    async def cancelled(ref, destination):
        Path(destination).write_bytes(b"partial")
        raise asyncio.CancelledError

    bridge.files.download.side_effect = cancelled
    with pytest.raises(asyncio.CancelledError):
        await bridge.plugin.dsh_export(REF, "inbox/a")
    assert not (bridge.root / "inbox/a").exists()
    assert list(bridge.runtime.iterdir()) == []


async def test_export_size_limit_before_publish(bridge):
    bridge.plugin.set_plugin_config(
        {
            "plugin": {"config_version": "1.0.0"},
            "workspace": {"root": str(bridge.root), "max_file_bytes": 1},
        }
    )
    result = await bridge.plugin.dsh_export(REF, "inbox/a")
    assert result["success"] is False
    bridge.files.download.assert_awaited_once()
    assert not (bridge.root / "inbox/a").exists()
    assert list(bridge.runtime.iterdir()) == []


async def test_export_scope_is_host_controlled_not_session_kwarg(bridge):
    # Deliberately stateful Mock contract: only the trusted current Host scope can
    # authorize attachments. This does not claim to test Host's database policy.
    current_chat = "chat-b"
    owner_chat = "chat-a"

    async def scoped_download(ref, destination):
        assert ref == REF
        if current_chat != owner_chat:
            raise PermissionError("other chat")
        Path(destination).write_bytes(b"authorized")
        return {"file_ref": ref, "size": 10}

    bridge.files.download = AsyncMock(side_effect=scoped_download)
    result = await bridge.plugin.dsh_export(REF, "inbox/denied", session_id="chat-a", chat_id="chat-a")
    assert result["error"]["code"] == "permission_denied"
    assert not (bridge.root / "inbox/denied").exists()
    current_chat = "chat-a"
    result = await bridge.plugin.dsh_export(REF, "inbox/allowed", session_id="chat-b")
    assert result["success"] is True
    assert (bridge.root / "inbox/allowed").read_bytes() == b"authorized"
    assert all(not call.kwargs for call in bridge.files.download.await_args_list)


async def test_import_does_not_forward_invented_scope(bridge):
    (bridge.root / "outbox" / "a").write_bytes(b"a")
    result = await bridge.plugin.dsh_import(["outbox/a"], send=True, session_id="invented")
    assert result["success"] is True
    assert bridge.files.upload.call_args.kwargs == {"name": "a"}
    assert not bridge.files.send.call_args.kwargs


async def test_disabled_tools_do_not_touch_files(bridge):
    bridge.plugin.set_plugin_config({"plugin": {"config_version": "1.0.0", "enabled": False}})
    assert (await bridge.plugin.dsh_import(["outbox/a"]))["error"]["code"] == "disabled"
    assert (await bridge.plugin.dsh_export(REF, "inbox/a"))["error"]["code"] == "disabled"
    bridge.files.upload.assert_not_awaited()
    bridge.files.download.assert_not_awaited()


async def test_lifecycle_never_uses_file_capability(bridge):
    await bridge.plugin.on_load()
    await bridge.plugin.on_config_update("self", {}, "1")
    await bridge.plugin.on_unload()
    bridge.files.upload.assert_not_awaited()
    bridge.files.download.assert_not_awaited()
    bridge.files.send.assert_not_awaited()


async def test_real_sdk_tool_metadata_and_schema(plugin_module):
    plugin = plugin_module.create_plugin()
    components = plugin.get_components()
    tools = {item["name"]: item for item in components}
    assert set(tools) == {"dsh_import", "dsh_export", "dsh_import_handoff", "dsh_handoff_status"}
    for name in tools:
        info = getattr(plugin, name).__maibot_component_info__
        assert info.metadata["visibility"] == "visible"
        schema = info.get_parameters_schema()
        assert "session_id" not in schema["properties"]
    schema = plugin.dsh_import.__maibot_component_info__.get_parameters_schema()
    assert schema["properties"]["paths"]["type"] == "array"
    assert schema["properties"]["paths"]["items"] == {"type": "string"}
    assert schema["properties"]["send"]["default"] is False
    assert "send" not in schema["required"]
