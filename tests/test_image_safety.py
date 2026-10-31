"""Content validation uses real Pillow decoding of private temporary snapshots."""

import hashlib
import io
import struct
import subprocess
import sys
import threading
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from PIL import Image, ImageFile

import image_safety
from image_safety import (
    MAX_IMAGE_BYTES,
    MAX_IMAGE_DIMENSION,
    MAX_IMAGE_PIXELS,
    OCTET_STREAM,
    ImageSafetyError,
    detect_upload_mime,
    validate_image,
)


def encoded(kind, *, size=(11, 7)):
    result = io.BytesIO()
    Image.new("RGB", size, (19, 97, 211)).save(result, format=kind)
    return result.getvalue()


def put(tmp_path, raw, name="private-snapshot"):
    path = tmp_path / name
    path.write_bytes(raw)
    return path


def oversized_png_header(width, height):
    def chunk(kind, value):
        return struct.pack(">I", len(value)) + kind + value + struct.pack(">I", zlib.crc32(kind + value))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00"))
        + chunk(b"IEND", b"")
    )


@pytest.mark.parametrize("kind,mime", [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")])
def test_valid_formats_are_verified_and_original_bytes_are_preserved(tmp_path, kind, mime):
    raw = encoded(kind)
    path = put(tmp_path, raw)
    digest = hashlib.sha256(raw).hexdigest()
    assert validate_image(path) == {"mime_type": mime, "width": 11, "height": 7, "size": len(raw)}
    assert detect_upload_mime(path) == mime
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest


@pytest.mark.parametrize("kind,mime", [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")])
def test_actual_content_wins_over_forged_filename(tmp_path, kind, mime):
    path = put(tmp_path, encoded(kind), "伪装成报告.pdf")
    assert validate_image(path)["mime_type"] == detect_upload_mime(path) == mime


def test_jpeg_with_png_extension_still_gets_jpeg_mime(tmp_path):
    assert detect_upload_mime(put(tmp_path, encoded("JPEG"), "图片.png")) == "image/jpeg"


@pytest.mark.parametrize("options", [{"lossless": True}, {"icc_profile": b"test-profile"}])
def test_lossless_and_extended_webp_headers_are_supported(tmp_path, options):
    buffer = io.BytesIO()
    Image.new("RGB", (11, 7), "red").save(buffer, format="WEBP", **options)
    assert validate_image(put(tmp_path, buffer.getvalue()))["mime_type"] == "image/webp"


@pytest.mark.parametrize("raw", [b"a PDF, not a picture", b"<svg>not raster data</svg>", b"GIF89a", b"%PDF-1.4"])
def test_extension_cannot_turn_arbitrary_file_into_image(tmp_path, raw):
    path = put(tmp_path, raw, "not-really.png")
    with pytest.raises(ImageSafetyError) as error:
        validate_image(path)
    assert error.value.code == "unsupported_image"
    assert detect_upload_mime(path) == OCTET_STREAM


@pytest.mark.parametrize("kind", ["PNG", "JPEG", "WEBP"])
def test_truncated_real_image_is_rejected_even_when_header_is_valid(tmp_path, kind):
    raw = encoded(kind)
    # JPEG verify() alone accepts an absent EOI; full pixel decoding must reject
    # it. PNG/WebP are cut far enough to remove actual encoded image data.
    truncated = raw[:-2] if kind == "JPEG" else raw[: len(raw) // 2]
    path = put(tmp_path, truncated, "picture." + kind.lower())
    with pytest.raises(ImageSafetyError) as error:
        validate_image(path)
    assert error.value.code == "invalid_image"
    assert detect_upload_mime(path) == OCTET_STREAM


def test_png_bad_chunk_crc_is_rejected(tmp_path):
    raw = bytearray(encoded("PNG"))
    raw[raw.index(b"IDAT") + 5] ^= 1
    path = put(tmp_path, bytes(raw))
    with pytest.raises(ImageSafetyError) as error:
        validate_image(path)
    assert error.value.code == "invalid_image"
    assert detect_upload_mime(path) == OCTET_STREAM


@pytest.mark.parametrize("raw", [b"", b"\x89PNG\r\n\x1a\n", b"\xff\xd8", b"RIFF0000WEBP"])
def test_empty_or_signature_only_bytes_do_not_make_an_image(tmp_path, raw):
    with pytest.raises(ImageSafetyError) as error:
        validate_image(put(tmp_path, raw))
    assert error.value.code == "invalid_image"


def test_real_unsupported_gif_is_not_decoded_as_a_supported_format(tmp_path):
    path = put(tmp_path, encoded("GIF"), "gif-with-a-png-name.png")
    with pytest.raises(ImageSafetyError) as error:
        validate_image(path)
    assert error.value.code == "unsupported_image"
    assert detect_upload_mime(path) == OCTET_STREAM


@pytest.mark.parametrize("kind", ["PNG", "WEBP"])
def test_animated_images_are_refused_instead_of_silently_picking_a_frame(tmp_path, kind):
    first = Image.new("RGB", (4, 3), "red")
    second = Image.new("RGB", (4, 3), "blue")
    raw = io.BytesIO()
    first.save(raw, format=kind, save_all=True, append_images=[second], duration=100, loop=0)
    path = put(tmp_path, raw.getvalue())
    with pytest.raises(ImageSafetyError) as error:
        validate_image(path)
    assert error.value.code == "animated_image"
    assert detect_upload_mime(path) == OCTET_STREAM


def test_encoded_byte_limit_is_exact_and_can_be_lowered(tmp_path):
    raw = encoded("PNG")
    path = put(tmp_path, raw)
    assert validate_image(path, max_bytes=len(raw))["size"] == len(raw)
    with pytest.raises(ImageSafetyError) as error:
        validate_image(path, max_bytes=len(raw) - 1)
    assert error.value.code == "image_too_large"


def test_absolute_byte_ceiling_does_not_read_a_large_attachment_into_memory(tmp_path):
    path = put(tmp_path, b"\x89PNG\r\n\x1a\n")
    with path.open("r+b") as stream:
        stream.truncate(MAX_IMAGE_BYTES + 1)
    with pytest.raises(ImageSafetyError) as error:
        validate_image(path)
    assert error.value.code == "image_too_large"
    assert detect_upload_mime(path) == OCTET_STREAM


def test_pixel_limit_can_be_lowered(tmp_path):
    path = put(tmp_path, encoded("PNG", size=(11, 7)))
    assert validate_image(path, max_pixels=77)["width"] == 11
    with pytest.raises(ImageSafetyError) as error:
        validate_image(path, max_pixels=76)
    assert error.value.code == "image_pixels_exceeded"


@pytest.mark.parametrize("size", [(5000, 4000), (MAX_IMAGE_DIMENSION + 1, 1), (1, MAX_IMAGE_DIMENSION + 1)])
def test_header_limits_are_checked_before_any_pixel_decode(tmp_path, monkeypatch, size):
    path = put(tmp_path, oversized_png_header(*size))

    def forbidden_decode(*args, **kwargs):
        raise AssertionError("Oversized image must never decode pixels")

    monkeypatch.setattr(ImageFile.ImageFile, "load", forbidden_decode)
    with pytest.raises(ImageSafetyError) as error:
        validate_image(path)
    assert error.value.code == "image_pixels_exceeded"
    assert detect_upload_mime(path) == OCTET_STREAM


@pytest.mark.parametrize("kind", [b"VP8 ", b"VP8L", b"VP8X"])
def test_webp_canvas_limit_precedes_native_decoder_construction(tmp_path, monkeypatch, kind):
    width, height = 5000, 4000
    if kind == b"VP8 ":
        data = b"\x00\x00\x00\x9d\x01\x2a" + struct.pack("<HH", width, height)
    elif kind == b"VP8L":
        data = b"\x2f" + struct.pack("<I", (width - 1) | ((height - 1) << 14))
    else:
        data = b"\x00\x00\x00\x00" + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    chunk = kind + struct.pack("<I", len(data)) + data + (b"\x00" if len(data) & 1 else b"")
    raw = b"RIFF" + struct.pack("<I", len(chunk) + 4) + b"WEBP" + chunk

    def forbidden_native_decoder(*args, **kwargs):
        raise AssertionError("WebP canvas must be checked before native decoder construction")

    monkeypatch.setattr(Image, "open", forbidden_native_decoder)
    with pytest.raises(ImageSafetyError) as error:
        validate_image(put(tmp_path, raw))
    assert error.value.code == "image_pixels_exceeded"


def test_webp_declared_length_cannot_hide_a_truncated_container(tmp_path):
    raw = bytearray(encoded("WEBP"))
    raw[4:8] = struct.pack("<I", len(raw) + 100)
    with pytest.raises(ImageSafetyError) as error:
        validate_image(put(tmp_path, raw))
    assert error.value.code == "invalid_image"


@pytest.mark.parametrize("frame_size,expected", [((5000, 4000), "image_pixels_exceeded"), ((4, 3), "invalid_image")])
def test_webp_extended_canvas_cannot_hide_another_bitstream_size(tmp_path, monkeypatch, frame_size, expected):
    def chunk(kind, data):
        return kind + struct.pack("<I", len(data)) + data + (b"\x00" if len(data) & 1 else b"")

    # Small claimed extended canvas followed by an inconsistent image header.
    canvas = b"\x00" * 10  # 1 x 1 canvas
    width, height = frame_size
    bitmap = b"\x2f" + struct.pack("<I", (width - 1) | ((height - 1) << 14))
    chunks = chunk(b"VP8X", canvas) + chunk(b"VP8L", bitmap)
    raw = b"RIFF" + struct.pack("<I", len(chunks) + 4) + b"WEBP" + chunks

    def forbidden_native_decoder(*args, **kwargs):
        raise AssertionError("Inconsistent bitstream dimensions must not reach the native decoder")

    monkeypatch.setattr(Image, "open", forbidden_native_decoder)
    with pytest.raises(ImageSafetyError) as error:
        validate_image(put(tmp_path, raw))
    assert error.value.code == expected


def test_pillow_bomb_exception_is_normalized_without_changing_global_settings(tmp_path, monkeypatch):
    path = put(tmp_path, encoded("PNG"))
    pixels = Image.MAX_IMAGE_PIXELS
    permissive = ImageFile.LOAD_TRUNCATED_IMAGES

    def decoder_limit(*args, **kwargs):
        raise Image.DecompressionBombError("decoder limit")

    monkeypatch.setattr(Image, "open", decoder_limit)
    with pytest.raises(ImageSafetyError) as error:
        validate_image(path)
    assert error.value.code == "image_pixels_exceeded"
    assert Image.MAX_IMAGE_PIXELS == pixels and ImageFile.LOAD_TRUNCATED_IMAGES == permissive


def test_permissive_decoder_setting_fails_closed_without_mutation(tmp_path, monkeypatch):
    path = put(tmp_path, encoded("PNG"))
    monkeypatch.setattr(ImageFile, "LOAD_TRUNCATED_IMAGES", True)
    with pytest.raises(ImageSafetyError) as error:
        validate_image(path)
    assert error.value.code == "invalid_image"
    assert ImageFile.LOAD_TRUNCATED_IMAGES is True
    assert detect_upload_mime(path) == OCTET_STREAM


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_bytes": 0}, {"max_bytes": True}, {"max_bytes": MAX_IMAGE_BYTES + 1},
        {"max_pixels": 0}, {"max_pixels": True}, {"max_pixels": MAX_IMAGE_PIXELS + 1},
    ],
)
def test_callers_cannot_disable_or_raise_compiled_limits(tmp_path, kwargs):
    with pytest.raises(ValueError):
        validate_image(tmp_path / "does-not-exist", **kwargs)


def test_missing_file_and_missing_decoder_are_not_silently_labeled_octet_stream(tmp_path, monkeypatch):
    with pytest.raises(FileNotFoundError):
        detect_upload_mime(tmp_path / "does-not-exist")
    path = put(tmp_path, encoded("PNG"))
    monkeypatch.setitem(sys.modules, "PIL", None)
    with pytest.raises(ImportError):
        detect_upload_mime(path)


def test_validation_has_a_shared_two_decode_concurrency_budget(tmp_path, monkeypatch):
    lock = threading.Lock()
    ready, release = threading.Event(), threading.Event()
    active = peak = 0

    def held_validation(*args, **kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if active == 2:
                ready.set()
        assert release.wait(5), "Test decode worker was not released"
        with lock:
            active -= 1
        return {"mime_type": "image/png", "width": 1, "height": 1, "size": 1}

    monkeypatch.setattr(image_safety, "_validate_snapshot", held_validation)
    with ThreadPoolExecutor(max_workers=4) as workers:
        futures = [workers.submit(validate_image, tmp_path / "unused") for _ in range(4)]
        try:
            assert ready.wait(2)
            assert not any(future.done() for future in futures)
        finally:
            release.set()
        assert all(future.result()["mime_type"] == "image/png" for future in futures)
    assert peak == 2


def test_module_import_preserves_the_dsh_helpers_standard_library_only_contract():
    result = subprocess.run(
        [sys.executable, "-S", "-c", "import image_safety, sys; assert 'PIL' not in sys.modules"],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
