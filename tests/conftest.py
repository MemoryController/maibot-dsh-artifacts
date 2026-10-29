"""Use the real SDK and the package loading layout used by MaiBot Runner."""

import hashlib
import importlib.util
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.fixture(scope="session")
def plugin_module():
    root = Path(__file__).resolve().parents[1]
    name = "dsh_plugin_under_test"
    spec = importlib.util.spec_from_file_location(name, root / "plugin.py", submodule_search_locations=[str(root)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def bridge(plugin_module, tmp_path):
    root = tmp_path / "shared"
    (root / "outbox").mkdir(parents=True)
    (root / "inbox").mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    uploads = []

    async def upload(path, *, name):
        content = Path(path).read_bytes()
        uploads.append((Path(path), name, content))
        return {
            "file_ref": f"{len(uploads):032x}",
            "name": name,
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "mime_type": "application/octet-stream",
            "url": "https://private.invalid/signed-upload-url",
        }

    async def download(file_ref, destination):
        Path(destination).write_bytes(b"\x00\xffdownload")
        return {"file_ref": file_ref, "name": "original.bin", "size": 10, "mime_type": "application/octet-stream"}

    files = SimpleNamespace(
        upload=AsyncMock(side_effect=upload),
        download=AsyncMock(side_effect=download),
        send=AsyncMock(return_value={"status": "sent", "message_id": "message-1"}),
    )
    plugin = plugin_module.create_plugin()
    plugin._ctx = SimpleNamespace(
        files=files,
        paths=SimpleNamespace(runtime_dir=runtime),
        logger=logging.getLogger("test"),
    )
    plugin.set_plugin_config({"plugin": {"config_version": "1.0.0"}, "workspace": {"root": str(root)}})
    return SimpleNamespace(plugin=plugin, root=root, runtime=runtime, files=files, uploads=uploads)
