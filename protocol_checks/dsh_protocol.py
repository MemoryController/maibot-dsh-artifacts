"""Real pinned DSH dist producer -> this plugin's request/Handoff consumer.

Run explicitly with DSH_ARTIFACTS_SOURCE and DSH_ARTIFACTS_COMMIT. This does not
fetch code, start DSH/ACP/QQ, or exercise actual Host capability issuance. The
only file-service boundary is a scope-checking test double.
"""

import hashlib
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from handoff import Handoff
from request import import_request

SUPPORTED = {
    "0958d8ac788ebbea5f2c507f3b024de3dd06cb24": "0.2.0",
    "f008cdc039122d360dee67b86e1e2004083ba019": "0.2.1",
}
NODE_PRODUCER = r"""
import { pathToFileURL } from 'node:url';
import { join } from 'node:path';
import { ArtifactService } from './dist/core.js';
const [root, state, pathsJSON] = process.argv.slice(1);
const owner = 'isolated-protocol-test';
const config = { workspaceRoot: root, stateDir: state, confirmationMode: 'request' };
const service = new ArtifactService(config);
const prepared = await service.prepare(JSON.parse(pathsJSON), owner);
const submission = await service.publish(prepared.manifest.task_id, owner);
const restarted = new ArtifactService(config);
const local = await restarted.localStatus(prepared.manifest.task_id, owner);
const defaults = new ArtifactService({ workspaceRoot: root, stateDir: state + '-default' });
const defaultPrepared = await defaults.prepare(JSON.parse(pathsJSON), owner);
const defaultSubmission = await defaults.publish(defaultPrepared.manifest.task_id, owner);
console.log(JSON.stringify({ manifest: prepared.manifest, submission, local, defaultSubmission }));
"""


class ScopedFiles:
    def __init__(self):
        self.scope = "original-chat"
        self.assets = {"original-chat": {}, "another-chat": {}}
        self.uploads = []
        self.sends = []
        self.uncertain = False

    async def list(self, offset=0):
        if self.scope is None:
            raise PermissionError("No current Host scope")
        return list(self.assets[self.scope].values())[offset : offset + 50]

    async def upload(self, path, *, name):
        content = Path(path).read_bytes()
        ref = f"{len(self.uploads) + 1:032x}"
        metadata = {
            "file_ref": ref,
            "name": name,
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "state": "ready",
        }
        self.uploads.append((self.scope, content, metadata))
        self.assets[self.scope][ref] = metadata
        if self.uncertain:
            raise TimeoutError("Simulated lost receipt after registration")
        return metadata

    async def send(self, file_ref):
        self.sends.append(file_ref)
        raise AssertionError("Request import must never send")


@pytest.fixture(scope="module")
def producer_source():
    source, expected = os.environ.get("DSH_ARTIFACTS_SOURCE"), os.environ.get("DSH_ARTIFACTS_COMMIT")
    assert source and expected in SUPPORTED, "Supply an explicitly supported DSH source + exact SHA"
    root = Path(source).resolve(strict=True)
    actual = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    assert actual == expected
    subprocess.run(
        ["git", "diff", "--exit-code", expected, "--", "package.json", "dist", "src"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    package = json.loads((root / "package.json").read_text())
    assert package["version"] == SUPPORTED[expected]
    assert package["engines"]["dsh"] == "0.2.2-alpha.2"
    assert int(subprocess.check_output(["node", "-p", "process.versions.node.split('.')[0]"], text=True)) >= 24
    return root


@pytest.fixture
def cross(producer_source, tmp_path):
    shared, runtime, data = (tmp_path / name for name in ("shared", "runtime", "data"))
    (shared / "outbox").mkdir(parents=True)
    runtime.mkdir(mode=0o700)
    names = ["中文 报告.txt", "图片🧪.png", "empty.bin", "binary.bin"]
    contents = ["中文\r\n与空格\n".encode(), bytes.fromhex("89504e470d0a1a0a"), b"", bytes(range(256))]
    paths = [f"outbox/{name}" for name in names]
    for path, content in zip(paths, contents, strict=True):
        (shared / path).write_bytes(content)
    output = subprocess.check_output(
        [
            "node",
            "--input-type=module",
            "-e",
            NODE_PRODUCER,
            str(shared),
            str(tmp_path / "dsh-private"),
            json.dumps(paths, ensure_ascii=False),
        ],
        cwd=producer_source,
        text=True,
        timeout=30,
    )
    produced = json.loads(output)
    args = produced["submission"]["confirmation"]["arguments"]
    files = ScopedFiles()
    return SimpleNamespace(
        shared=shared,
        runtime=runtime,
        data=data,
        files=files,
        paths=paths,
        contents=contents,
        produced=produced,
        args=args,
        broker=Handoff(files, str(shared), runtime, data, 50 * 1024**2, 8),
    )


async def confirm(s):
    return await import_request(s.broker, s.args["task_id"], s.args["request_sha256"])


async def test_actual_dsh_bytes_schema_and_registration(cross):
    s = cross
    request = s.shared / f"outbox/.dsh-handoffs/{s.args['task_id']}.json"
    raw = request.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == s.args["request_sha256"]
    assert json.loads(raw) == s.produced["manifest"]
    assert s.produced["submission"]["confirmation"]["tool"] == "dsh_import_request"
    assert s.produced["submission"]["result"] == "submitted"
    assert s.produced["submission"]["state"] == "prepared"
    assert s.produced["submission"]["maibot_registration"] == "unconfirmed"
    assert s.produced["local"]["state"] == "prepared"  # DSH local state is not an import receipt.
    result = await confirm(s)
    assert result["registered"] == "confirmed" and result["delivery"] == "not_requested"
    assert [item["file"]["name"] for item in result["items"]] == [Path(path).name for path in s.paths]
    assert [data for _, data, _ in s.files.uploads] == s.contents
    assert {item["file"]["file_ref"] for item in result["items"]} == {
        metadata["file_ref"] for _, _, metadata in s.files.uploads
    }
    assert all(scope == "original-chat" for scope, _, _ in s.files.uploads)
    assert not s.files.sends


async def test_duplicate_restart_and_scope_isolation(cross):
    s = cross
    first = await confirm(s)
    refs = {item["file"]["file_ref"] for item in first["items"]}
    s.broker = Handoff(s.files, str(s.shared), s.runtime, s.data, 50 * 1024**2, 8)
    assert (await confirm(s))["cached"] is True
    assert len(s.files.uploads) == 4
    s.files.scope = "another-chat"
    rejected = await confirm(s)
    assert rejected["registered"] == "unconfirmed"
    assert not any(ref in repr(rejected) for ref in refs)
    assert len(s.files.uploads) == 4 and not s.files.sends


async def test_lost_registration_receipt_is_not_retried(cross):
    s = cross
    s.files.uncertain = True
    result = await confirm(s)
    assert result["registered"] == "unknown"
    count = len(s.files.uploads)
    s.files.uncertain = False
    await confirm(s)
    s.broker = Handoff(s.files, str(s.shared), s.runtime, s.data, 50 * 1024**2, 8)
    await confirm(s)
    assert len(s.files.uploads) == count and not s.files.sends


async def test_dsh_submission_without_confirmation_never_imports(cross):
    s = cross
    assert s.produced["submission"]["result"] == "submitted"
    assert not s.files.uploads and not s.files.sends and not s.data.exists()
    result = await s.broker.status(s.args["task_id"])
    assert result["registered"] == "unconfirmed"
    assert not s.data.exists()


async def test_tampering_after_dsh_publication_fails_closed(cross):
    s = cross
    request = s.shared / f"outbox/.dsh-handoffs/{s.args['task_id']}.json"
    request.write_bytes(request.read_bytes() + b"\n")
    result = await confirm(s)
    assert result["error"]["code"] == "request_hash_mismatch"
    assert not s.files.uploads and not s.data.exists()


async def test_default_confirmation_mode_uses_an_available_plugin_tool(cross):
    s = cross
    confirmation = s.produced["defaultSubmission"]["confirmation"]
    expected = os.environ["DSH_ARTIFACTS_COMMIT"]
    if SUPPORTED[expected] == "0.2.0":
        assert confirmation["tool"] == "dsh_import_request"

        async def execute():
            return await import_request(s.broker, **confirmation["arguments"])
    else:
        assert confirmation["tool"] == "dsh_import_handoff"
        assert confirmation["arguments"]["send"] is False

        async def execute():
            return await s.broker.import_manifest(**confirmation["arguments"])

    assert (await execute())["registered"] == "confirmed"
    assert (await execute())["cached"] is True
    assert len(s.files.uploads) == 4 and not s.files.sends
