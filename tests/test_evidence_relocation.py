"""Relocation preserves custody and fails closed before namespace changes."""

import json
import os
import runpy
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

relocation = runpy.run_path(
    str(Path(__file__).parents[1] / "scripts" / "relocate_closed_evidence.py")
)
manifest = relocation["tree_manifest"]
verify = relocation["verify_tree"]
allocation = relocation["allocation_estimate"]
checked_paths = relocation["checked_paths"]
scan = relocation["scan_handles"]
lock = relocation["relocation_lock"]
retire = relocation["retire_backend"]


def fixture_tree(root: Path) -> Path:
    """Include opaque bytes, a hardlink and nanosecond timestamps."""
    root.mkdir(mode=0o700)
    child = root / "opaque.bin"
    child.write_bytes(bytes(range(256)) * 8)
    child.chmod(0o600)
    os.utime(child, ns=(1700000000000000001, 1700000000000000003))
    os.link(child, root / "shared.bin")
    (root / "empty").mkdir(mode=0o700)
    return root


def test_complete_manifest_and_namespace_guards(tmp_path: Path) -> None:
    """Verify bytes, modes, exact timestamps and complete member coverage."""
    root = fixture_tree(tmp_path / "source")
    snapshot = manifest(root)
    verify(root, snapshot)
    assert snapshot["opaque.bin"]["mtime_ns"] == 1700000000000000003
    assert snapshot["opaque.bin"]["sha256"] == snapshot["shared.bin"]["sha256"]
    (root / "extra").write_bytes(b"unexpected")
    with pytest.raises(ValueError, match="coverage"):
        verify(root, snapshot)


@pytest.mark.parametrize("change", ["bytes", "mode", "mtime", "missing"])
def test_changed_or_missing_members_fail(tmp_path: Path, change: str) -> None:
    """A copy cannot pass with changed payloads or incomplete metadata."""
    root = fixture_tree(tmp_path / "source")
    snapshot = manifest(root)
    path = root / "opaque.bin"
    if change == "bytes":
        path.write_bytes(b"changed")
    elif change == "mode":
        path.chmod(0o640)
    elif change == "mtime":
        os.utime(path, ns=(1, 2))
    else:
        path.unlink()
    with pytest.raises(ValueError):
        verify(root, snapshot)


def test_symlink_and_unsafe_manifest_members_fail(tmp_path: Path) -> None:
    """External files and traversal addresses never become retirement members."""
    root = fixture_tree(tmp_path / "source")
    snapshot = manifest(root)
    with pytest.raises(ValueError, match="member"):
        verify(root, {**snapshot, "../outside": snapshot["opaque.bin"]})
    (root / "linked").symlink_to(tmp_path / "outside")
    with pytest.raises(ValueError, match="symlink"):
        manifest(root)


def test_hardlinks_do_not_overstate_reclaim(tmp_path: Path) -> None:
    """Cross-root ownership prevents counting shared bytes as reclaimed space."""
    root = fixture_tree(tmp_path / "source")
    before = allocation(root)
    assert before["exclusive_inode_bytes"] == before["selected_inode_bytes"]
    os.link(root / "opaque.bin", tmp_path / "outside")
    after = allocation(root)
    assert after["selected_inode_bytes"] == before["selected_inode_bytes"]
    assert after["exclusive_inode_bytes"] == 0


@pytest.mark.parametrize("kind", ["nested", "symlink", "relative"])
def test_unsafe_or_overlapping_namespaces_fail(tmp_path: Path, kind: str) -> None:
    """Source, destination and journal cannot alias one another."""
    source = fixture_tree(tmp_path / "source")
    destination = tmp_path / "destination"
    evidence = tmp_path / "evidence"
    if kind == "nested":
        destination = source / "nested"
    elif kind == "symlink":
        destination.symlink_to(tmp_path / "other")
    else:
        destination = Path("relative")
    with pytest.raises(ValueError):
        checked_paths(source, destination, evidence)


def test_read_handles_allowed_writers_and_unknown_access_visible(tmp_path: Path) -> None:
    """Handle classification uses actual flags; an idle writer is still unsafe."""
    source = fixture_tree(tmp_path / "source")
    proc = tmp_path / "proc"
    fd = proc / "123" / "fd"
    fd.mkdir(parents=True)
    info = proc / "123" / "fdinfo"
    info.mkdir()
    (proc / "123" / "maps").write_text("")
    (fd / "7").symlink_to(source / "opaque.bin")
    (info / "7").write_text("flags:\t0100000\n")
    assert scan(source, proc)["handles"][0]["writable"] is False
    (info / "7").write_text("flags:\t0100002\n")
    assert scan(source, proc)["handles"][0]["writable"] is True
    (info / "7").write_text("malformed")
    assert scan(source, proc)["denied"]


def test_global_lock_serializes_different_transactions(tmp_path: Path) -> None:
    """Two cutovers sharing a coordinator cannot enter together."""
    with lock(tmp_path), pytest.raises(BlockingIOError), lock(tmp_path):
        pytest.fail("a second relocation entered the global lock")


def test_retirement_resume_keeps_verified_copy_and_open_reader(tmp_path: Path) -> None:
    """A committed retirement resumes missing members without losing read handles."""
    backend = fixture_tree(tmp_path / "backend")
    snapshot = manifest(backend)
    journal = tmp_path / "journal"
    journal.mkdir()
    relocation["private_json"](journal / "manifest.private.json", snapshot)
    relocation["private_json"](
        journal / "retirement-started.private.json",
        {"manifest_sha256": relocation["digest"](journal / "manifest.private.json")},
    )
    with (backend / "opaque.bin").open("rb") as reader:
        (backend / "shared.bin").unlink()
        retire(backend, snapshot, journal, started=True)
        assert reader.read() == bytes(range(256)) * 8
    assert not list(backend.iterdir())
    assert json.loads((journal / "retirement-complete.private.json").read_bytes())["complete"]


def test_broken_hardlink_relationship_fails_copy_validation(tmp_path: Path) -> None:
    """Identical bytes cannot disguise lost sharing inside the selected namespace."""
    source = fixture_tree(tmp_path / "source")
    snapshot = manifest(source)
    destination = tmp_path / "copy"
    shutil.copytree(source, destination, copy_function=shutil.copy2)
    with pytest.raises(ValueError, match="changed"):
        verify(destination, snapshot)


def test_copy_directory_timestamp_completion_preserves_every_payload_guard(tmp_path: Path) -> None:
    """Complete directory timestamps deterministically without hiding changed bytes."""
    source = fixture_tree(tmp_path / "source")
    destination = fixture_tree(tmp_path / "destination")
    snapshot = manifest(source)
    relocation["complete_directory_timestamps"](destination, snapshot)
    verify(destination, snapshot)
    (destination / "opaque.bin").write_bytes(b"altered")
    with pytest.raises(ValueError, match="guard changed"):
        relocation["complete_directory_timestamps"](destination, snapshot)


@pytest.mark.parametrize("code,expected", [(0, True), (32, False), (1, None)])
def test_unknown_mount_state_fails_visibly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int, expected: bool | None
) -> None:
    """An observation failure cannot be interpreted as permission to replace a mount."""
    monkeypatch.setattr(
        relocation["subprocess"], "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=code)
    )
    if expected is None:
        with pytest.raises(ValueError, match="could not be verified"):
            relocation["is_mountpoint"](tmp_path)
    else:
        assert relocation["is_mountpoint"](tmp_path) is expected
