"""Apply the shipped patch to disposable copies of the pinned real Host sources.

CI supplies MAIBOT_SOURCE_PATH via a separate v1.3.18 checkout. No production
Host is modified or started and no upstream source fixture is vendored here.
"""

import ast
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from integrations.maibot_1_3_18 import install as installer


@pytest.fixture
def host_tree(tmp_path):
    configured = os.environ.get("MAIBOT_SOURCE_PATH")
    source = Path(configured) if configured else Path("/tmp/maibot-upstream")
    if not source.is_dir():
        pytest.skip("Set MAIBOT_SOURCE_PATH to an unmodified MaiBot v1.3.18 checkout for Host patch tests")
    spec = json.loads((installer.HERE / "checksums.json").read_text())
    root = tmp_path / "host"
    root.mkdir()
    for name, hashes in spec["files"].items():
        original = source / name
        assert original.is_file(), f"Pinned Host source missing: {name}"
        assert installer.digest(original) == hashes["before"], f"Test checkout differs from v1.3.18: {name}"
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, target)
    (root / "local-operator-config.toml").write_text("keep = true\n")
    return root, spec


def tree_bytes(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_digest_uses_sha256(tmp_path):
    path = tmp_path / "binary"
    path.write_bytes(bytes(range(256)))
    assert installer.digest(path) == hashlib.sha256(bytes(range(256))).hexdigest()


def test_nonexistent_host_root_is_not_created(tmp_path):
    target = tmp_path / "missing"
    with pytest.raises(FileNotFoundError):
        installer.install(target)
    assert not target.exists()


def test_read_only_check_does_not_modify_or_add_files(host_tree):
    root, _ = host_tree
    before = tree_bytes(root)
    assert installer.install(root) == "compatible_not_applied"
    assert installer.install(root, apply=False) == "compatible_not_applied"
    assert tree_bytes(root) == before
    assert not (root / ".git").exists()  # git apply works without a Git checkout.


def test_explicit_apply_changes_exactly_five_sources_and_adds_three_modules(host_tree):
    root, spec = host_tree
    before = tree_bytes(root)
    assert installer.install(root, apply=True) == "applied"
    after = tree_bytes(root)
    assert {name for name in before if before[name] != after[name]} == set(spec["files"])
    assert set(after) - set(before) == {f"src/agent/{name}" for name in installer.OVERLAY}
    assert len(installer.OVERLAY) == 3
    for name, hashes in spec["files"].items():
        assert installer.digest(root / name) == hashes["after"]
        ast.parse((root / name).read_text())
    for name, source in installer.OVERLAY.items():
        assert (root / "src/agent" / name).read_bytes() == source.read_bytes()
        ast.parse((root / "src/agent" / name).read_text())
    assert (root / "local-operator-config.toml").read_text() == "keep = true\n"


def test_repeated_apply_and_check_are_idempotent(host_tree):
    root, _ = host_tree
    assert installer.install(root, apply=True) == "applied"
    installed = tree_bytes(root)
    assert installer.install(root, apply=True) == "already_applied"
    assert installer.install(root, apply=False) == "already_applied"
    assert tree_bytes(root) == installed


def test_unknown_local_source_edit_is_preserved_without_writes(host_tree):
    root, spec = host_tree
    changed = root / next(iter(spec["files"]))
    changed.write_text(changed.read_text() + "\n# operator customization\n")
    before = tree_bytes(root)
    with pytest.raises(ValueError, match="differs from pinned"):
        installer.install(root, apply=True)
    assert tree_bytes(root) == before


def test_partial_host_patch_is_refused_without_writes(host_tree):
    root, spec = host_tree
    original = root / next(iter(spec["files"]))
    original_bytes = original.read_bytes()
    installer.install(root, apply=True)
    original.write_bytes(original_bytes)
    partial = tree_bytes(root)
    with pytest.raises(ValueError, match="Partial Host patch"):
        installer.install(root, apply=True)
    assert tree_bytes(root) == partial


def test_partial_overlay_is_refused_without_writes(host_tree):
    root, _ = host_tree
    installer.install(root, apply=True)
    (root / "src/agent/artifact_provider.py").unlink()
    partial = tree_bytes(root)
    with pytest.raises(ValueError, match="Partial overlay"):
        installer.install(root, apply=True)
    assert tree_bytes(root) == partial


def test_different_existing_overlay_is_not_overwritten(host_tree):
    root, _ = host_tree
    path = root / "src/agent/artifact_provider.py"
    path.write_text("# user's unrelated module\n")
    before = tree_bytes(root)
    with pytest.raises(ValueError, match="Refusing to overwrite"):
        installer.install(root, apply=True)
    assert tree_bytes(root) == before


def test_symlinked_source_is_refused(host_tree, tmp_path):
    root, spec = host_tree
    path = root / next(iter(spec["files"]))
    outside = tmp_path / "outside-source.py"
    path.rename(outside)
    path.symlink_to(outside)
    before = tree_bytes(root)
    outside_bytes = outside.read_bytes()
    with pytest.raises(ValueError, match="linked Host source"):
        installer.install(root, apply=True)
    assert tree_bytes(root) == before
    assert outside.read_bytes() == outside_bytes


def test_symlinked_overlay_is_refused(host_tree, tmp_path):
    root, _ = host_tree
    outside = tmp_path / "outside.py"
    outside.write_bytes(installer.OVERLAY["artifact_provider.py"].read_bytes())
    (root / "src/agent/artifact_provider.py").symlink_to(outside)
    before = tree_bytes(root)
    with pytest.raises(ValueError, match="Refusing to overwrite"):
        installer.install(root, apply=True)
    assert tree_bytes(root) == before


def test_git_apply_check_failure_prevents_all_writes(host_tree, monkeypatch):
    root, _ = host_tree
    before = tree_bytes(root)

    def reject(command, **kwargs):
        assert "--check" in command
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(installer.subprocess, "run", reject)
    with pytest.raises(subprocess.CalledProcessError):
        installer.install(root, apply=True)
    assert tree_bytes(root) == before


def test_apply_failure_removes_only_new_overlay_modules(host_tree, monkeypatch):
    root, _ = host_tree
    before = tree_bytes(root)
    real_run = installer.subprocess.run

    def reject_apply(command, **kwargs):
        if "--check" in command:
            return real_run(command, **kwargs)
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(installer.subprocess, "run", reject_apply)
    with pytest.raises(subprocess.CalledProcessError):
        installer.install(root, apply=True)
    assert tree_bytes(root) == before
