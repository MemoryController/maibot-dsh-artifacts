"""Isolated integration with unmodified, pinned official Host file components.

This does NOT boot MaiBot, its tool dispatcher/Runner, ACP, Docker or QQ. A test
harness issues/revokes real FileService tickets and bridges the real SDK to the
real capability handler and localhost HTTP routes. Chat discovery, logging and
QQ transport are doubles. SQLite/file registration/HTTP bytes are real. The
image consumer is a representative test consumer, not an installed image plugin.
No historical Host integration/patch is imported or applied.

The media checks additionally compile unchanged method ASTs from the pinned
ComponentQueryService/MaisakaReasoningEngine sources. They use the real Host
ToolContentItem, ImageComponent, ToolResultMediaMessage, context projection and
image resolver. This source-method harness is not the complete Host dispatcher.

Run explicitly; these checks are separate from the mock-based default suite:
MAIBOT_SOURCE_PATH=... DSH_ARTIFACTS_SOURCE=... python -m pytest -q host_checks/stock_files.py
"""

from __future__ import annotations

import ast
import asyncio
import base64
import binascii
import contextvars
import hashlib
import importlib
import importlib.metadata
import importlib.util
import inspect
import io
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from html import escape
from importlib.metadata import version
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast

import pytest
from maibot_sdk import PluginContext
from maibot_sdk.context import PluginPaths
from PIL import Image

from host_checks.media_methods import source_class

HOST_COMMIT = "2a9af62cec37035d29237acbe2cf3adceb700b27"
DSH_COMMIT = "f008cdc039122d360dee67b86e1e2004083ba019"
PLUGIN_ID = "github.MemoryController.maibot-dsh-artifacts"


def checked_source(variable: str, expected: str, paths: tuple[str, ...]) -> Path:
    value = os.environ.get(variable)
    assert value, f"{variable} must identify the separately checked-out pinned source"
    source = Path(value).resolve(strict=True)
    actual = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    assert actual == expected, f"Unexpected {variable} commit"
    subprocess.run(["git", "diff", "--exit-code", expected, "--", *paths], cwd=source, check=True, capture_output=True)
    return source


@pytest.fixture(scope="module")
def official_modules():
    assert sys.version_info >= (3, 12), "The unmodified Host component check requires Python 3.12+"
    assert version("maibot-plugin-sdk") == "2.19.0"
    assert int(subprocess.check_output(["node", "-p", "process.versions.node.split('.')[0]"], text=True)) >= 24
    host = checked_source("MAIBOT_SOURCE_PATH", HOST_COMMIT, ("src",))
    dsh = checked_source("DSH_ARTIFACTS_SOURCE", DSH_COMMIT, ("src", "dist", "package.json"))
    package = json.loads((dsh / "package.json").read_text())
    assert package["version"] == "0.2.1" and package["engines"]["dsh"] == "0.2.2-alpha.2"
    assert importlib.metadata.version("maibot-plugin-sdk") == "2.19.0"
    assert int(subprocess.check_output(["node", "-p", "process.versions.node.split('.')[0]"], text=True)) >= 24
    saved = {key: value for key, value in sys.modules.items() if key == "src" or key.startswith("src.")}
    for key in saved:
        del sys.modules[key]
    bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True  # Never create files in the official checkout.
    with pytest.MonkeyPatch.context() as patch:
        patch.syspath_prepend(str(host))
        # Logger bootstrapping normally loads the full bot config. These doubles
        # keep this integration limited to files; all file modules stay intact.
        logger = ModuleType("src.common.logger")
        logger.get_logger = logging.getLogger
        patch.setitem(sys.modules, logger.__name__, logger)
        config = ModuleType("src.config.config")
        config.global_config = SimpleNamespace(capabilities=SimpleNamespace(files=True))
        patch.setitem(sys.modules, config.__name__, config)
        modules = SimpleNamespace(dsh=dsh, source=host)
        for name in ("config", "models", "store", "service", "capability", "http", "provider"):
            setattr(modules, name, importlib.import_module("src.files." + name))
        modules.tooling = importlib.import_module("src.core.tooling")
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location(
            "stock_host_check_plugin", root / "plugin.py", submodule_search_locations=[str(root)]
        )
        modules.plugin = importlib.util.module_from_spec(spec)
        patch.setitem(sys.modules, spec.name, modules.plugin)
        spec.loader.exec_module(modules.plugin)
        yield modules
    sys.dont_write_bytecode = bytecode
    for key in list(sys.modules):
        if key == "src" or key.startswith("src."):
            del sys.modules[key]
    sys.modules.update(saved)


@pytest.fixture
def connected(official_modules, tmp_path, monkeypatch, request):
    import uvicorn
    from fastapi import FastAPI, Response
    from sqlmodel import SQLModel, create_engine

    host = official_modules
    engine = create_engine("sqlite:///" + str(tmp_path / "host-files.sqlite3"))
    request.addfinalizer(engine.dispose)
    SQLModel.metadata.create_all(engine)
    store = host.store.FileStore(tmp_path / "host-blobs", engine, host.config.FilesConfig())
    service = host.service.FileService(store, enabled_provider=lambda: True)
    chats = {name: SimpleNamespace(session_id=name) for name in ("original-chat", "another-chat")}
    chat_module = ModuleType("src.chat.message_receive.chat_manager")
    chat_module.chat_manager = SimpleNamespace(get_session_by_session_id=chats.get)
    monkeypatch.setitem(sys.modules, chat_module.__name__, chat_module)
    for module in (host.service, host.capability, host.http, host.provider):
        monkeypatch.setattr(module, "get_file_service", lambda: service)

    deliveries = []
    transport = ModuleType("src.services.send_service")

    async def send(kind, asset, session_id, **kwargs):
        deliveries.append((kind, asset["file_ref"], session_id))
        return None  # Official service converts this QQ transport double to unknown.

    transport.custom_to_stream_with_message = send
    monkeypatch.setitem(sys.modules, transport.__name__, transport)
    http = SimpleNamespace(lose_next_upload=False, uploads=0)
    app = FastAPI()

    @app.middleware("http")
    async def observe_and_lose_receipt(request, call_next):
        response = await call_next(request)
        if request.method == "PUT":
            http.uploads += 1
            if http.lose_next_upload and response.status_code == 200:
                http.lose_next_upload = False
                return Response(status_code=502)  # Registration happened; receipt was lost.
        return response

    app.include_router(host.http.router)
    listener = socket.socket()
    request.addfinalizer(listener.close)
    listener.bind(("127.0.0.1", 0))
    service.default_base_url = f"http://127.0.0.1:{listener.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False, lifespan="off"))
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()

    def stop_server():
        server.should_exit = True
        thread.join(timeout=5)
        assert not thread.is_alive(), "Component-test HTTP thread failed to stop"

    request.addfinalizer(stop_server)
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started, "Local component-test HTTP server did not start"
    # The test endpoint is loopback only; keep inherited proxies for all other hosts.
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    scope = contextvars.ContextVar("component_test_scope", default="")
    calls = []

    async def rpc(method, plugin_id, payload, **kwargs):
        assert method == "cap.call" and plugin_id == PLUGIN_ID
        capability = payload["capability"]
        assert capability in {"files.list", "files.upload", "files.download", "files.send"}
        args = {**payload["args"], "file_scope": scope.get()}
        calls.append(capability)
        return await host.capability.file_capability(PLUGIN_ID, capability, args)

    @contextmanager
    def invocation(session_id="original-chat"):
        # Test harness boundary, not the real component_query/Runner dispatcher.
        service.require_session(session_id)
        ticket = service.ticket("plugin", lifetime=3600, plugin_id=PLUGIN_ID, session_id=session_id)
        current = scope.set(ticket)
        try:
            yield ticket
        finally:
            scope.reset(current)
            service.revoke(ticket)

    shared, runtime, data = (tmp_path / name for name in ("shared", "runtime", "data"))
    (shared / "outbox").mkdir(parents=True)
    runtime.mkdir(mode=0o700)
    ctx = PluginContext(PLUGIN_ID, rpc, PluginPaths(data_dir=data, runtime_dir=runtime))

    def new_plugin():
        plugin = host.plugin.create_plugin()
        plugin._set_context(ctx)
        plugin.set_plugin_config({"plugin": {"config_version": "1.0.0"}, "workspace": {"root": str(shared)}})
        return plugin

    result = SimpleNamespace(
        host=host, store=store, service=service, ctx=ctx, invocation=invocation, plugin=new_plugin(),
        new_plugin=new_plugin, shared=shared, runtime=runtime, data=data, calls=calls,
        http=http, deliveries=deliveries, tmp=tmp_path,
    )
    yield result


NODE_PRODUCER = """
import { ArtifactService } from './dist/core.js';
const [root, state, mode] = process.argv.slice(1);
const service = new ArtifactService({ workspaceRoot: root, stateDir: state, confirmationMode: mode });
const prepared = await service.prepare(['outbox/中文 图片.png', 'outbox/二进制.bin'], 'isolated-host-test');
console.log(JSON.stringify(await service.publish(prepared.manifest.task_id, 'isolated-host-test')));
"""


def publish(s, mode="request"):
    image = io.BytesIO()
    Image.new("RGB", (4, 3), (20, 80, 160)).save(image, format="PNG")
    (s.shared / "outbox/中文 图片.png").write_bytes(image.getvalue())
    (s.shared / "outbox/二进制.bin").write_bytes(bytes(range(256)))
    result = json.loads(subprocess.check_output(
        ["node", "--input-type=module", "-e", NODE_PRODUCER, str(s.shared), str(s.tmp / "dsh-state"), mode],
        cwd=s.host.dsh, text=True, timeout=30,
    ))
    assert result["result"] == "submitted" and result["maibot_registration"] == "unconfirmed"
    return result


async def execute(s, submitted, **kwargs):
    confirmation = submitted["confirmation"]
    tool = getattr(s.plugin, confirmation["tool"])
    return await tool(**confirmation["arguments"], **kwargs)


async def listing(s, session_id="original-chat"):
    result = await s.host.provider.FileToolProvider().invoke(
        s.host.tooling.ToolInvocation("files_list"),
        s.host.tooling.ToolExecutionContext(session_id=session_id),
    )
    assert result.success, result.error_message
    return result.structured_content


@pytest.mark.parametrize("mode", ["manifest", "request"])
async def test_actual_dsh_sdk_http_registration_and_builtin_files_list(connected, mode):
    s = connected
    submitted = publish(s, mode)
    assert not s.store.list("original-chat")  # Local DSH publication is not an import.
    with s.invocation() as ticket:
        result = await execute(s, submitted, session_id="forged-chat", chat_id="another-chat")
        assert result["registered"] == "confirmed" and result["delivery"] == "not_requested"
        refs = {item["file"]["file_ref"] for item in result["items"]}
        assert refs == {item["file_ref"] for item in await s.ctx.files.list()}
    assert refs == {item["file_ref"] for item in await listing(s)}
    assert len(refs) == 2 and s.http.uploads == 2 and not s.deliveries
    assert {item["name"] for item in s.store.list("original-chat")} == {"中文 图片.png", "二进制.bin"}
    for item in result["items"]:
        assert s.store.read_bytes(item["file"]["file_ref"], "original-chat") == (s.shared / item["path"]).read_bytes()
    with pytest.raises(PermissionError):
        s.service.claims(ticket, "plugin")  # Ticket revoked on test invocation return.


async def test_downloaded_registered_image_can_feed_a_representative_consumer(connected):
    s = connected
    submitted = publish(s)
    with s.invocation():
        result = await execute(s, submitted)
    ref = result["items"][0]["file"]["file_ref"]
    destination = s.tmp / "downstream.png"
    with s.invocation():  # A new lawful invocation, never a saved scope.
        await s.ctx.files.download(ref, destination)
    with Image.open(destination) as image:
        assert image.format == "PNG" and image.size == (4, 3)
        transformed = image.resize((2, 2)).convert("L")
        transformed.save(s.tmp / "processed.png")
    assert (s.tmp / "processed.png").is_file()
    # Host v1.3.18 does not expose a built-in file_ref->image tool; this tests the
    # supported SDK download contract an actual file-aware image plugin can use.


async def test_replay_restart_and_other_chat_cannot_access_registered_references(connected):
    s = connected
    submitted = publish(s)
    with s.invocation():
        result = await execute(s, submitted)
    refs = {item["file"]["file_ref"] for item in result["items"]}
    s.plugin = s.new_plugin()  # Same private ledger, fresh plugin instance.
    with s.invocation():
        assert (await execute(s, submitted))["cached"]
    assert s.http.uploads == 2 and await listing(s, "another-chat") == []
    with s.invocation("another-chat"):
        rejected = await execute(s, submitted, session_id="original-chat")
        status = await s.plugin.dsh_handoff_status(submitted["manifest"]["task_id"])
        assert rejected["registered"] == status["registered"] == "unconfirmed"
        assert not any(ref in repr((rejected, status)) for ref in refs)
        for ref in refs:
            with pytest.raises(PermissionError):
                await s.ctx.files.download(ref, s.tmp / (ref + ".foreign"))
    assert s.http.uploads == 2 and not s.deliveries


async def test_no_scope_expired_scope_and_foreign_plugin_scope_rejected(connected):
    s = connected
    submitted = publish(s)
    assert (await execute(s, submitted))["registered"] == "unconfirmed"
    for claims in (
        {"lifetime": -1, "plugin_id": PLUGIN_ID, "session_id": "original-chat"},
        {"lifetime": 60, "plugin_id": "another-plugin", "session_id": "original-chat"},
    ):
        ticket = s.service.ticket("plugin", **claims)
        try:
            with pytest.raises(PermissionError):
                await s.host.capability.file_capability(PLUGIN_ID, "files.list", {"file_scope": ticket})
        finally:
            s.service.revoke(ticket)
    assert not s.store.list("original-chat") and s.http.uploads == 0
    with pytest.raises(PermissionError):
        await s.ctx.files.list()


async def test_lost_real_http_upload_receipt_does_not_trigger_reupload(connected):
    s = connected
    submitted = publish(s)
    s.http.lose_next_upload = True
    with s.invocation():
        result = await execute(s, submitted)
    assert result["registered"] == "partial" and result["items"][0]["status"] == "unknown"
    assert len(s.store.list("original-chat")) == 2  # First HTTP upload completed despite the lost response.
    s.plugin = s.new_plugin()
    with s.invocation():
        repeated = await execute(s, submitted)
        await s.plugin.dsh_handoff_status(submitted["manifest"]["task_id"])
    assert repeated["registered"] == "partial" and s.http.uploads == 2 and not s.deliveries


async def test_cancelled_invocation_after_real_registration_never_blindly_retries(connected, monkeypatch):
    s = connected
    submitted = publish(s)
    actual_upload = s.ctx.files.upload
    registered = asyncio.Event()
    hold = asyncio.Event()

    async def interrupted_upload(*args, **kwargs):
        await actual_upload(*args, **kwargs)
        registered.set()
        await hold.wait()  # Simulate loss of process execution before receipt persistence.

    monkeypatch.setattr(s.ctx.files, "upload", interrupted_upload)
    with s.invocation():
        task = asyncio.create_task(execute(s, submitted))
        await asyncio.wait_for(registered.wait(), 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    monkeypatch.setattr(s.ctx.files, "upload", actual_upload)
    assert len(s.store.list("original-chat")) == 1
    s.plugin = s.new_plugin()
    with s.invocation():
        assert (await execute(s, submitted))["registered"] == "unconfirmed"
    assert s.http.uploads == 1 and not s.deliveries
    assert not list(s.runtime.iterdir())


async def test_real_file_service_unknown_send_not_repeated(connected):
    s = connected
    submitted = publish(s, "manifest")
    manifest = submitted["manifest"]
    with s.invocation():
        result = await s.plugin.dsh_import_handoff(manifest, send=True)
    assert result["registered"] == "confirmed" and result["delivery"] == "unknown"
    assert len(s.deliveries) == 2
    with s.invocation():
        await s.plugin.dsh_import_handoff(manifest, send=True)
    assert len(s.deliveries) == 2 and s.http.uploads == 2
    assert all(session_id == "original-chat" for _, _, session_id in s.deliveries)


@pytest.fixture(scope="module")
def official_media(official_modules):
    """Real media data types/resolver, plus exact isolated source methods.

    Only the unused SessionMessage class is doubled to avoid importing the
    full message processing/database boot graph. ToolResultMediaMessage and
    every exercised media conversion/resolution implementation are official.
    """
    host = official_modules
    with pytest.MonkeyPatch.context() as patch:
        session_module = ModuleType("src.chat.message_receive.message")
        session_module.SessionMessage = type("UnusedSessionMessageDouble", (), {})
        patch.setitem(sys.modules, session_module.__name__, session_module)
        components = importlib.import_module("src.common.data_models.message_component_data_model")
        messages = importlib.import_module("src.maisaka.context.messages")
        items = importlib.import_module("src.llm_models.payload_content.context_item")
        options = importlib.import_module("src.llm_models.payload_content.tool_option")
        attachment = importlib.import_module("src.maisaka.context.image_attachment")
        codec = importlib.import_module("src.plugin_runtime.protocol.codec")
        envelope = importlib.import_module("src.plugin_runtime.protocol.envelope")
        transport = importlib.import_module("src.plugin_runtime.transport.base")
        spec = importlib.util.spec_from_file_location(
            "stock_host_media_core", host.source / "src/plugin_runtime/capabilities/core.py"
        )
        core = importlib.util.module_from_spec(spec)
        patch.setitem(sys.modules, spec.name, core)
        spec.loader.exec_module(core)
        converter = source_class(
            host.source / "src/plugin_runtime/component_query.py",
            "ComponentQueryService",
            ("_parse_tool_invoke_result", "_parse_tool_content_items"),
            {"Any": Any, "cast": cast, **vars(host.tooling)},
        )
        engine_source = host.source / "src/maisaka/reasoning_engine.py"
        engine_tree = ast.parse(engine_source.read_text())
        media_types = next(
            node.value for node in engine_tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "TOOL_RESULT_MEDIA_TYPES" for target in node.targets)
        )
        engine = source_class(
            engine_source,
            "MaisakaReasoningEngine",
            (
                "__init__", "_iter_tool_result_media_items", "_build_tool_result_media_index",
                "_get_tool_result_media_metadata_value", "_build_xml_attrs", "_build_tool_result_media_xml_attrs",
                "_describe_tool_result_media_item", "_build_tool_result_history_content",
                "_decode_tool_result_base64_data",
                "_build_tool_result_media_message_sequence", "_build_tool_result_media_visible_text",
                "_build_tool_result_media_messages",
            ),
            {
                "Any": Any, "datetime": datetime, "escape": escape, "b64decode": base64.b64decode,
                "BinasciiError": binascii.Error, "TOOL_RESULT_MEDIA_TYPES": ast.literal_eval(media_types),
                "ToolResultMediaMessage": messages.ToolResultMediaMessage,
                "MessageSequence": components.MessageSequence, "TextComponent": components.TextComponent,
                "ImageComponent": components.ImageComponent,
            },
        )
        assert tuple(inspect.signature(converter._parse_tool_invoke_result).parameters) == ("entry", "result")
        assert tuple(inspect.signature(converter._parse_tool_content_items).parameters) == ("raw_items",)
        assert tuple(inspect.signature(engine._build_tool_result_media_messages).parameters) == (
            "self", "tool_call", "result",
        )
        assert tuple(inspect.signature(attachment.resolve_context_image).parameters) == (
            "runtime", "source_id", "image_index",
        )
        yield SimpleNamespace(
            components=components, messages=messages, items=items, options=options,
            attachment=attachment, core=core, converter=converter, engine=engine,
            codec=codec, envelope=envelope, max_frame_size=transport.MAX_FRAME_SIZE,
        )


def convert_open_result(media, opened, call_id="actual-host-call-123"):
    """Execute the actual pinned parsing/projection methods, no index guess."""
    envelope = media.envelope.Envelope(
        request_id=1, message_type=media.envelope.MessageType.RESPONSE,
        method="plugin.invoke_tool", plugin_id=PLUGIN_ID,
        payload=media.envelope.InvokeResultPayload(success=True, result=opened).model_dump(),
    )
    codec = media.codec.MsgPackCodec()
    wire = codec.encode_envelope(envelope)
    assert len(wire) <= media.max_frame_size
    decoded = codec.decode_envelope(wire)
    entry = SimpleNamespace(name="dsh_open_image", plugin_id=PLUGIN_ID)
    result = media.converter._parse_tool_invoke_result(entry, decoded.payload["result"])
    runtime = SimpleNamespace(session_id="original-chat", _chat_history=[])
    engine = media.engine(runtime)
    call = media.options.ToolCall(call_id=call_id, func_name="dsh_open_image", args={})
    messages = engine._build_tool_result_media_messages(call, result)
    runtime._chat_history.extend(messages)  # Harness inserts exactly the official generated messages.
    return SimpleNamespace(result=result, runtime=runtime, engine=engine, call=call, messages=messages)


@pytest.mark.parametrize("mode", ["manifest", "request"])
async def test_open_image_real_download_and_official_tool_media_projection(connected, official_media, mode):
    s, media = connected, official_media
    submitted = publish(s, mode)
    with s.invocation():
        imported = await execute(s, submitted)
    file_ref = imported["items"][0]["file"]["file_ref"]
    assert s.store.get(file_ref, "original-chat").mime_type == "image/png"
    with s.invocation():
        opened = await s.plugin.dsh_open_image(file_ref)
    assert opened["success"] and s.http.uploads == 2 and not s.deliveries
    assert not list(s.runtime.iterdir())
    raw = (s.shared / "outbox/中文 图片.png").read_bytes()
    expected_base64 = base64.b64encode(raw).decode("ascii")
    converted = convert_open_result(media, opened)
    assert converted.result.success and len(converted.result.content_items) == 1
    item = converted.result.content_items[0]
    assert isinstance(item, s.host.tooling.ToolContentItem)
    assert item.content_type == "image" and item.mime_type == "image/png" and item.data == expected_base64
    assert expected_base64 not in converted.result.get_history_content()
    history = converted.engine._build_tool_result_history_content(converted.call, converted.result)
    assert 'msg_id="tool_result:actual-host-call-123:1"' in history and expected_base64 not in history
    assert len(converted.messages) == 1
    message = converted.messages[0]
    assert isinstance(message, media.messages.ToolResultMediaMessage)
    assert message.source_kind == "tool_result_media" and message.session_id == "original-chat"
    assert not isinstance(message, media.messages.SessionBackedMessage) and not message.count_in_context
    assert not hasattr(message, "user_info") and not hasattr(message, "original_message")
    assert message.message_id == "tool_result:actual-host-call-123:1"
    images = [component for component in message.raw_message.components
              if isinstance(component, media.components.ImageComponent)]
    assert len(images) == 1 and images[0].binary_data == raw
    assert images[0].binary_hash == hashlib.sha256(raw).hexdigest()
    projected = message.to_context_item(enable_visual_message=True)
    image_parts = [part for part in projected.parts if isinstance(part, media.items.ContextImagePart)]
    assert len(image_parts) == 1 and image_parts[0].image_base64 == expected_base64
    assert not [part for part in message.to_context_item(enable_visual_message=False).parts
                if isinstance(part, media.items.ContextImagePart)]


async def test_legacy_octet_stream_image_with_binary_filename_opens_without_reupload(connected, official_media):
    s, media = connected, official_media
    publish(s)  # Create the valid PNG bytes; do not import the submitted task.
    path = s.shared / "outbox/中文 图片.png"
    with s.invocation():
        asset = await s.ctx.files.upload(path, name="历史附件.bin", mime_type="application/octet-stream")
    assert s.store.get(asset["file_ref"], "original-chat").mime_type == "application/octet-stream"
    with s.invocation():
        opened = await s.plugin.dsh_open_image(asset["file_ref"])
    converted = convert_open_result(media, opened)
    assert converted.result.success and converted.result.content_items[0].mime_type == "image/png"
    image = await media.attachment.resolve_context_image(converted.runtime, converted.messages[0].message_id)
    assert image.binary_data == path.read_bytes()
    assert len(await listing(s)) == 1 and s.http.uploads == 1 and not s.deliveries


async def test_official_sdk_context_image_resolver_accepts_generated_media_index(
    connected, official_media, monkeypatch,
):
    s, media = connected, official_media
    submitted = publish(s)
    with s.invocation():
        imported = await execute(s, submitted)
        opened = await s.plugin.dsh_open_image(imported["items"][0]["file"]["file_ref"])
    converted = convert_open_result(media, opened, call_id="real-call-456")
    manager_module = ModuleType("src.chat.heart_flow.heartflow_manager")
    manager_module.heartflow_manager = SimpleNamespace(heartflow_chat_list={
        "original-chat": converted.runtime,
        "another-chat": SimpleNamespace(session_id="another-chat", _chat_history=[]),
    })
    monkeypatch.setitem(sys.modules, manager_module.__name__, manager_module)
    seen = []

    async def resolve_rpc(method, plugin_id, payload, **kwargs):
        assert method == "cap.call" and plugin_id == "test-image-consumer"
        assert payload["capability"] == "maisaka.context.resolve_image"
        seen.append(payload["args"])
        return await media.core.RuntimeCoreCapabilityMixin()._cap_maisaka_context_resolve_image(
            plugin_id, payload["capability"], payload["args"],
        )

    consumer = PluginContext("test-image-consumer", resolve_rpc)
    resolved = await consumer.maisaka.context.resolve_image("original-chat", converted.messages[0].message_id)
    assert resolved["success"]
    image = resolved["image"]
    raw = (s.shared / "outbox/中文 图片.png").read_bytes()
    assert base64.b64decode(image["binary_data_base64"], validate=True) == raw
    assert image["binary_hash"] == hashlib.sha256(raw).hexdigest()
    # This official capability selects an already-existing runtime by stream_id;
    # it is not a file-scope authorization API. The bridge does not request it.
    wrong_history = await consumer.maisaka.context.resolve_image("another-chat", converted.messages[0].message_id)
    missing = await consumer.maisaka.context.resolve_image("original-chat", "tool_result:missing:1")
    wrong_index = await consumer.maisaka.context.resolve_image(
        "original-chat", converted.messages[0].message_id, index=1,
    )
    assert not wrong_history["success"] and not missing["success"] and not wrong_index["success"]
    assert not [result for result in (wrong_history, missing, wrong_index) if "image" in result]
    assert len(seen) == 4 and not s.deliveries and s.http.uploads == 2


async def test_open_image_other_chat_and_outside_scope_produce_no_media(connected, official_media):
    s, media = connected, official_media
    submitted = publish(s)
    with s.invocation():
        imported = await execute(s, submitted)
    file_ref = imported["items"][0]["file"]["file_ref"]
    with s.invocation("another-chat"):
        forbidden = await s.plugin.dsh_open_image(file_ref, session_id="original-chat", chat_id="original-chat")
    unscoped = await s.plugin.dsh_open_image(file_ref)
    for result in (forbidden, unscoped):
        assert not result["success"] and not result.get("content_items")
        converted = convert_open_result(media, result)
        assert not converted.result.success and not converted.messages
    assert s.http.uploads == 2 and not s.deliveries and not list(s.runtime.iterdir())
