"""Real v1.3.18 FileService/Store/ToolRegistry + SQLite component integration.

Only chat discovery and QQ transport are doubles. No production bot, LLM, ACP
process, SSH connection, HTTP transfer or SnowLuma client is started here.
"""

import importlib
import os
import shutil
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from integrations.maibot_1_3_18.install import install


@pytest.fixture(scope="module")
def host_modules(tmp_path_factory):
    configured = os.environ.get("MAIBOT_SOURCE_PATH")
    if not configured:
        pytest.skip("MAIBOT_SOURCE_PATH required for real Host component integration")
    source = Path(configured)
    root = tmp_path_factory.mktemp("real-host")
    shutil.copytree(source / "src", root / "src", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copyfile(source / "pyproject.toml", root / "pyproject.toml")
    assert install(root, apply=True) == "applied"
    saved = {key: value for key, value in sys.modules.items() if key == "src" or key.startswith("src.")}
    for key in saved:
        del sys.modules[key]
    with pytest.MonkeyPatch.context() as patch:
        patch.syspath_prepend(str(root))
        patch.chdir(root)
        modules = SimpleNamespace(
            service=importlib.import_module("src.files.service"),
            store=importlib.import_module("src.files.store"),
            config=importlib.import_module("src.files.config"),
            tooling=importlib.import_module("src.core.tooling"),
            provider=importlib.import_module("src.agent.artifact_provider"),
            broker=importlib.import_module("src.agent.artifact_handoff"),
        )
        yield modules
    for key in list(sys.modules):
        if key == "src" or key.startswith("src."):
            del sys.modules[key]
    sys.modules.update(saved)


@pytest.fixture
def connected(host_modules, tmp_path, monkeypatch):
    from sqlmodel import SQLModel, create_engine

    host = host_modules
    engine = create_engine("sqlite:///" + str(tmp_path / "files.sqlite3"))
    SQLModel.metadata.create_all(engine)
    store = host.store.FileStore(tmp_path / "files", engine, host.config.FilesConfig())
    service = host.service.FileService(store, enabled_provider=lambda: True)
    chats = {"original-chat", "other-chat"}

    def require_session(chat):
        if chat not in chats:
            raise ValueError("Unknown chat")
        return SimpleNamespace(session_id=chat)

    monkeypatch.setattr(service, "require_session", require_session)
    monkeypatch.setattr(host.service, "get_file_service", lambda: service)
    deliveries = []
    transport = ModuleType("src.services.send_service")

    async def send(kind, asset, chat, **kwargs):
        deliveries.append((kind, asset["file_ref"], chat))
        return None  # An uncertain real FileService.send receipt, not an SDK mock.

    transport.custom_to_stream_with_message = send
    monkeypatch.setitem(sys.modules, "src.services.send_service", transport)
    shared = tmp_path / "shared"
    shared.mkdir()
    broker = host.broker.HandoffBroker(
        tmp_path / "state",
        "__maibot_agent__",
        host.broker.HandoffConfig(str(shared), allow_send=True),
        host.provider.HostFiles(),
    )
    agent = SimpleNamespace(service=SimpleNamespace(agent_id="__maibot_agent__"), artifacts=broker)
    provider = host.provider.ArtifactToolProvider(agent)
    registry = host.tooling.ToolRegistry()
    registry.register_provider(provider)
    yield SimpleNamespace(
        host=host, broker=broker, registry=registry, store=store, shared=shared, deliveries=deliveries
    )
    engine.dispose()


async def make_request(connected, send=False):
    task = "a" * 32
    await connected.broker.issue(task, "original-chat", send_allowed=send)
    grant = await connected.broker.grant(task)
    folder = connected.shared / grant["outbox_prefix"]
    folder.mkdir(parents=True)
    paths = []
    for name, content in [("报告.pdf", b"\x00\xffPDF"), ("图片.png", bytes(range(256)))]:
        (folder / name).write_bytes(content)
        paths.append(grant["outbox_prefix"] + name)
    return {"task_id": task, "token": grant["token"], "request_id": "batch-1", "paths": paths, "send": send}


async def invoke(connected, arguments, caller="__maibot_agent__"):
    return await connected.registry.invoke(
        connected.host.tooling.ToolInvocation("agent_artifact_handoff", arguments),
        connected.host.tooling.ToolExecutionContext(session_id=caller),
    )


@pytest.mark.asyncio
async def test_real_host_registers_binary_files_in_original_chat_and_replays_refs(connected):
    request = await make_request(connected)
    result = await invoke(connected, request)
    assert result.success, result.get_history_content()
    items = result.structured_content["items"]
    assets = connected.store.list("original-chat")
    assert len(assets) == 2
    assert {asset["file_ref"] for asset in assets} == {item["file"]["file_ref"] for item in items}
    assert {asset["name"] for asset in assets} == {"报告.pdf", "图片.png"}
    for item, content in zip(items, [b"\x00\xffPDF", bytes(range(256))], strict=True):
        ref = item["file"]["file_ref"]
        assert connected.store.read_bytes(ref, "original-chat") == content
        with pytest.raises(PermissionError):
            connected.store.get(ref, "other-chat")
    assert connected.store.list("other-chat") == []
    repeated = await invoke(connected, request)
    assert repeated.structured_content == result.structured_content
    assert len(connected.store.list("original-chat")) == 2


@pytest.mark.asyncio
async def test_real_host_unknown_send_is_not_repeated(connected):
    request = await make_request(connected, send=True)
    result = await invoke(connected, request)
    assert not result.success
    assert all(item["send"]["status"] == "unknown" for item in result.structured_content["items"])
    assert len(connected.deliveries) == 2
    assert all(chat == "original-chat" for _, _, chat in connected.deliveries)
    await invoke(connected, request)
    assert len(connected.deliveries) == 2
    assert len(connected.store.list("original-chat")) == 2


@pytest.mark.asyncio
async def test_real_registry_rejects_chat_impersonation(connected):
    request = await make_request(connected)
    assert not (await invoke(connected, request, caller="other-chat")).success
    assert not (await invoke(connected, {**request, "session_id": "other-chat"})).success
    assert not (await invoke(connected, {**request, "target": "other-chat"})).success
    assert connected.store.list("original-chat") == []
    assert connected.store.list("other-chat") == []


@pytest.mark.asyncio
async def test_real_mcp_transport_handoff_without_planner(connected):
    import json

    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    mcp_module = importlib.import_module("src.agent.mcp_server")
    provider = connected.host.provider.ArtifactToolProvider(
        SimpleNamespace(artifacts=connected.broker, service=SimpleNamespace(agent_id="__maibot_agent__"))
    )
    server = mcp_module.ToolMCPServer([provider], "__maibot_agent__", 10)
    arguments = await make_request(connected)
    try:
        mount = await server.start()
        # This client only contacts the local test server. No external URL or credential.
        async with httpx.AsyncClient(
            headers={"Authorization": "Bearer " + server.token}, trust_env=False, timeout=10
        ) as http:
            async with streamable_http_client(mount["url"], http_client=http) as (read, write, _):
                async with ClientSession(read, write) as client:
                    await client.initialize()
                    tools = await client.list_tools()
                    assert [tool.name for tool in tools.tools] == ["agent_artifact_handoff"]
                    result = await client.call_tool("agent_artifact_handoff", arguments)
                    assert not result.isError
                    data = json.loads(result.content[0].text)
                    assert len(data["items"]) == 2
                    for item in data["items"]:
                        assert connected.store.get(item["file"]["file_ref"], "original-chat").state == "ready"
                    repeated = await client.call_tool("agent_artifact_handoff", arguments)
                    assert json.loads(repeated.content[0].text) == data
                    assert len(connected.store.list("original-chat")) == 2
    finally:
        await server.close()


def host_method(host_modules, module_file, class_name, method_name, extra=None):
    """Execute the shipped patched method without starting the full bot services."""
    import ast

    source_root = Path(host_modules.service.__file__).parents[1]
    tree = ast.parse((source_root / "agent" / module_file).read_text())
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
    method = next(node for node in cls.body if isinstance(node, ast.AsyncFunctionDef) and node.name == method_name)
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    compiled = ast.fix_missing_locations(ast.Module(body=[future, method], type_ignores=[]))
    namespace = {"redact_tokens": host_modules.broker.redact_tokens, **(extra or {})}
    exec(compile(compiled, module_file, "exec"), namespace)
    return namespace[method_name]


@pytest.mark.asyncio
async def test_patched_acp_update_redacts_every_token_split(host_modules):
    from unittest.mock import AsyncMock

    context = importlib.import_module("src.llm_models.payload_content.context_item")
    method = host_method(host_modules, "acp_agent.py", "AcpAgent", "_update", vars(context))
    token = "dshh_" + "f" * 64
    for kind in ("agent_message_chunk", "agent_thought_chunk"):
        for split in range(1, len(token)):
            history = SimpleNamespace(append=AsyncMock())

            async def record(record_kind, items):
                return {"kind": record_kind, "items": items}

            agent = SimpleNamespace(
                backend_state={"session_id": "acp-session"},
                artifacts=object(),
                history=history,
                record=record,
                _artifact_streams={kind: host_modules.broker.StreamRedactor()},
            )
            for text in (token[:split], token[split:]):
                await method(
                    agent,
                    {
                        "sessionId": "acp-session",
                        "update": {
                            "sessionUpdate": kind,
                            "content": {"type": "text", "text": text},
                        },
                    },
                )
            persisted = "".join(
                part.text
                for call in history.append.await_args_list
                for item in call.args[0]["items"]
                for part in (item.parts if hasattr(item, "parts") else [])
            )
            # Also inspect reasoning repr, where text is stored in text_parts.
            persisted += "".join(str(call.args[0]) for call in history.append.await_args_list)
            assert token not in persisted
            assert "f" * 64 not in persisted


@pytest.mark.asyncio
async def test_patched_stderr_suppresses_split_token_lines(host_modules):
    import asyncio
    from collections import deque
    from unittest.mock import Mock

    logger = Mock()
    method = host_method(host_modules, "acp.py", "ACPConnection", "_read_errors", {"logger": logger})
    stderr = asyncio.StreamReader()
    stderr.feed_data(b"dshh_" + b"f" * 32 + b"\n" + b"f" * 32 + b"\n")
    stderr.feed_eof()
    connection = SimpleNamespace(process=SimpleNamespace(stderr=stderr), _errors=deque(), _sensitive_artifacts=True)
    await method(connection)
    assert all(line == "[stderr suppressed in artifact handoff mode]" for line in connection._errors)
    assert "f" * 32 not in str(logger.info.call_args_list)
