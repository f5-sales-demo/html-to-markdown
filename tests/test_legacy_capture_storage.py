"""Historical receipt addresses must retain bytes and cannot admit arbitrary mutable paths."""

import io
import json
import sqlite3
import tarfile

import pytest

from html_to_markdown.capture_payloads import capture_payloads
from html_to_markdown.capture_store_maintenance import share_capture_storage
from html_to_markdown.content_inventory import atomic_private, verify_capture
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.work_queue import export_queue, unpack_queue


def legacy_queue(tmp_path):
    source = "docs-cloud-f5-com"
    url = "https://docs.cloud.f5.com/docs-v2/platform/how-to/current"
    html, article, image = (
        b"complete historical HTML",
        b"complete historical article",
        b"original media",
    )
    parent = "articles/content/" + source + "/platform/how-to/current"
    item = {
        "sourceId": source,
        "url": url,
        "identity": source + ":platform/how-to/current",
        "source_html_path": "captures/" + sha(url) + ".html",
        "source_html_sha256": sha(html),
        "article_path": parent + "/index.md",
        "article_sha256": sha(article),
        "media": [{"path": parent + "/assets/" + sha(image), "sha256": sha(image)}],
        "captured_at": "2026-11-10T00:00:00Z",
        "capture_complete": True,
    }
    payloads = {
        item["source_html_path"]: html,
        item["article_path"]: article,
        item["media"][0]["path"]: image,
    }
    root = tmp_path / "queue"
    for name in ["one", "two"]:
        directory = root / name / source
        for path, data in payloads.items():
            atomic_private(directory / path, data)
        atomic_private(
            directory / "capture-binding.json",
            json.dumps({"request": {"source": source}, "started_at": item["captured_at"]}).encode(),
        )
        with sqlite3.connect(directory / "inventory.sqlite") as database:
            database.executescript(
                "CREATE TABLE documents(url TEXT,data TEXT);"
                "CREATE TABLE phases(source TEXT,data TEXT);"
            )
            database.execute("INSERT INTO documents VALUES (?,?)", (url, json.dumps(item)))
            database.execute(
                "INSERT INTO phases VALUES (?,?)", (source, '{"enumeration_complete":false}')
            )
    return root, source, item, payloads


def test_legacy_maintenance_and_real_export_share_receipt_bound_bytes(tmp_path):
    root, source, item, payloads = legacy_queue(tmp_path)
    originals = {path: path.read_bytes() for path in root.glob("*/*/inventory.sqlite")}
    report = share_capture_storage(root, tmp_path / "maintenance", source=source)
    assert report["sources"][0]["state"] == "complete"
    assert report["sources"][0]["processed_files"] == 6
    for name, data in payloads.items():
        one, two = root / "one" / source / name, root / "two" / source / name
        assert one.read_bytes() == two.read_bytes() == data
        assert one.stat().st_ino == two.stat().st_ino
    archive = tmp_path / "export.tar.gz"
    with archive.open("wb") as stream:
        export_queue(root, "one/" + source, stream)
    destinations = [tmp_path / "received-one", tmp_path / "received-two"]
    for destination in destinations:
        unpack_queue(archive, destination, capture_store=tmp_path / "mac-store")
        verify_capture(destination, item)
    for name, data in payloads.items():
        one, two = (directory / name for directory in destinations)
        assert one.read_bytes() == two.read_bytes() == data
        assert one.stat().st_ino == two.stat().st_ino
    one, two = (directory / "inventory.json" for directory in destinations)
    assert one.read_bytes() == two.read_bytes()
    assert one.stat().st_ino != two.stat().st_ino
    assert {path: path.read_bytes() for path in originals} == originals


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_html_path", "captures/" + sha("another-url") + ".html"),
        ("article_path", "articles/content/docs-cloud-f5-com/another/index.md"),
        ("article_path", "reviews/index.md"),
        ("identity", "docs-cloud-f5-com:another"),
        ("url", "https://example.com/unrelated"),
    ],
)
def test_legacy_receipts_cannot_address_forged_paths(tmp_path, field, value):
    _, _, item, _ = legacy_queue(tmp_path)
    item[field] = value
    with pytest.raises(ValueError):
        capture_payloads(item)


@pytest.mark.parametrize("corruption", ["bytes", "missing", "conflict"])
def test_legacy_transfer_rejects_bad_evidence_before_writing_any_member(tmp_path, corruption):
    _, _, item, payloads = legacy_queue(tmp_path)
    documents = [item]
    if corruption == "bytes":
        payloads[item["source_html_path"]] = b"different bytes"
    elif corruption == "missing":
        del payloads[item["source_html_path"]]
    else:
        documents.append({**item, "source_html_sha256": sha(b"different bytes")})
    payloads["inventory.json"] = json.dumps({"documents": documents}).encode()
    archive = tmp_path / "corrupt.tar"
    with tarfile.open(archive, "w") as stream:
        for name, data in sorted(payloads.items()):
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            stream.addfile(entry, io.BytesIO(data))
    destination = tmp_path / "received"
    with pytest.raises(ValueError):
        unpack_queue(archive, destination, capture_store=tmp_path / "store")
    assert not destination.exists()
    assert not (tmp_path / "store").exists()


def test_enumeration_failure_preserves_checkpoint_and_resumes_after_receipt_repair(tmp_path):
    root, source, item, _ = legacy_queue(tmp_path)
    output = tmp_path / "maintenance"
    completed = share_capture_storage(root, output, source=source)
    checkpoint = output / (source + ".private.json")
    original = checkpoint.read_bytes()
    database_path = root / "one" / source / "inventory.sqlite"
    with sqlite3.connect(database_path) as database:
        invalid = {**item, "source_html_path": "reviews/current.json"}
        database.execute("UPDATE documents SET data=?", (json.dumps(invalid),))
    with pytest.raises(ValueError):
        share_capture_storage(root, output, source=source)
    interrupted = json.loads(checkpoint.read_bytes())
    assert interrupted["state"] == "interrupted" and interrupted["stage"] == "enumeration"
    assert (output / "history" / (sha(original) + ".json")).read_bytes() == original
    assert (
        json.loads((output / "errors" / (source + ".private.json")).read_bytes())["stage"]
        == "enumeration"
    )
    with sqlite3.connect(database_path) as database:
        assert json.loads(database.execute("SELECT data FROM documents").fetchone()[0]) == invalid
        database.execute("UPDATE documents SET data=?", (json.dumps(item),))
    resumed = share_capture_storage(root, output, source=source)
    assert resumed["sources"][0]["state"] == completed["sources"][0]["state"] == "complete"
    assert resumed["sources"][0]["processed_files"] == 6
