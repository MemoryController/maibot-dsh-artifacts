"""Negative interoperability checks against actual drawing-plugin v2.1.3.

Run explicitly with DRAW_PIC_SOURCE pointing to the pinned clean checkout.
Only imports its schema and source-image lookup helpers. Message capabilities
are doubles; no drawing provider, credentials, task creation or send is used.
These checks prove a limitation, not successful edit_image integration.
"""

from __future__ import annotations

import base64
import importlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

EXPECTED_COMMIT = "56284ac69bed57829ea67ab68ce31309d10b9551"
MEDIA_INDEX = "tool_result:actual-host-call:1"


def _git(source: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(source), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture(scope="module")
def peer():
    source_setting = os.environ.get("DRAW_PIC_SOURCE")
    if not source_setting:
        pytest.fail("Set DRAW_PIC_SOURCE to the clean pinned v2.1.3 test checkout")
    source = Path(source_setting).resolve(strict=True)
    assert _git(source, "rev-parse", "HEAD") == EXPECTED_COMMIT
    assert not _git(source, "status", "--porcelain", "--", "src", "_manifest.json")
    manifest = json.loads((source / "_manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == "2.1.3"
    assert manifest["sdk"] == {"min_version": "2.12.0", "max_version": "2.17.0"}
    original_path = list(sys.path)
    original_bytecode = sys.dont_write_bytecode
    sys.path.insert(0, str(source / "src"))
    sys.dont_write_bytecode = True
    try:
        schemas = importlib.import_module("maimai_drawpic.interfaces.tools.schemas")
        images = importlib.import_module("maimai_drawpic.interfaces.chat.message_images")
        for module in (schemas, images):
            assert Path(module.__file__).resolve().is_relative_to(source / "src")
        yield SimpleNamespace(source=source, manifest=manifest, schemas=schemas, images=images)
        assert not _git(source, "status", "--porcelain", "--", "src", "_manifest.json")
    finally:
        sys.path[:] = original_path
        sys.dont_write_bytecode = original_bytecode


def test_actual_edit_image_schema_does_not_accept_a_media_reference(peer):
    schema = peer.schemas.EDIT_IMAGE_TOOL_PARAMETERS_SCHEMA
    assert schema["required"] == ["prompt"]
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {
        "prompt",
        "source_message_id",
        "source_image_base64",
        "model",
        "negative_prompt",
    }
    assert {"media_index", "image_refs", "source_id", "file_ref"}.isdisjoint(schema["properties"])


@pytest.mark.asyncio
async def test_actual_lookup_falls_back_to_another_image_for_a_tool_media_index(peer):
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2), "blue").save(buffer, format="PNG")
    unrelated_bytes = buffer.getvalue()
    encoded = base64.b64encode(unrelated_bytes).decode("ascii")
    calls = []

    async def call_capability(capability, **arguments):
        calls.append((capability, arguments))
        if capability == "message.get_by_id":
            return None
        if capability == "message.get_recent":
            return [{
                "message_id": "unrelated-qq-image",
                "raw_message": [{"type": "image", "binary_data_base64": encoded}],
            }]
        raise AssertionError(f"Unexpected capability: {capability}")

    ctx = SimpleNamespace(call_capability=call_capability)
    peer.images.clear_source_image_cache()
    try:
        found, matched = await peer.images.find_source_image_inputs(
            ctx, "test-current-chat", source_message_id=MEDIA_INDEX,
        )
        assert matched == "unrelated-qq-image"
        assert len(found) == 1
        assert peer.images.decode_image_base64(found[0].base64_data) == unrelated_bytes
        assert calls == [
            ("message.get_by_id", {
                "message_id": MEDIA_INDEX,
                "chat_id": "test-current-chat",
                "include_binary_data": True,
            }),
            ("message.get_recent", {
                "chat_id": "test-current-chat",
                "limit": 8,
                "include_binary_data": True,
            }),
            ("message.get_by_id", {
                "message_id": "unrelated-qq-image",
                "chat_id": "test-current-chat",
                "include_binary_data": True,
            }),
        ]
        assert all(name != "maisaka.context.resolve_image" for name, _ in calls)
    finally:
        peer.images.clear_source_image_cache()
