"""Open an authorized file as real tool image content without uploading it again.

File capability doubles model Host authorization. Pillow decodes genuine image
fixtures; these tests do not claim that a live model visually understood them.
"""

import asyncio
import base64
import hashlib
import io
import json
import struct
import zlib
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from image_safety import MAX_IMAGE_BYTES, MAX_IMAGE_PIXELS

REF = "a" * 32
pytestmark = pytest.mark.asyncio


def image_bytes(kind="PNG", size=(3, 2)):
    image = Image.new("RGB", size, color=(255, 0, 128))
    buffer = io.BytesIO()
    image.save(buffer, format=kind)
    return buffer.getvalue()


@pytest.fixture
def picture(bridge):
    bridge.image_content = image_bytes()
    bridge.image_metadata = {
        "file_ref": REF,
        "name": "private-original-name.png",
        "size": len(bridge.image_content),
        "state": "ready",
        "mime_type": "application/octet-stream",
    }
    bridge.stages = []

    async def listing(offset=0):
        return [dict(bridge.image_metadata)] if offset == 0 else []

    async def download(file_ref, destination):
        assert file_ref == REF
        destination = Path(destination)
        assert destination.parent.stat().st_mode & 0o777 == 0o700
        bridge.stages.append(destination)
        destination.write_bytes(bridge.image_content)
        return {
            **bridge.image_metadata,
            "url": "https://secret.invalid/transfer?token=INTERNAL_SECRET",
            "name": "../../../INTERNAL_SECRET.png",
            "session_id": "INTERNAL_SECRET",
        }

    bridge.files.list = AsyncMock(side_effect=listing)
    bridge.files.download.side_effect = download
    return bridge


def no_mutation_or_residue(picture):
    picture.files.upload.assert_not_awaited()
    picture.files.send.assert_not_awaited()
    assert not list(picture.runtime.iterdir())
    assert all(not path.exists() for path in picture.stages)


def assert_failure(result):
    assert result["success"] is False
    assert result["content_items"] == []
    assert isinstance(result["content"], str) and result["content"]
    assert "error" in result and "code" in result["error"]
    assert "INTERNAL_SECRET" not in repr(result)
    assert "https://" not in repr(result)
    assert "image" not in result


@pytest.mark.parametrize("kind,mime", [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")])
async def test_open_authorized_image_returns_actual_media_item_not_just_metadata(picture, kind, mime):
    picture.image_content = image_bytes(kind)
    picture.image_metadata["size"] = len(picture.image_content)
    result = await picture.plugin.dsh_open_image(REF)
    assert result["success"] is True
    assert REF in result["content"]
    assert result["image"] == {"size": len(picture.image_content), "width": 3, "height": 2, "mime_type": mime}
    assert len(result["content_items"]) == 1
    item = result["content_items"][0]
    assert item["type"] == "image" and item["mime_type"] == mime
    assert base64.b64decode(item["data"], validate=True) == picture.image_content
    assert item["name"] and "private-original-name" not in item["name"]
    assert "INTERNAL_SECRET" not in repr(result)
    picture.files.download.assert_awaited_once()
    no_mutation_or_residue(picture)


@pytest.mark.parametrize(
    "name,declared_mime",
    [
        ("old-file.bin", "application/octet-stream"),
        ("spoofed.jpg", "image/jpeg"),
        ("spoofed.pdf", "application/pdf"),
        ("中文文件.webp", "image/webp"),
    ],
)
async def test_bytes_determine_mime_for_historical_octet_and_spoofed_extension(picture, name, declared_mime):
    picture.image_metadata.update(name=name, mime_type=declared_mime)
    result = await picture.plugin.dsh_open_image(REF)
    assert result["success"] is True
    assert result["image"]["mime_type"] == result["content_items"][0]["mime_type"] == "image/png"
    assert base64.b64decode(result["content_items"][0]["data"]) == picture.image_content
    no_mutation_or_residue(picture)


@pytest.mark.parametrize("bad_content", [b"", b"not an image", b"%PDF-1.7", b"\x89PNG\r\n\x1a\nbroken", b"\xff\xd8bad"])
async def test_corrupt_or_nonimage_has_no_metadata_success_fallback(picture, bad_content):
    picture.image_content = bad_content
    picture.image_metadata.update(name="looks-real.png", mime_type="image/png", size=len(bad_content))
    result = await picture.plugin.dsh_open_image(REF)
    assert_failure(result)
    assert result["error"]["code"] in {"invalid_image", "unsupported_image", "image_unavailable"}
    assert "file_ref" not in result or result["file_ref"] == REF
    no_mutation_or_residue(picture)


async def test_oversized_list_metadata_is_rejected_before_download(picture):
    picture.image_metadata["size"] = MAX_IMAGE_BYTES + 1
    result = await picture.plugin.dsh_open_image(REF)
    assert_failure(result)
    assert result["error"]["code"] == "image_too_large"
    picture.files.download.assert_not_awaited()
    no_mutation_or_residue(picture)


async def test_downloaded_bytes_cannot_bypass_encoded_size_limit(picture):
    picture.image_content = b"\x89PNG\r\n\x1a\n" + b"x" * MAX_IMAGE_BYTES
    # An inaccurate preflight cannot weaken the bounded decoder.
    picture.image_metadata["size"] = 100
    result = await picture.plugin.dsh_open_image(REF)
    assert_failure(result)
    assert result["error"]["code"] in {"image_too_large", "image_unavailable"}
    no_mutation_or_residue(picture)


async def test_dimensions_exceeding_pixel_limit_are_rejected_without_pixel_decode(picture):
    def chunk(kind, content):
        value = kind + content
        return struct.pack("!I", len(content)) + value + struct.pack("!I", zlib.crc32(value))

    width, height = 4001, 4000
    assert width * height > MAX_IMAGE_PIXELS
    picture.image_content = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack("!2I5B", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
        + chunk(b"IEND", b"")
    )
    picture.image_metadata["size"] = len(picture.image_content)
    result = await picture.plugin.dsh_open_image(REF)
    assert_failure(result)
    assert result["error"]["code"] == "image_pixels_exceeded"
    no_mutation_or_residue(picture)


@pytest.mark.parametrize("reference", ["", "../other", "A" * 32, "g" * 32, "a" * 31, None, 7])
async def test_invalid_refs_are_rejected_without_file_capabilities(picture, reference):
    result = await picture.plugin.dsh_open_image(reference)
    assert_failure(result)
    assert result["error"]["code"] == "invalid_file_ref"
    picture.files.list.assert_not_awaited()
    picture.files.download.assert_not_awaited()
    no_mutation_or_residue(picture)


async def test_other_chat_cannot_use_known_reference_or_forged_kwargs(picture):
    picture.files.list.side_effect = None
    picture.files.list.return_value = []
    result = await picture.plugin.dsh_open_image(REF, session_id="original-chat", chat_id="original-chat", admin=True)
    assert_failure(result)
    picture.files.download.assert_not_awaited()
    assert all(not call.kwargs.keys() - {"offset"} for call in picture.files.list.await_args_list)
    no_mutation_or_residue(picture)


@pytest.mark.parametrize("error", [PermissionError("foreign reference"), FileNotFoundError("deleted attachment")])
async def test_host_download_remains_authority_when_preflight_is_stale(picture, error):
    picture.files.download.side_effect = error
    result = await picture.plugin.dsh_open_image(REF)
    assert_failure(result)
    picture.files.download.assert_awaited_once()
    no_mutation_or_residue(picture)


async def test_list_scope_failure_never_attempts_download_or_returns_cached_media(picture):
    picture.files.list.side_effect = PermissionError("INTERNAL_SECRET no real scope")
    result = await picture.plugin.dsh_open_image(REF)
    assert_failure(result)
    picture.files.download.assert_not_awaited()
    no_mutation_or_residue(picture)


@pytest.mark.parametrize(
    "bad",
    [
        {"state": "deleted"},
        {"state": "uploading"},
        {"state": None},
        {"size": None},
        {"size": True},
        {"size": -1},
        {"size": "123"},
    ],
)
async def test_unready_or_unbounded_metadata_fails_closed(picture, bad):
    picture.image_metadata.update(bad)
    result = await picture.plugin.dsh_open_image(REF)
    assert_failure(result)
    picture.files.download.assert_not_awaited()
    no_mutation_or_residue(picture)


async def test_missing_required_size_metadata_has_no_download_metadata_fallback(picture):
    del picture.image_metadata["size"]
    result = await picture.plugin.dsh_open_image(REF)
    assert_failure(result)
    picture.files.download.assert_not_awaited()
    no_mutation_or_residue(picture)


async def test_download_failure_cleans_partial_stage_and_sanitizes_error(picture):
    async def interrupted(file_ref, destination):
        Path(destination).write_bytes(b"partial bytes")
        picture.stages.append(Path(destination))
        raise TimeoutError("https://secret.invalid/INTERNAL_SECRET")

    picture.files.download.side_effect = interrupted
    result = await picture.plugin.dsh_open_image(REF)
    assert_failure(result)
    no_mutation_or_residue(picture)


async def test_cancelled_download_propagates_and_cleans_stage(picture):
    started = asyncio.Event()

    async def paused(file_ref, destination):
        Path(destination).write_bytes(b"partial bytes")
        picture.stages.append(Path(destination))
        started.set()
        await asyncio.Future()

    picture.files.download.side_effect = paused
    task = asyncio.create_task(picture.plugin.dsh_open_image(REF))
    await asyncio.wait_for(started.wait(), 3)
    assert picture.stages[0].exists()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    no_mutation_or_residue(picture)


async def test_model_kwargs_are_not_forwarded_to_download(picture):
    result = await picture.plugin.dsh_open_image(REF, session_id="forged", target="forged", send=True)
    assert result["success"] is True
    args, kwargs = picture.files.download.call_args
    assert args[0] == REF and not kwargs
    no_mutation_or_residue(picture)


async def test_disabled_image_tool_calls_no_capabilities(picture):
    picture.plugin.set_plugin_config({"plugin": {"config_version": "1.0.0", "enabled": False}})
    result = await picture.plugin.dsh_open_image(REF)
    assert_failure(result)
    picture.files.list.assert_not_awaited()
    picture.files.download.assert_not_awaited()
    no_mutation_or_residue(picture)


async def test_preflight_finds_image_on_later_current_scope_page(picture):
    filler = [{"file_ref": f"{index:032x}", "state": "ready", "size": 1} for index in range(50)]
    picture.files.list.side_effect = lambda offset=0: filler if offset == 0 else [dict(picture.image_metadata)]
    result = await picture.plugin.dsh_open_image(REF)
    assert result["success"] is True
    assert [call.kwargs["offset"] for call in picture.files.list.await_args_list] == [0, 50]
    picture.files.download.assert_awaited_once()
    no_mutation_or_residue(picture)


@pytest.mark.parametrize("page", [{"files": []}, None, [None] * 51])
async def test_malformed_preflight_page_cannot_authorize_download(picture, page):
    picture.files.list.side_effect = None
    picture.files.list.return_value = page
    result = await picture.plugin.dsh_open_image(REF)
    assert_failure(result)
    picture.files.download.assert_not_awaited()
    no_mutation_or_residue(picture)


async def test_rpc_serialization_budget_rejects_media_instead_of_returning_metadata(
    picture, plugin_module, monkeypatch
):
    image_module = __import__(plugin_module.__name__ + ".image_open", fromlist=["open_image"])
    monkeypatch.setattr(image_module, "MAX_RPC_RESULT_BYTES", 100)
    result = await picture.plugin.dsh_open_image(REF)
    assert_failure(result)
    assert result["error"]["code"] == "image_rpc_too_large"
    no_mutation_or_residue(picture)


async def test_unsafe_runtime_prevents_download_and_media_creation(picture):
    picture.runtime.chmod(0o770)
    result = await picture.plugin.dsh_open_image(REF)
    assert_failure(result)
    picture.files.download.assert_not_awaited()
    no_mutation_or_residue(picture)


async def test_decoder_cancellation_waits_for_worker_before_removing_private_stage(picture, plugin_module, monkeypatch):
    import threading

    image_module = __import__(plugin_module.__name__ + ".image_open", fromlist=["open_image"])
    real_media_result = image_module._media_result
    started, release = threading.Event(), threading.Event()

    def paused_result(path, file_ref):
        started.set()
        if not release.wait(5):
            raise TimeoutError("Test did not release the decoder")
        assert path.exists(), "A running decoder must retain its private snapshot until it exits"
        return real_media_result(path, file_ref)

    monkeypatch.setattr(image_module, "_media_result", paused_result)
    task = asyncio.create_task(picture.plugin.dsh_open_image(REF))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(started.wait, 3), 4)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert picture.stages[0].exists()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 4)
    finally:
        release.set()
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    no_mutation_or_residue(picture)


@pytest.mark.parametrize(
    "name,content,expected",
    [
        ("picture.bin", "PNG", "image/png"),
        ("picture.png", "JPEG", "image/jpeg"),
        ("picture.jpg", "WEBP", "image/webp"),
        ("picture.png", None, "application/octet-stream"),
    ],
)
async def test_import_registers_detected_mime_without_trusting_filename(bridge, name, content, expected):
    data = image_bytes(content) if content else b"this is not a PNG"
    (bridge.root / "outbox" / name).write_bytes(data)
    result = await bridge.plugin.dsh_import(["outbox/" + name])
    assert result["success"] is True
    bridge.files.upload.assert_awaited_once()
    assert bridge.files.upload.call_args.kwargs == {"name": name, "mime_type": expected}
    assert result["items"][0]["file"]["mime_type"] == expected
    bridge.files.send.assert_not_awaited()
    assert not list(bridge.runtime.iterdir())


@pytest.mark.parametrize("entrypoint", ["handoff", "request"])
async def test_strict_handoff_entrypoints_register_detected_image_mime(bridge, entrypoint):
    content = image_bytes("PNG")
    relative = "outbox/image-without-extension"
    (bridge.root / relative).write_bytes(content)
    bridge.plugin._ctx.paths.data_dir = bridge.runtime.parent / "data"
    bridge.files.list = AsyncMock(return_value=[])
    manifest = {
        "type": "dsh_artifacts",
        "version": 1,
        "task_id": "b" * 32,
        "files": [{"path": relative, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}],
    }
    if entrypoint == "handoff":
        result = await bridge.plugin.dsh_import_handoff(manifest)
    else:
        directory = bridge.root / "outbox/.dsh-handoffs"
        directory.mkdir()
        payload = json.dumps(manifest).encode()
        (directory / f"{manifest['task_id']}.json").write_bytes(payload)
        result = await bridge.plugin.dsh_import_request(manifest["task_id"], hashlib.sha256(payload).hexdigest())
    assert result["success"] and result["registered"] == "confirmed"
    assert result["items"][0]["file"]["mime_type"] == "image/png"
    bridge.files.upload.assert_awaited_once()
    assert bridge.files.upload.call_args.kwargs == {"name": "image-without-extension", "mime_type": "image/png"}
    bridge.files.send.assert_not_awaited()
    assert not list(bridge.runtime.iterdir())


async def test_animated_png_does_not_become_a_static_tool_image(picture):
    first, second = Image.new("RGB", (2, 2), "red"), Image.new("RGB", (2, 2), "blue")
    buffer = io.BytesIO()
    first.save(buffer, format="PNG", save_all=True, append_images=[second], duration=100, loop=0)
    picture.image_content = buffer.getvalue()
    picture.image_metadata["size"] = len(picture.image_content)
    result = await picture.plugin.dsh_open_image(REF)
    assert_failure(result)
    assert result["error"]["code"] == "animated_image"
    no_mutation_or_residue(picture)
