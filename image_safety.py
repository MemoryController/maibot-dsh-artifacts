"""Bounded image validation for private snapshots, never extension-based MIME.

Pillow is imported only when decoding an image. Importing the shared protocol
from the DSH standard-library helper must not require site-packages or the SDK.
Call the synchronous helpers in a worker when used from an async plugin tool.
"""

from __future__ import annotations

import io
import struct
import threading
from pathlib import Path
from typing import Any

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000
MAX_IMAGE_DIMENSION = 8192
OCTET_STREAM = "application/octet-stream"
_MIME = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
_DECODE_BUDGET = threading.BoundedSemaphore(2)


class ImageSafetyError(ValueError):
    """An image was refused before it could become a tool media result."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _candidate(header: bytes) -> bool:
    return (
        header.startswith(b"\x89PNG\r\n\x1a\n")
        or header.startswith(b"\xff\xd8")
        or (header.startswith(b"RIFF") and header[8:12] == b"WEBP")
    )


def _dimensions(width: int, height: int, max_pixels: int) -> None:
    if width < 1 or height < 1:
        raise ImageSafetyError("invalid_image", "Image has invalid dimensions")
    if width * height > max_pixels or max(width, height) > MAX_IMAGE_DIMENSION:
        raise ImageSafetyError("image_pixels_exceeded", "Image exceeds the pixel or dimension limit")


def _preflight_webp(raw: bytes, max_pixels: int) -> None:
    """Bound the canvas before Pillow constructs libwebp's native decoder.

    Pillow's WebP open() creates WebPAnimDecoder before exposing image.size.
    Read only the specified RIFF header fields here; Pillow still verifies and
    decodes the complete file afterwards, and its dimensions are checked again.
    """
    if len(raw) < 20 or int.from_bytes(raw[4:8], "little") + 8 != len(raw):
        raise ImageSafetyError("invalid_image", "Invalid WebP container length")
    position = 12
    canvas = frame = None
    view = memoryview(raw)
    while position + 8 <= len(raw):
        kind = raw[position : position + 4]
        length = int.from_bytes(raw[position + 4 : position + 8], "little")
        end = position + 8 + length
        if end + (length & 1) > len(raw):
            raise ImageSafetyError("invalid_image", "Truncated WebP chunk")
        data = view[position + 8 : end]
        if kind == b"VP8X":
            if position != 12 or length != 10:
                raise ImageSafetyError("invalid_image", "Invalid WebP extended header")
            canvas = (1 + int.from_bytes(data[4:7], "little"), 1 + int.from_bytes(data[7:10], "little"))
            _dimensions(*canvas, max_pixels)
            if data[0] & 0x02:
                raise ImageSafetyError("animated_image", "Only static images are supported")
        elif kind in {b"ANIM", b"ANMF"}:
            raise ImageSafetyError("animated_image", "Only static images are supported")
        elif kind in {b"VP8 ", b"VP8L"}:
            if frame is not None:
                raise ImageSafetyError("invalid_image", "Multiple WebP image chunks")
            if kind == b"VP8L" and length >= 5 and data[0] == 0x2F:
                bits = int.from_bytes(data[1:5], "little")
                if bits >> 29:
                    raise ImageSafetyError("invalid_image", "Invalid WebP lossless version")
                frame = ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
            elif kind == b"VP8 " and length >= 10 and data[3:6] == b"\x9d\x01\x2a" and not data[0] & 1:
                frame = (int.from_bytes(data[6:8], "little") & 0x3FFF, int.from_bytes(data[8:10], "little") & 0x3FFF)
            else:
                raise ImageSafetyError("invalid_image", "Invalid WebP image header")
            _dimensions(*frame, max_pixels)
        elif position == 12:
            raise ImageSafetyError("invalid_image", "Invalid first WebP chunk")
        position = end + (length & 1)
    if position != len(raw) or frame is None or (canvas is not None and canvas != frame):
        raise ImageSafetyError("invalid_image", "Incomplete or inconsistent WebP dimensions")


def validate_image(
    path: Path,
    *,
    max_bytes: int = MAX_IMAGE_BYTES,
    max_pixels: int = MAX_IMAGE_PIXELS,
) -> dict[str, Any]:
    """Verify and fully decode a static PNG/JPEG/WebP without changing bytes.

    ``path`` must be a plugin-owned private snapshot, never a model path. Limits
    may be lowered but cannot exceed the compiled safety ceilings. Image header
    dimensions are checked before verification or pixel decoding. No process-
    global Pillow decoder or decompression-warning settings are changed.
    """
    if (
        type(max_bytes) is not int
        or not 1 <= max_bytes <= MAX_IMAGE_BYTES
        or type(max_pixels) is not int
        or not 1 <= max_pixels <= MAX_IMAGE_PIXELS
    ):
        raise ValueError("Invalid image safety limits")
    # Several chats can import/open simultaneously. Bound the decoded images
    # and encoded buffers held by this plugin, not just the size of one image.
    with _DECODE_BUDGET:
        return _validate_snapshot(Path(path), max_bytes, max_pixels)


def _validate_snapshot(path: Path, max_bytes: int, max_pixels: int) -> dict[str, Any]:
    with Path(path).open("rb") as source:
        raw = source.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ImageSafetyError("image_too_large", "Image exceeds the encoded byte limit")
    if not raw:
        raise ImageSafetyError("invalid_image", "Image is empty")
    if not _candidate(raw[:12]):
        raise ImageSafetyError("unsupported_image", "Only PNG, JPEG and WebP are supported")
    if raw[:4] == b"RIFF":
        _preflight_webp(raw, max_pixels)

    from PIL import Image, ImageFile

    # A different library enabling permissive decoding must not silently weaken
    # this validator. Refuse instead of mutating that process-global setting.
    if ImageFile.LOAD_TRUNCATED_IMAGES:
        raise ImageSafetyError("invalid_image", "Strict image decoding is unavailable")
    try:
        with Image.open(io.BytesIO(raw), formats=list(_MIME)) as image:
            kind = image.format
            if kind not in _MIME:
                raise ImageSafetyError("unsupported_image", "Only PNG, JPEG and WebP are supported")
            width, height = image.size
            _dimensions(width, height, max_pixels)
            image.verify()
        # verify() alone does not decode JPEG pixels. Reopen and load after
        # checking the dimensions, then refuse animation instead of choosing an
        # arbitrary frame on behalf of the user.
        with Image.open(io.BytesIO(raw), formats=list(_MIME)) as image:
            try:
                image.seek(1)
            except EOFError:
                image.seek(0)
            else:
                raise ImageSafetyError("animated_image", "Only static images are supported")
            image.load()
    except ImageSafetyError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise ImageSafetyError("image_pixels_exceeded", "Image exceeds decoder safety limits") from error
    except (OSError, ValueError, SyntaxError, EOFError, IndexError, OverflowError, struct.error) as error:
        raise ImageSafetyError("invalid_image", "Image verification or decoding failed") from error
    return {"mime_type": _MIME[kind], "width": width, "height": height, "size": len(raw)}


def detect_upload_mime(path: Path) -> str:
    """Recognize a bounded, fully validated image; keep other files importable.

    Non-images, corrupt images and images outside display limits remain ordinary
    binary attachments. Their filename is never used as evidence of a format.
    Filesystem errors and missing decoder dependencies still propagate.
    """
    with Path(path).open("rb") as source:
        header = source.read(12)
    if not _candidate(header):
        return OCTET_STREAM
    try:
        return str(validate_image(path)["mime_type"])
    except ImageSafetyError:
        return OCTET_STREAM
