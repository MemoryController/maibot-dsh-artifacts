"""Official SDK plugin wiring on an unmodified Host-shaped file scope.

ctx.files is a contract double, not a real MaiBot/DSH/QQ deployment. No Host
installer or experimental integration module is imported or executed.
"""

import ast
import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def stock(bridge):
    data = bridge.runtime.parent / "data"
    data.mkdir()
    bridge.plugin._ctx.paths.data_dir = data

    async def listing(offset=0):
        values = [
            {
                "file_ref": f"{index:032x}",
                "name": name,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "state": "ready",
            }
            for index, (_, name, content) in enumerate(bridge.uploads, 1)
        ]
        return values[offset : offset + 50]

    bridge.files.list = AsyncMock(side_effect=listing)
    bridge.data = data
    return bridge


def manifest_for(stock, names=("报告.pdf", "图片.png")):
    files = []
    for index, name in enumerate(names):
        content = bytes([index, 255, 0])
        path = "outbox/" + name
        (stock.root / path).write_bytes(content)
        files.append({"path": path, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()})
    return {"type": "dsh_artifacts", "version": 1, "task_id": "a" * 32, "files": files}


@pytest.mark.asyncio
async def test_stock_plugin_batch_import_real_sdk_declarations(stock):
    manifest = manifest_for(stock)
    result = await stock.plugin.dsh_import_handoff(manifest)
    assert result["tool_called"] is True
    assert result["registered"] == "confirmed"
    assert result["delivery"] == "not_requested"
    assert [item["file"]["name"] for item in result["items"]] == ["报告.pdf", "图片.png"]
    assert {item["file"]["file_ref"] for item in result["items"]} == {f"{i:032x}" for i in (1, 2)}
    assert stock.files.upload.await_count == 2
    stock.files.send.assert_not_awaited()
    cached = await stock.plugin.dsh_import_handoff(manifest)
    assert cached["cached"] is True
    assert stock.files.upload.await_count == 2


@pytest.mark.asyncio
async def test_notification_does_not_import_when_planner_never_calls_tool(stock):
    manifest = manifest_for(stock)
    # Preparing/queuing this text is deliberately not a tool call.
    relay = {"delivered": True, "queued": True, "text": json.dumps(manifest)}
    assert relay["delivered"]
    result = await stock.plugin.dsh_handoff_status(manifest["task_id"])
    assert result["state"] == "unconfirmed"
    assert result["tool_called"] == "unconfirmed"
    assert result["registered"] == "unconfirmed"
    assert not (stock.data / "handoff").exists()
    stock.files.upload.assert_not_awaited()
    stock.files.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_replay_and_status_cannot_select_chat_via_kwargs(stock):
    manifest = manifest_for(stock)
    original = await stock.plugin.dsh_import_handoff(manifest, session_id="forged-destination")
    refs = {item["file"]["file_ref"] for item in original["items"]}
    stock.files.list = AsyncMock(return_value=[])  # Actual current Host scope is another chat.
    status = await stock.plugin.dsh_handoff_status(manifest["task_id"], session_id="original-chat")
    replay = await stock.plugin.dsh_import_handoff(manifest, session_id="original-chat")
    assert status["state"] == replay["state"] == "unconfirmed"
    for value in (status, replay):
        assert not any(ref in repr(value) for ref in refs)
        assert "报告" not in repr(value)
    assert stock.files.upload.await_count == 2
    assert all(not call.kwargs.keys() - {"name"} for call in stock.files.upload.await_args_list)


@pytest.mark.asyncio
async def test_unknown_delivery_is_not_automatically_repeated(stock):
    manifest = manifest_for(stock)
    stock.files.send.return_value = {"status": "unknown", "message_id": "private-message"}
    result = await stock.plugin.dsh_import_handoff(manifest, send=True)
    assert result["registered"] == "confirmed"
    assert result["delivery"] == "unknown"
    assert not result["success"]
    await stock.plugin.dsh_import_handoff(manifest, send=True)
    status = await stock.plugin.dsh_handoff_status(manifest["task_id"])
    assert status["delivery"] == "unconfirmed"
    assert "private-message" not in repr(status)
    assert stock.files.send.await_count == 2
    assert stock.files.upload.await_count == 2


@pytest.mark.asyncio
async def test_partial_missing_file_never_claims_whole_batch_success(stock):
    manifest = manifest_for(stock)
    (stock.root / manifest["files"][1]["path"]).unlink()
    result = await stock.plugin.dsh_import_handoff(manifest)
    assert result["registered"] == "partial"
    assert not result["success"]
    assert result["items"][1]["status"] == "failed"
    assert stock.files.upload.await_count == 1
    await stock.plugin.dsh_import_handoff(manifest)
    assert stock.files.upload.await_count == 1


@pytest.mark.asyncio
async def test_manifest_cannot_request_delivery_or_chat(stock):
    manifest = manifest_for(stock)
    for key, value in [("send", True), ("session_id", "other"), ("target", "other")]:
        result = await stock.plugin.dsh_import_handoff({**manifest, key: value})
        assert result["success"] is False
    stock.files.upload.assert_not_awaited()
    stock.files.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_handoff_tools_use_no_file_capability(stock):
    manifest = manifest_for(stock)
    stock.plugin.set_plugin_config({"plugin": {"config_version": "1.0.0", "enabled": False}})
    assert not (await stock.plugin.dsh_import_handoff(manifest))["success"]
    assert not (await stock.plugin.dsh_handoff_status(manifest["task_id"]))["success"]
    stock.files.list.assert_not_awaited()
    stock.files.upload.assert_not_awaited()


@pytest.mark.asyncio
async def test_official_sdk_manifest_and_pure_plugin_install_without_host(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    destination = tmp_path / "plugins" / "maibot-dsh-artifacts"
    destination.mkdir(parents=True)
    for name in ("plugin.py", "handoff.py", "safe_files.py", "_manifest.json"):
        shutil.copyfile(root / name, destination / name)
    # This install has no Host source, patch, installer or external service.
    assert not list(tmp_path.rglob("host.patch"))
    assert not (tmp_path / "src").exists()
    for name in ("plugin.py", "handoff.py", "safe_files.py"):
        tree = ast.parse((destination / name).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith(("src", "integrations"))
            elif isinstance(node, ast.Import):
                assert all(not alias.name.startswith(("src", "integrations")) for alias in node.names)
    manifest = json.loads((destination / "_manifest.json").read_text())
    assert manifest["manifest_version"] == 2
    assert set(manifest["capabilities"]) == {"files.list", "files.upload", "files.download", "files.send"}
    name = "installed_stock_dsh_plugin"
    spec = importlib.util.spec_from_file_location(
        name, destination / "plugin.py", submodule_search_locations=[str(destination)]
    )
    installed = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, installed)
    spec.loader.exec_module(installed)
    workspace = tmp_path / "shared"
    workspace.mkdir()
    plugin = installed.create_plugin()
    plugin.set_plugin_config({"plugin": {"config_version": "1.0.0"}, "workspace": {"root": str(workspace)}})
    await plugin.on_load()  # Runs without Host sources or a file-capability scope.
    assert plugin.get_components()
    for tool in ("dsh_import_handoff", "dsh_handoff_status"):
        info = getattr(installed.DSHArtifactsPlugin, tool).__maibot_component_info__
        assert info.metadata["visibility"] == "visible"
        assert "session_id" not in info.get_parameters_schema()["properties"]
