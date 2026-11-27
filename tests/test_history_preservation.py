"""Immutable privacy histories are verified and registered without repeated writes."""

import errno
import hashlib
import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from html_to_markdown import capture_store
from html_to_markdown.baseline_history_lookup import HistoryLookup
from html_to_markdown.content_inventory import atomic_private
from html_to_markdown.history_preservation import history_transaction, preserve_history

DATA = b'{"binding":{},"privacy_feedback":["original","exhausted"]}\n'


def history_path(output):
    return output / "history" / (hashlib.sha256(DATA).hexdigest() + ".json")


def generation(path):
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def preserve_in_process(output):
    return generation(preserve_history(Path(output), DATA))


@pytest.mark.parametrize("mask", [0o022, 0o077])
def test_repeated_preservation_does_not_replace_verified_generation(tmp_path, mask):
    previous = os.umask(mask)
    try:
        output = tmp_path / "review"
        path = preserve_history(output, DATA)
        before = generation(path)
        assert preserve_history(output, DATA) == path
        assert generation(path) == before
        assert path.read_bytes() == DATA
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
        assert list(path.parent.iterdir()) == [path]
    finally:
        os.umask(previous)


def test_reuse_does_not_stage_or_write_another_payload(tmp_path, monkeypatch):
    output = tmp_path / "review"
    path = preserve_history(output, DATA)

    def unexpected(*_args):
        raise AssertionError("identical history must not be written again")

    monkeypatch.setattr(capture_store, "atomic_write", unexpected)
    assert preserve_history(output, DATA) == path


def test_concurrent_threads_keep_one_complete_generation(tmp_path):
    output = tmp_path / "review"
    barrier = Barrier(4)

    def worker(_index):
        barrier.wait(timeout=5)
        return generation(preserve_history(output, DATA))

    with ThreadPoolExecutor(max_workers=4) as pool:
        snapshots = list(pool.map(worker, range(4)))
    assert len(set(snapshots)) == 1
    path = history_path(output)
    assert path.read_bytes() == DATA
    assert list(path.parent.iterdir()) == [path]


def test_concurrent_processes_keep_one_complete_generation(tmp_path):
    output = tmp_path / "review"
    with ProcessPoolExecutor(
        max_workers=4, mp_context=multiprocessing.get_context("spawn")
    ) as pool:
        snapshots = list(pool.map(preserve_in_process, [str(output)] * 8))
    assert len(set(snapshots)) == 1
    path = history_path(output)
    assert path.read_bytes() == DATA
    assert list(path.parent.iterdir()) == [path]


def test_reused_history_registers_with_owning_transaction(tmp_path):
    output = tmp_path / "review"
    path = preserve_history(output, DATA)
    before = generation(path)
    observer = HistoryLookup(output, output)
    observer.paths({})
    calls = []
    original = observer.remember

    def remember(saved):
        calls.append(saved)
        original(saved)

    observer.remember = remember
    with history_transaction(observer):
        preserve_history(output, DATA)
    assert calls == [path]
    assert generation(path) == before
    observer.verify()


@pytest.mark.parametrize("damage", ["corrupt", "directory", "symlink", "dangling_symlink"])
def test_invalid_existing_history_is_never_overwritten_or_registered(tmp_path, damage):
    output = tmp_path / "review"
    observer = HistoryLookup(output, output)
    observer.paths({})
    path = history_path(output)
    path.parent.mkdir(parents=True, mode=0o700)
    outside = tmp_path / "outside"
    atomic_private(outside, b"protected bytes")
    if damage == "corrupt":
        atomic_private(path, b"corrupt history")
    elif damage == "directory":
        path.mkdir()
    elif damage == "symlink":
        path.symlink_to(outside)
    else:
        path.symlink_to(tmp_path / "absent")
    before = path.lstat()
    with history_transaction(observer), pytest.raises(ValueError, match="digest|symlink|immutable"):
        preserve_history(output, DATA)
    after = path.lstat()
    assert (after.st_ino, after.st_mode) == (before.st_ino, before.st_mode)
    assert outside.read_bytes() == b"protected bytes"
    assert observer.metrics["files_parsed"] == 0


@pytest.mark.parametrize("mode", [0o644, 0o660])
def test_non_private_history_is_rejected_without_changing_permissions(tmp_path, mode):
    output = tmp_path / "review"
    path = preserve_history(output, DATA)
    path.chmod(mode)
    with pytest.raises(ValueError, match="private owner"):
        preserve_history(output, DATA)
    assert path.stat().st_mode & 0o777 == mode


def test_foreign_owned_history_is_rejected(tmp_path, monkeypatch):
    output = tmp_path / "review"
    path = preserve_history(output, DATA)
    uid = os.getuid()
    monkeypatch.setattr(capture_store.os, "getuid", lambda: uid + 1)
    with pytest.raises(ValueError, match="private owner"):
        preserve_history(output, DATA)
    assert path.read_bytes() == DATA


def test_symlinked_parent_cannot_receive_history(tmp_path):
    output = tmp_path / "review"
    output.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (output / "history").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        preserve_history(output, DATA)
    assert not list(outside.iterdir())


def test_failed_new_publication_retains_private_staging(tmp_path, monkeypatch):
    output = tmp_path / "review"

    def interrupted(*_args):
        raise OSError(errno.ENOSPC, "synthetic publication interruption")

    monkeypatch.setattr(capture_store.os, "link", interrupted)
    with pytest.raises(OSError, match="synthetic publication interruption"):
        preserve_history(output, DATA)
    assert not history_path(output).exists()
    staged = list((output / "history").glob("markdown-codex-*/payload"))
    assert len(staged) == 1
    assert staged[0].read_bytes() == DATA
    assert staged[0].stat().st_mode & 0o077 == 0


def test_losing_writer_verifies_and_retains_winner_without_replacement(tmp_path, monkeypatch):
    output = tmp_path / "review"
    path = history_path(output)
    winner = []

    def competing(_source, destination):
        atomic_private(destination, DATA)
        winner.append(generation(destination))
        raise FileExistsError(errno.EEXIST, "synthetic competing publication")

    monkeypatch.setattr(capture_store.os, "link", competing)
    assert preserve_history(output, DATA) == path
    assert generation(path) == winner[0]
    assert list(path.parent.iterdir()) == [path]


def test_corrupt_competing_publication_cannot_be_replaced(tmp_path, monkeypatch):
    output = tmp_path / "review"
    path = history_path(output)

    def competing(_source, destination):
        atomic_private(destination, b"corrupt competing generation")
        raise FileExistsError(errno.EEXIST, "synthetic competing publication")

    monkeypatch.setattr(capture_store.os, "link", competing)
    with pytest.raises(ValueError, match="digest"):
        preserve_history(output, DATA)
    assert path.read_bytes() == b"corrupt competing generation"
    assert list(path.parent.glob("markdown-codex-*/payload"))


def test_failed_directory_durability_cannot_register_history(tmp_path, monkeypatch):
    output = tmp_path / "review"
    observer = HistoryLookup(output, output)
    observer.paths({})

    def interrupted(*_args):
        raise OSError(errno.EIO, "synthetic history durability failure")

    monkeypatch.setattr(capture_store, "_flush_directory", interrupted)
    with history_transaction(observer), pytest.raises(OSError, match="durability failure"):
        preserve_history(output, DATA)
    assert history_path(output).read_bytes() == DATA
    assert observer.metrics["files_parsed"] == 0
    assert list((output / "history").glob("markdown-codex-*/payload"))


def test_reused_generation_still_requires_directory_durability(tmp_path, monkeypatch):
    output = tmp_path / "review"
    path = preserve_history(output, DATA)
    before = generation(path)
    observer = HistoryLookup(output, output)
    observer.paths({})

    def interrupted(*_args):
        raise OSError(errno.EIO, "synthetic reused history durability failure")

    monkeypatch.setattr(capture_store, "_flush_directory", interrupted)
    with history_transaction(observer), pytest.raises(OSError, match="durability failure"):
        preserve_history(output, DATA)
    assert generation(path) == before
    observer.verify()


def test_new_generation_retains_complete_prior_history(tmp_path):
    output = tmp_path / "review"
    first = preserve_history(output, DATA)
    before = generation(first)
    changed = DATA.replace(b"exhausted", b"independently validated")
    second = preserve_history(output, changed)
    assert first != second
    assert first.read_bytes() == DATA
    assert second.read_bytes() == changed
    assert generation(first) == before
    assert set(first.parent.iterdir()) == {first, second}


def test_history_durability_is_independent_of_capture_import_scope(tmp_path):
    output = tmp_path / "review"
    with capture_store.durable_links():
        path = preserve_history(output, DATA)
    assert path.read_bytes() == DATA
    assert list(path.parent.iterdir()) == [path]
