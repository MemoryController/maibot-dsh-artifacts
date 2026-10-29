"""Real filesystem tests; fault injection covers permission and race windows."""

import errno
import hashlib
import os

import pytest

from safe_files import SafetyError, Workspace


@pytest.fixture
def root(tmp_path):
    root = tmp_path / "shared"
    (root / "outbox").mkdir(parents=True)
    (root / "inbox").mkdir()
    return root


@pytest.mark.parametrize("name", ["binary.bin", "报告.pdf", "空 格.png"])
def test_snapshot_preserves_bytes_and_name(root, tmp_path, name):
    content = b"\x00\xff\x80\r\n" * 13
    (root / "outbox" / name).write_bytes(content)
    target = tmp_path / "snapshot"
    with Workspace(root) as workspace:
        result = workspace.snapshot(f"outbox/{name}", target, len(content))
    assert result == {"name": name, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
    assert target.read_bytes() == content
    assert target.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "../outbox/a",
        "outbox/../a",
        "outbox/x/../../a",
        "outbox/./a",
        "outbox//a",
        "outbox/a/",
        "outbox/",
        "outbox",
        "inbox/a",
        "outbox\\a",
        "outbox/a\x00b",
        "outbox/a\nb",
        "outbox/a\x7fb",
        "outbox/ a",
        "outbox/a ",
        "outbox/" + "x" * 256,
        "outbox/" + "a/" * 32 + "file",
        "outbox/" + "a" * 4096,
    ],
)
def test_snapshot_rejects_bad_path(root, tmp_path, path):
    with Workspace(root) as workspace, pytest.raises(SafetyError):
        workspace.snapshot(path, tmp_path / "copy", 100)
    assert not (tmp_path / "copy").exists()


@pytest.mark.parametrize("path", ["outbox/a", "inbox/../a", "/inbox/a", "inbox/a/", "inbox/a\\b"])
def test_publish_rejects_bad_path(root, tmp_path, path):
    source = tmp_path / "source"
    source.write_bytes(b"x")
    with Workspace(root) as workspace, pytest.raises(SafetyError):
        workspace.publish(source, path, 100)


@pytest.mark.parametrize("root_path", ["/", "relative", "/tmp/../tmp", "/tmp//shared", "/tmp/shared/"])
def test_rejects_unsafe_root(root_path):
    with pytest.raises(SafetyError), Workspace(root_path):
        pytest.fail("unsafe root was accepted")


def test_symlink_in_root_rejected(root, tmp_path):
    link = tmp_path / "alias"
    link.symlink_to(root, target_is_directory=True)
    with pytest.raises(OSError), Workspace(link):
        pytest.fail("symlink root was accepted")


@pytest.mark.parametrize("kind", ["file", "directory"])
def test_snapshot_symlink_escape_rejected(root, tmp_path, kind):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_bytes(b"private")
    if kind == "file":
        (root / "outbox" / "secret").symlink_to(outside / "secret")
        relative = "outbox/secret"
    else:
        (root / "outbox" / "sub").symlink_to(outside, target_is_directory=True)
        relative = "outbox/sub/secret"
    with Workspace(root) as workspace, pytest.raises(OSError):
        workspace.snapshot(relative, tmp_path / "copy", 100)
    assert not (tmp_path / "copy").exists()


def test_snapshot_rejects_hardlink(root, tmp_path):
    outside = tmp_path / "secret"
    outside.write_bytes(b"private")
    os.link(outside, root / "outbox" / "secret")
    with Workspace(root) as workspace, pytest.raises(SafetyError, match="one hard link"):
        workspace.snapshot("outbox/secret", tmp_path / "copy", 100)


def test_snapshot_rejects_fifo_without_blocking(root, tmp_path):
    os.mkfifo(root / "outbox" / "pipe")
    with Workspace(root) as workspace, pytest.raises(SafetyError, match="regular"):
        workspace.snapshot("outbox/pipe", tmp_path / "copy", 100)


def test_snapshot_missing(root, tmp_path):
    with Workspace(root) as workspace, pytest.raises(FileNotFoundError):
        workspace.snapshot("outbox/missing", tmp_path / "copy", 100)


def test_snapshot_permission_error(root, tmp_path, monkeypatch):
    (root / "outbox" / "locked").write_bytes(b"x")
    original_open = os.open

    def deny(path, flags, *args, **kwargs):
        if path == "locked":
            raise PermissionError(errno.EACCES, "permission denied")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", deny)
    with Workspace(root) as workspace, pytest.raises(PermissionError):
        workspace.snapshot("outbox/locked", tmp_path / "copy", 100)
    assert not (tmp_path / "copy").exists()


def test_snapshot_limit_cleans_staging(root, tmp_path):
    (root / "outbox" / "big").write_bytes(b"abc")
    destination = tmp_path / "copy"
    with Workspace(root) as workspace, pytest.raises(SafetyError) as error:
        workspace.snapshot("outbox/big", destination, 2)
    assert error.value.code == "too_large"
    assert not destination.exists()


@pytest.mark.parametrize("limit,code", [(100, "source_changed"), (3, "too_large")])
def test_growth_during_snapshot_rejected(root, tmp_path, monkeypatch, limit, code):
    source = root / "outbox" / "growing"
    source.write_bytes(b"ab")
    original_read = os.read
    modified = False

    def grow(fd, size):
        nonlocal modified
        data = original_read(fd, size)
        if not modified:
            modified = True
            with source.open("ab") as writer:
                writer.write(b"cd")
        return data

    monkeypatch.setattr(os, "read", grow)
    with Workspace(root) as workspace, pytest.raises(SafetyError) as error:
        workspace.snapshot("outbox/growing", tmp_path / "copy", limit)
    assert error.value.code == code
    assert not (tmp_path / "copy").exists()


def test_existing_snapshot_destination_preserved(root, tmp_path):
    (root / "outbox" / "a").write_bytes(b"new")
    destination = tmp_path / "copy"
    destination.write_bytes(b"old")
    with Workspace(root) as workspace, pytest.raises(FileExistsError):
        workspace.snapshot("outbox/a", destination, 10)
    assert destination.read_bytes() == b"old"


def test_snapshot_parent_symlink_swap_stays_on_pinned_directory(root, tmp_path, monkeypatch):
    (root / "outbox" / "file").write_bytes(b"safe")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "file").write_bytes(b"private")
    original_open = os.open
    swapped = False

    def swap(path, flags, *args, **kwargs):
        nonlocal swapped
        fd = original_open(path, flags, *args, **kwargs)
        if path == "outbox" and not swapped:
            swapped = True
            (root / "outbox").rename(root / "pinned-outbox")
            (root / "outbox").symlink_to(outside, target_is_directory=True)
        return fd

    monkeypatch.setattr(os, "open", swap)
    with Workspace(root) as workspace:
        workspace.snapshot("outbox/file", tmp_path / "copy", 100)
    assert (tmp_path / "copy").read_bytes() == b"safe"


def test_publish_bytes_and_nested_directories(root, tmp_path):
    source = tmp_path / "download"
    source.write_bytes(b"\x00\xffbinary")
    with Workspace(root) as workspace:
        result = workspace.publish(source, "inbox/job/中文.pdf", 100)
    destination = root / "inbox" / "job" / "中文.pdf"
    assert result == {
        "path": "inbox/job/中文.pdf",
        "name": "中文.pdf",
        "size": 8,
        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
    }
    assert destination.read_bytes() == source.read_bytes()
    assert destination.stat().st_mode & 0o777 == 0o640
    assert sorted(p.name for p in destination.parent.iterdir()) == ["中文.pdf"]


@pytest.mark.parametrize("existing_kind", ["file", "symlink"])
def test_publish_does_not_overwrite_existing_target(root, tmp_path, existing_kind):
    source = tmp_path / "source"
    source.write_bytes(b"new")
    target = root / "inbox" / "output"
    original = tmp_path / "original"
    original.write_bytes(b"old")
    if existing_kind == "file":
        target.write_bytes(b"old")
    else:
        target.symlink_to(original)
    with Workspace(root) as workspace, pytest.raises(FileExistsError):
        workspace.publish(source, "inbox/output", 100)
    assert target.read_bytes() == b"old"
    assert target.is_symlink() == (existing_kind == "symlink")
    assert [p.name for p in target.parent.iterdir()] == ["output"]


def test_publish_parent_symlink_escape_rejected(root, tmp_path):
    source = tmp_path / "source"
    source.write_bytes(b"x")
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "inbox" / "link").symlink_to(outside, target_is_directory=True)
    with Workspace(root) as workspace, pytest.raises(OSError):
        workspace.publish(source, "inbox/link/output", 100)
    assert not list(outside.iterdir())


def test_publish_copy_failure_exposes_no_partial_file(root, tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.write_bytes(b"partial")
    original_read = os.read
    reads = 0

    def fail(fd, size):
        nonlocal reads
        reads += 1
        if reads == 2:
            raise OSError(errno.EIO, "injected read error")
        return original_read(fd, size)

    monkeypatch.setattr(os, "read", fail)
    with Workspace(root) as workspace, pytest.raises(OSError, match="injected"):
        workspace.publish(source, "inbox/output", 100)
    assert not list((root / "inbox").iterdir())


def test_publish_limit_exposes_no_partial_file(root, tmp_path):
    source = tmp_path / "source"
    source.write_bytes(b"toolarge")
    with Workspace(root) as workspace, pytest.raises(SafetyError) as error:
        workspace.publish(source, "inbox/output", 2)
    assert error.value.code == "too_large"
    assert not list((root / "inbox").iterdir())


def test_publish_parent_swap_cannot_redirect_write_outside_workspace(root, tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.write_bytes(b"safe")
    outside = tmp_path / "outside"
    outside.mkdir()
    original_link = os.link

    def swap_then_link(*args, **kwargs):
        (root / "inbox").rename(root / "pinned-inbox")
        (root / "inbox").symlink_to(outside, target_is_directory=True)
        return original_link(*args, **kwargs)

    monkeypatch.setattr(os, "link", swap_then_link)
    with Workspace(root) as workspace:
        workspace.publish(source, "inbox/output", 100)
    assert not list(outside.iterdir())
    assert (root / "pinned-inbox" / "output").read_bytes() == b"safe"
    assert [p.name for p in (root / "pinned-inbox").iterdir()] == ["output"]


def test_workspace_must_be_opened(root, tmp_path):
    with pytest.raises(RuntimeError, match="context manager"):
        Workspace(root).snapshot("outbox/a", tmp_path / "copy", 10)


def test_validate_target_accepts_missing_directories_without_creating_them(root):
    with Workspace(root) as workspace:
        workspace.validate_target("inbox/new/sub/output")
    assert not (root / "inbox" / "new").exists()


@pytest.mark.parametrize("existing_kind", ["file", "dangling_symlink", "directory"])
def test_validate_target_rejects_any_existing_entry(root, existing_kind):
    target = root / "inbox" / "output"
    if existing_kind == "file":
        target.write_bytes(b"old")
    elif existing_kind == "directory":
        target.mkdir()
    else:
        target.symlink_to(root / "does-not-exist")
    with Workspace(root) as workspace, pytest.raises(FileExistsError):
        workspace.validate_target("inbox/output")


def test_validate_target_rejects_parent_symlink(root, tmp_path):
    (root / "inbox" / "alias").symlink_to(tmp_path, target_is_directory=True)
    with Workspace(root) as workspace, pytest.raises(OSError):
        workspace.validate_target("inbox/alias/output")
