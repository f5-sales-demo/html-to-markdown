"""Immutable byte sharing cannot change captures, bypass guards or share mutable state."""

import errno
import fcntl
import io
import json
import os
import sqlite3
import tarfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from html_to_markdown import capture_store_maintenance as maintenance
from html_to_markdown.adapters.docs_cloud import DocsCloudAdapter
from html_to_markdown.capture_store import capture_digest, share_file, store_bytes
from html_to_markdown.capture_store_maintenance import share_capture_storage
from html_to_markdown.content_inventory import atomic_private, collect_content
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.fetcher import Fetcher
from html_to_markdown.models import DiscoveredPage, FetchResult
from html_to_markdown.source_review import binding_for
from html_to_markdown.work_queue import unpack_queue


def test_same_bytes_share_allocation_and_repeat_preserves_snapshot(tmp_path):
    data = b"complete captured HTML" * 1000
    store = tmp_path / "store"
    first, second = tmp_path / "one/capture", tmp_path / "two/capture"
    store_bytes(store, first, data)
    store_bytes(store, second, data)
    assert first.stat().st_ino == second.stat().st_ino
    before = first.stat()
    store_bytes(store, first, data)
    assert first.stat().st_ino == before.st_ino
    assert first.stat().st_mtime_ns == before.st_mtime_ns
    assert first.read_bytes() == second.read_bytes() == data
    changed = tmp_path / "third/capture"
    store_bytes(store, changed, b"changed captured HTML")
    assert changed.stat().st_ino != first.stat().st_ino
    assert first.read_bytes() == data


def test_existing_verified_files_seed_store_without_an_extra_payload_copy(tmp_path):
    store = tmp_path / "store"
    first, second = tmp_path / "one", tmp_path / "two"
    data = b"preserved bytes"
    atomic_private(first, data)
    atomic_private(second, data)
    old = first.stat().st_ino
    share_file(store, first, sha(data))
    share_file(store, second, sha(data))
    assert first.stat().st_ino == second.stat().st_ino == old
    assert first.read_bytes() == second.read_bytes() == data


def test_concurrent_writers_share_one_complete_object(tmp_path):
    store = tmp_path / "store"
    paths = [tmp_path / str(index) for index in range(8)]
    data = b"preserved media bytes" * 1000
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda path: store_bytes(store, path, data), paths))
    assert len({path.stat().st_ino for path in paths}) == 1
    assert all(path.read_bytes() == data for path in paths)


def test_cache_corruption_is_not_repaired_or_copied_into_capture(tmp_path):
    store = tmp_path / "store"
    first = tmp_path / "first"
    data = b"preserved bytes"
    store_bytes(store, first, data)
    cached = store / "objects" / sha(data)
    atomic_private(cached, b"tampered")
    second = tmp_path / "second"
    with pytest.raises(ValueError, match="digest"):
        store_bytes(store, second, data)
    assert not second.exists()
    assert cached.read_bytes() == b"tampered"
    assert first.read_bytes() == data


@pytest.mark.parametrize("target", ["store", "object", "destination", "lock"])
def test_symlinks_are_rejected_before_any_capture_is_changed(tmp_path, target):
    data = b"preserved bytes"
    digest = sha(data)
    store = tmp_path / "store"
    outside = tmp_path / "outside"
    atomic_private(outside, b"protected")
    destination = tmp_path / "capture"
    if target == "store":
        store.symlink_to(tmp_path, target_is_directory=True)
    else:
        store.mkdir(mode=0o700)
        (store / "objects").mkdir(mode=0o700)
        (store / "locks").mkdir(mode=0o700)
        if target == "object":
            (store / "objects" / digest).symlink_to(outside)
        elif target == "lock":
            (store / "locks" / digest).symlink_to(outside)
        else:
            destination.symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        store_bytes(store, destination, data)
    assert outside.read_bytes() == b"protected"


def test_expected_digest_mismatch_preserves_existing_capture(tmp_path):
    capture = tmp_path / "capture"
    atomic_private(capture, b"original")
    with pytest.raises(ValueError, match="digest"):
        share_file(tmp_path / "store", capture, sha(b"different"))
    assert capture.read_bytes() == b"original"


def test_cross_device_links_keep_verified_copy_fallback(tmp_path, monkeypatch):
    original = os.link

    def cross_device(source, destination):
        raise OSError(errno.EXDEV, "cross-device")

    monkeypatch.setattr("html_to_markdown.capture_store.os.link", cross_device)
    first, second = tmp_path / "first", tmp_path / "second"
    store_bytes(tmp_path / "store", first, b"complete")
    store_bytes(tmp_path / "store", second, b"complete")
    assert first.read_bytes() == second.read_bytes() == b"complete"
    assert first.stat().st_ino != second.stat().st_ino
    monkeypatch.setattr("html_to_markdown.capture_store.os.link", original)


@pytest.mark.parametrize(
    "name",
    [
        "inventory.json",
        "capture-binding.json",
        "journal/" + "a" * 64 + ".json",
        "responses/" + "a" * 64 + ".json",
    ],
)
def test_mutable_metadata_and_review_evidence_are_not_capture_payloads(name):
    assert capture_digest(Path(name)) is None


def test_mac_capture_transfers_share_only_digest_bound_raw_payloads(tmp_path):
    raw = b"complete captured source"
    digest = sha(raw)
    names = {
        "captures/" + digest + ".html": raw,
        "responses/" + digest + ".body": raw,
        "articles/content/my-f5-com/K000123456/assets/" + digest: raw,
        "inventory.json": b'{"documents":[]}',
    }
    archive = tmp_path / "capture.tar"
    with tarfile.open(archive, "w") as stream:
        for name, data in names.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            stream.addfile(member, io.BytesIO(data))
    first, second = tmp_path / "one", tmp_path / "two"
    for directory in [first, second]:
        unpack_queue(archive, directory, capture_store=tmp_path / "store")
    for name, data in names.items():
        assert (first / name).read_bytes() == (second / name).read_bytes() == data
        assert ((first / name).stat().st_ino == (second / name).stat().st_ino) == (
            name != "inventory.json"
        )
    atomic_private(second / "inventory.json", b"updated inventory")
    assert (first / "inventory.json").read_bytes() == b'{"documents":[]}'


def test_bad_raw_digest_cannot_enter_the_shared_store(tmp_path):
    archive = tmp_path / "bad.tar"
    with tarfile.open(archive, "w") as stream:
        member = tarfile.TarInfo("captures/" + sha(b"expected") + ".html")
        member.size = 3
        stream.addfile(member, io.BytesIO(b"bad"))
    with pytest.raises(ValueError, match="digest"):
        unpack_queue(archive, tmp_path / "received", capture_store=tmp_path / "store")
    assert not (tmp_path / "received" / member.name).exists()


@pytest.mark.asyncio
async def test_two_collections_reuse_html_extraction_and_media_without_rebinding_reviews(
    tmp_path, monkeypatch
):

    source = "docs-cloud-f5-com"
    url = "https://docs.cloud.f5.com/docs-v2/platform/how-to/current"
    html = '<main><h1>Current</h1><p>Use a 30 second timeout.</p><img src="/image.png" alt="Diagram"/></main>'

    async def discover(self, fetcher):
        return [DiscoveredPage(source_id=source, url=url)]

    async def fetch(self, adapter, target):
        return FetchResult(url=target, final_url=target, status_code=200, html=html)

    async def asset(self, target):
        return b"complete immutable media", "image/png"

    monkeypatch.setattr(DocsCloudAdapter, "discover", discover)
    monkeypatch.setattr(Fetcher, "fetch", fetch)
    monkeypatch.setattr(Fetcher, "fetch_asset", asset)
    first, second = tmp_path / "run-one", tmp_path / "run-two"
    one = await collect_content(first, source=source, capture_store=tmp_path / "store")
    two = await collect_content(second, source=source, capture_store=tmp_path / "store")
    a, b = one["documents"][0], two["documents"][0]
    assert a["article_sha256"] == b["article_sha256"]
    assert a["source_html_sha256"] == b["source_html_sha256"]
    names = [a["article_path"], a["source_html_path"], a["media"][0]["path"]]
    assert all((first / name).read_bytes() == (second / name).read_bytes() for name in names)
    assert all((first / name).stat().st_ino == (second / name).stat().st_ino for name in names)
    assert binding_for(a) == binding_for(b)
    assert a["disposition"] == b["disposition"] == "queued"
    assert not one["completeness"]["review"] and not two["completeness"]["publication"]


def _maintenance_queue(tmp_path):
    root = tmp_path / "queue"
    data = b"complete captured source"
    name = "captures/" + sha(data) + ".html"
    for run in ["one", "two"]:
        directory = root / run / "my-f5-com"
        atomic_private(directory / name, data)
        connection = sqlite3.connect(directory / "inventory.sqlite")
        connection.execute("CREATE TABLE documents(data TEXT)")
        connection.execute(
            "INSERT INTO documents VALUES (?)",
            (
                json.dumps(
                    {
                        "source_html_path": name,
                        "source_html_sha256": sha(data),
                        "captured_at": "2026-11-16T00:00:00Z",
                    }
                ),
            ),
        )
        connection.commit()
        connection.close()
    return root, name


def test_maintenance_is_resumable_and_preserves_source_records(tmp_path, monkeypatch):
    root, name = _maintenance_queue(tmp_path)
    originals = {path: path.read_bytes() for path in root.glob("*/*/inventory.sqlite")}
    output = tmp_path / "reports"
    result = share_capture_storage(root, output, source="my-f5-com")
    assert result["sources"][0]["state"] == "complete"
    assert result["sources"][0]["processed_files"] == 2
    assert (root / "one/my-f5-com" / name).stat().st_ino == (
        root / "two/my-f5-com" / name
    ).stat().st_ino
    assert all(path.read_bytes() == data for path, data in originals.items())
    monkeypatch.setattr(
        "html_to_markdown.capture_store_maintenance.share_file",
        lambda *args: pytest.fail("settled maintenance should not rewrite captures"),
    )
    repeated = share_capture_storage(root, output, source="my-f5-com")
    assert repeated == result


def test_maintenance_defers_a_live_collector_and_never_touches_mutable_inventory(tmp_path):
    root, _ = _maintenance_queue(tmp_path)
    with (root / "my-f5-com.collector.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = share_capture_storage(root, tmp_path / "reports", source="my-f5-com")
    assert result["sources"] == [{"source": "my-f5-com", "state": "deferred_active_collector"}]
    assert not (root / ".capture-store").exists()


def test_maintenance_cannot_store_mutable_metadata_mislabelled_as_an_article(tmp_path):
    root, _ = _maintenance_queue(tmp_path)
    directory = root / "one/my-f5-com"
    atomic_private(directory / "inventory.json", b"mutable inventory")
    connection = sqlite3.connect(directory / "inventory.sqlite")
    connection.execute(
        "UPDATE documents SET data=?",
        (
            json.dumps(
                {"article_path": "inventory.json", "article_sha256": sha(b"mutable inventory")}
            ),
        ),
    )
    connection.commit()
    connection.close()
    with pytest.raises(ValueError, match="immutable payload"):
        share_capture_storage(root, tmp_path / "reports", source="my-f5-com")
    assert (directory / "inventory.json").read_bytes() == b"mutable inventory"


def test_interrupted_maintenance_keeps_completed_cursor_and_resumes(tmp_path, monkeypatch):

    root, name = _maintenance_queue(tmp_path)
    original = maintenance.share_file
    calls = []

    def interrupted(store, path, expected):
        calls.append(path)
        if len(calls) == 2:
            raise OSError("interrupted checkpoint")
        return original(store, path, expected)

    monkeypatch.setattr(maintenance, "share_file", interrupted)
    output = tmp_path / "reports"
    with pytest.raises(OSError, match="interrupted"):
        maintenance.share_capture_storage(root, output, source="my-f5-com")
    checkpoint = json.loads((output / "my-f5-com.private.json").read_bytes())
    assert checkpoint["processed_files"] == 1 and checkpoint["state"] == "interrupted"
    monkeypatch.setattr(maintenance, "share_file", original)
    resumed = maintenance.share_capture_storage(root, output, source="my-f5-com")
    assert resumed["sources"][0]["processed_files"] == 2
    assert resumed["sources"][0]["state"] == "complete"
    assert (root / "one/my-f5-com" / name).read_bytes() == (
        root / "two/my-f5-com" / name
    ).read_bytes()
