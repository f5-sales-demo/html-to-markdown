"""Private queue snapshots are complete, safe to unpack, and independent of review."""

import io
import json
import sqlite3
import tarfile
from pathlib import Path

import pytest

from html_to_markdown.curation import json_bytes
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.work_queue import export_queue, list_queues, pack_private_tree, unpack_queue


def test_partial_collector_exports_completed_records_atomically(tmp_path: Path) -> None:
    directory = tmp_path / "queues/run-1/my-f5-com"
    directory.mkdir(parents=True)
    (directory / "capture-binding.json").write_bytes(
        json_bytes({"started_at": "2026-11-10T00:00:00Z", "request": {"source": "my-f5-com"}})
    )
    source = b"complete captured article"
    (directory / "source.html").write_bytes(source)
    connection = sqlite3.connect(directory / "inventory.sqlite")
    connection.executescript(
        "CREATE TABLE documents(url TEXT,data TEXT);CREATE TABLE phases(source TEXT,data TEXT);"
    )
    complete = {
        "identity": "my-f5-com:K000123456",
        "url": "https://my.f5.com/manage/s/article/K000123456",
        "capture_complete": True,
        "source_html_path": "source.html",
        "source_html_sha256": sha(source),
        "disposition": "queued",
    }
    pending = {
        "identity": "my-f5-com:K000123457",
        "url": "https://my.f5.com/manage/s/article/K000123457",
        "capture_complete": False,
        "disposition": "queued",
    }
    connection.executemany(
        "INSERT INTO documents VALUES (?,?)",
        [(item["url"], json.dumps(item)) for item in (complete, pending)],
    )
    connection.execute(
        "INSERT INTO phases VALUES (?,?)",
        ("my-f5-com", json.dumps({"enumeration_complete": False})),
    )
    connection.commit()
    connection.close()
    queues = list_queues(tmp_path / "queues")
    assert queues[0]["captured"] == 1 and queues[0]["discovered"] == 2
    stream = io.BytesIO()
    export_queue(tmp_path / "queues", "run-1/my-f5-com", stream)
    archive = tmp_path / "queue.tar.gz"
    archive.write_bytes(stream.getvalue())
    unpack_queue(archive, tmp_path / "received")
    inventory = json.loads((tmp_path / "received/inventory.json").read_bytes())
    assert not inventory["completeness"]["enumeration"]
    assert len(inventory["documents"]) == 2
    assert (tmp_path / "received/source.html").read_bytes() == source
    with pytest.raises(ValueError, match="identity"):
        export_queue(tmp_path / "queues", "../my-f5-com", io.BytesIO())


@pytest.mark.parametrize(
    "name,kind",
    [("../escape", tarfile.REGTYPE), ("/absolute", tarfile.REGTYPE), ("safe", tarfile.SYMTYPE)],
)
def test_private_transfer_rejects_unsafe_members_before_extracting(
    tmp_path: Path, name: str, kind: bytes
) -> None:
    archive = tmp_path / "input.tar"
    with tarfile.open(archive, "w") as output:
        member = tarfile.TarInfo(name)
        member.type = kind
        member.linkname = "/private/auth.json"
        output.addfile(member)
    with pytest.raises(ValueError, match="unsafe"):
        unpack_queue(archive, tmp_path / "received")
    assert not (tmp_path / "received").exists()


def test_reviewed_tree_transfer_keeps_private_files_and_paths_exact(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / ".admission").mkdir(parents=True)
    (source / ".admission/receipt.json").write_text('{"sha":"verified"}')
    stream = io.BytesIO()
    pack_private_tree(source, stream)
    archive = tmp_path / "transfer.tar.gz"
    archive.write_bytes(stream.getvalue())
    unpack_queue(archive, tmp_path / "received")
    assert (tmp_path / "received/.admission/receipt.json").read_bytes() == (
        source / ".admission/receipt.json"
    ).read_bytes()
