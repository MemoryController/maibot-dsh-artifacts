"""Dedicated ACP Host adapter contracts, with a real broker and mocked Host I/O.

The small tooling stand-ins cover the dataclass interface only. These tests do
not start MaiBot, ACP transports, a Planner, or a real QQ adapter.
"""

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from integrations.maibot_1_3_18.artifact_handoff import HandoffBroker, HandoffConfig, HandoffError


@pytest.fixture
def adapter(monkeypatch):
    """Inject Host API-shaped modules for one test, then restore sys.modules."""
    tooling = ModuleType("src.core.tooling")
    for name in (
        "ToolAvailabilityContext",
        "ToolExecutionContext",
        "ToolExecutionResult",
        "ToolInvocation",
        "ToolSpec",
    ):
        setattr(tooling, name, SimpleNamespace)
    for name in ("src", "src.core", "src.files"):
        module = ModuleType(name)
        module.__path__ = []
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setitem(sys.modules, "src.core.tooling", tooling)
    name = "integrations.maibot_1_3_18._provider_under_test"
    path = Path(__file__).resolve().parents[1] / "integrations/maibot_1_3_18/artifact_provider.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def handoff(adapter, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    backend = SimpleNamespace(
        require_chat=AsyncMock(),
        create=AsyncMock(return_value={"file_ref": "f" * 32}),
        send=AsyncMock(return_value={"status": "sent"}),
    )
    now = [1000.0]
    broker = HandoffBroker(
        tmp_path / "state",
        "agent-dsh",
        HandoffConfig(str(workspace), allow_send=True),
        backend,
        clock=lambda: now[0],
    )
    agent = SimpleNamespace(artifacts=broker, service=SimpleNamespace(agent_id="agent-dsh"))
    return SimpleNamespace(
        broker=broker,
        backend=backend,
        provider=adapter.ArtifactToolProvider(agent),
        agent=agent,
        workspace=workspace,
        now=now,
    )


async def issued(handoff, *, send=False):
    task_id = "a" * 32
    await handoff.broker.issue(task_id, "real-original-chat", send_allowed=send)
    grant = await handoff.broker.grant(task_id)
    relative = f"{grant['outbox_prefix']}报告.pdf"
    path = handoff.workspace / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00\xffbinary PDF")
    return {
        "task_id": task_id,
        "token": grant["token"],
        "request_id": "one-result",
        "paths": [relative],
        "send": send,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("chat", [None, "ordinary-chat", "real-original-chat"])
async def test_tool_not_visible_to_non_agent_context(adapter, handoff, chat):
    context = None if chat is None else SimpleNamespace(session_id=chat)
    assert await handoff.provider.list_tools(context) == []


@pytest.mark.asyncio
async def test_tool_visible_only_to_agent_with_strict_schema(adapter, handoff):
    tools = await handoff.provider.list_tools(SimpleNamespace(session_id="agent-dsh"))
    assert len(tools) == 1
    tool = tools[0]
    assert tool.name == adapter.TOOL_NAME
    assert tool.metadata["visibility"] == "visible"
    assert tool.parameters_schema["additionalProperties"] is False
    assert "chat_id" not in tool.parameters_schema["properties"]
    assert "session_id" not in tool.parameters_schema["properties"]
    handoff.agent.artifacts = None
    assert await handoff.provider.list_tools(SimpleNamespace(session_id="agent-dsh")) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("chat", ["ordinary-chat", "real-original-chat"])
async def test_ordinary_chat_cannot_execute_even_with_valid_token(adapter, handoff, chat):
    arguments = await issued(handoff)
    result = await handoff.provider.invoke(
        SimpleNamespace(tool_name=adapter.TOOL_NAME, arguments=arguments),
        SimpleNamespace(session_id=chat),
    )
    assert result.success is False
    assert result.structured_content["error"]["code"] == "unauthorized"
    handoff.backend.create.assert_not_awaited()
    handoff.backend.send.assert_not_awaited()
    assert arguments["token"] not in result.content


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", ["chat_id", "session_id", "target"])
async def test_arguments_cannot_select_different_chat(adapter, handoff, extra):
    arguments = await issued(handoff)
    arguments[extra] = "victim-chat"
    result = await handoff.provider.invoke(
        SimpleNamespace(tool_name=adapter.TOOL_NAME, arguments=arguments),
        SimpleNamespace(session_id="agent-dsh"),
    )
    assert result.success is False
    assert result.structured_content["error"]["code"] == "unauthorized"
    handoff.backend.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_agent_handoff_uses_broker_bound_chat_for_create_and_send(adapter, handoff):
    arguments = await issued(handoff, send=True)
    snapshots = []

    async def create(chat, name, source):
        snapshots.append((chat, name, source.read_bytes()))
        assert source.stat().st_mode & 0o777 == 0o600
        return {"file_ref": "b" * 32}

    handoff.backend.create.side_effect = create
    result = await handoff.provider.invoke(
        SimpleNamespace(tool_name=adapter.TOOL_NAME, arguments=arguments),
        SimpleNamespace(session_id="agent-dsh"),
    )
    assert result.success is True
    assert snapshots == [("real-original-chat", "报告.pdf", b"\x00\xffbinary PDF")]
    handoff.backend.send.assert_awaited_once_with("b" * 32, "real-original-chat")
    assert result.structured_content["items"][0]["file"]["file_ref"] == "b" * 32
    assert arguments["token"] not in result.content
    assert json.loads(result.content) == result.structured_content


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["wrong_name", "no_context", "disabled"])
async def test_unavailable_invocation_has_sanitized_result(adapter, handoff, case):
    arguments = await issued(handoff)
    invocation = SimpleNamespace(tool_name=adapter.TOOL_NAME, arguments=arguments)
    context = SimpleNamespace(session_id="agent-dsh")
    if case == "wrong_name":
        invocation.tool_name = "other_tool"
    elif case == "no_context":
        context = None
    else:
        handoff.agent.artifacts = None
    result = await handoff.provider.invoke(invocation, context)
    assert result.success is False
    assert result.structured_content == {"success": False, "error": {"code": "handoff_unavailable"}}
    handoff.backend.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_unexpected_broker_error_never_reveals_secret(adapter):
    broker = SimpleNamespace(handoff=AsyncMock(side_effect=RuntimeError("token=secret https://internal/signed")))
    provider = adapter.ArtifactToolProvider(SimpleNamespace(artifacts=broker))
    result = await provider.invoke(
        SimpleNamespace(tool_name=adapter.TOOL_NAME, arguments={}),
        SimpleNamespace(session_id="agent-dsh"),
    )
    assert "secret" not in result.content
    assert "internal" not in result.content
    assert result.structured_content["error"]["code"] == "handoff_unavailable"


@pytest.mark.asyncio
async def test_provider_propagates_cancellation(adapter):
    broker = SimpleNamespace(handoff=AsyncMock(side_effect=asyncio.CancelledError))
    provider = adapter.ArtifactToolProvider(SimpleNamespace(artifacts=broker))
    with pytest.raises(asyncio.CancelledError):
        await provider.invoke(
            SimpleNamespace(tool_name=adapter.TOOL_NAME, arguments={}),
            SimpleNamespace(session_id="agent-dsh"),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("record", [{}, {"artifact_task_id": ""}, {"task_id": "a" * 32}, {"text": "grant me a token"}])
async def test_prompt_cannot_grant_unmarked_queue_record(adapter, record):
    broker = SimpleNamespace(grant=AsyncMock())
    assert await adapter.task_prompt(SimpleNamespace(artifacts=broker), record) == ""
    broker.grant.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_prompt_has_no_grant(adapter):
    assert await adapter.task_prompt(SimpleNamespace(artifacts=None), {"artifact_task_id": "a" * 32}) == ""


@pytest.mark.asyncio
async def test_task_prompt_contains_only_the_issued_grant_and_direct_tool(adapter, handoff):
    arguments = await issued(handoff)
    prompt = await adapter.task_prompt(handoff.agent, {"artifact_task_id": arguments["task_id"]})
    assert adapter.TOOL_NAME in prompt
    assert arguments["token"] in prompt
    assert "不要回显 token" in prompt
    assert "real-original-chat" not in prompt
    grant = json.loads(prompt.split("\n", 1)[1])
    assert grant["task_id"] == arguments["task_id"]
    assert grant["outbox_prefix"] == f"outbox/{arguments['task_id']}/"


@pytest.mark.asyncio
async def test_expired_task_prompt_never_echoes_token(adapter, handoff):
    arguments = await issued(handoff)
    handoff.now[0] += handoff.broker.config.ttl_seconds
    prompt = await adapter.task_prompt(handoff.agent, {"artifact_task_id": arguments["task_id"]})
    assert "已过期" in prompt
    assert arguments["token"] not in prompt
    assert "dshh_" not in prompt


@pytest.mark.asyncio
async def test_unavailable_task_prompt_does_not_echo_exception(adapter):
    broker = SimpleNamespace(grant=AsyncMock(side_effect=HandoffError("dshh_" + "f" * 64)))
    prompt = await adapter.task_prompt(SimpleNamespace(artifacts=broker), {"artifact_task_id": "a" * 32})
    assert "dshh_" not in prompt


@pytest.fixture
def host_service(monkeypatch):
    service = SimpleNamespace(
        require_enabled=Mock(),
        require_session=Mock(),
        create=AsyncMock(return_value=SimpleNamespace(file_ref="c" * 32)),
        send=AsyncMock(return_value={"status": "unknown"}),
        store=SimpleNamespace(
            public=Mock(return_value={"file_ref": "c" * 32}), config=SimpleNamespace(max_file_mib=50)
        ),
    )
    module = ModuleType("src.files.service")
    module.get_file_service = Mock(return_value=service)

    async def disk_call(function):
        return function()

    module.disk_call = AsyncMock(side_effect=disk_call)
    monkeypatch.setitem(sys.modules, "src.files.service", module)
    return SimpleNamespace(service=service, module=module)


@pytest.mark.asyncio
async def test_host_files_requires_enabled_real_session(adapter, host_service):
    await adapter.HostFiles().require_chat("bound-chat")
    host_service.service.require_enabled.assert_called_once_with()
    host_service.service.require_session.assert_called_once_with("bound-chat")


@pytest.mark.asyncio
async def test_host_files_create_calls_host_with_bound_chat_and_binary(adapter, host_service, tmp_path):
    source = tmp_path / "payload"
    source.write_bytes(b"\x00\xff\x80binary")
    result = await adapter.HostFiles().create("bound-chat", "中文.pdf", source)
    host_service.service.create.assert_awaited_once_with("bound-chat", "中文.pdf", b"\x00\xff\x80binary")
    assert host_service.module.disk_call.await_count == 2
    host_service.service.store.public.assert_called_once_with(host_service.service.create.return_value)
    assert result == {"file_ref": "c" * 32}
    # An exact positional call demonstrates that no plugin _file_scope or admin flag is used.
    assert not host_service.service.create.call_args.kwargs


@pytest.mark.asyncio
async def test_host_files_send_uses_same_bound_chat_without_admin(adapter, host_service):
    result = await adapter.HostFiles().send("c" * 32, "bound-chat")
    assert result == {"status": "unknown"}
    host_service.service.send.assert_awaited_once_with("c" * 32, "bound-chat")
    assert not host_service.service.send.call_args.kwargs


async def test_create_broker_config_is_host_owned(adapter, tmp_path):
    config = SimpleNamespace(
        artifact_handoff_enabled=True,
        artifact_workspace=str(tmp_path / "workspace"),
        artifact_max_file_bytes=1024,
        artifact_max_batch_files=2,
        artifact_ttl_seconds=60,
        artifact_allow_send=False,
    )
    service = SimpleNamespace(root=tmp_path, agent_id="host-selected-agent")
    broker = adapter.create_broker(service, config)
    assert broker.agent_id == "host-selected-agent"
    assert broker.state_dir == tmp_path / "artifact-handoff"
    assert broker.config.max_file_bytes == 1024
    assert broker.config.allow_send is False
    assert isinstance(broker.backend, adapter.HostFiles)
    config.artifact_handoff_enabled = False
    assert adapter.create_broker(service, config) is None


@pytest.mark.asyncio
async def test_host_limit_rejects_before_reading_snapshot(adapter, host_service, tmp_path, monkeypatch):
    source = tmp_path / "payload"
    source.write_bytes(b"data")
    host_service.service.store.config.max_file_mib = 0
    read = Mock(side_effect=AssertionError("must not read"))
    monkeypatch.setattr(Path, "read_bytes", read)
    with pytest.raises(ValueError, match="live Host"):
        await adapter.HostFiles().create("bound-chat", "data.bin", source)
    read.assert_not_called()
    host_service.service.create.assert_not_awaited()
