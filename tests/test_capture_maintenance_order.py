"""Path components and POSIX strings must not create different resume orders."""

import json
import sqlite3

import pytest

from html_to_markdown import capture_store_maintenance as maintenance
from html_to_markdown.content_inventory import atomic_private
from html_to_markdown.enrichment.analysis import sha


def prefix_queues(tmp_path):
    source = "my-f5-com"
    root = tmp_path / "queue"
    raw = b"complete immutable HTML"
    article = b"complete immutable article"
    item = {
        "identity": source + ":K000123456",
        "sourceId": source,
        "url": "https://my.f5.com/manage/s/article/K000123456",
        "source_html_path": "captures/" + sha(raw) + ".html",
        "source_html_sha256": sha(raw),
        "article_path": "articles/content/my-f5-com/K000123456/index.md",
        "article_sha256": sha(article),
        "captured_at": "2026-11-10T00:00:00Z",
    }
    payloads = {item["source_html_path"]: raw, item["article_path"]: article}
    for prefix in ["a", "a-b", "a.a", "aa"]:
        directory = root / prefix / source
        for path, data in payloads.items():
            atomic_private(directory / path, data)
        with sqlite3.connect(directory / "inventory.sqlite") as connection:
            connection.execute("CREATE TABLE documents(data TEXT)")
            connection.execute("INSERT INTO documents VALUES (?)", (json.dumps(item),))
    paths = sorted(
        (
            root / prefix / source / path
            for prefix in ["a", "a-b", "a.a", "aa"]
            for path in payloads
        ),
        key=lambda path: path.relative_to(root).as_posix(),
    )
    return root, source, paths


def test_first_run_visits_neighboring_prefixes_and_mixed_depth_paths(tmp_path, monkeypatch):
    root, source, paths = prefix_queues(tmp_path)
    original = maintenance.share_file
    visited = []

    def share(store, path, expected):
        visited.append(path)
        return original(store, path, expected)

    monkeypatch.setattr(maintenance, "share_file", share)
    result = maintenance.share_capture_storage(root, tmp_path / "report", source=source)["sources"][
        0
    ]
    assert visited == paths
    assert (
        result["state"] == "complete"
        and result["processed_files"] == result["available_files"] == 8
    )
    assert result["ordering"] == maintenance.ORDERING


def test_interrupted_prefix_order_resumes_without_skips_or_repeated_completed_paths(
    tmp_path, monkeypatch
):
    root, source, paths = prefix_queues(tmp_path)
    output = tmp_path / "report"
    original = maintenance.share_file
    completed = []

    def share(store, path, expected):
        if len(completed) == 3:
            raise OSError("synthetic interrupted sharing")
        result = original(store, path, expected)
        completed.append(path)
        return result

    monkeypatch.setattr(maintenance, "share_file", share)
    with pytest.raises(OSError):
        maintenance.share_capture_storage(root, output, source=source)
    state = json.loads((output / (source + ".private.json")).read_bytes())
    assert state["processed_files"] == 3 and completed == paths[:3]
    resumed = []

    def resume(store, path, expected):
        resumed.append(path)
        return original(store, path, expected)

    monkeypatch.setattr(maintenance, "share_file", resume)
    state = maintenance.share_capture_storage(root, output, source=source)["sources"][0]
    assert resumed == paths[3:]
    assert state["processed_files"] == state["available_files"] == 8
    resumed.clear()
    assert (
        maintenance.share_capture_storage(root, output, source=source)["sources"][0]["state"]
        == "complete"
    )
    assert not resumed


@pytest.mark.parametrize("ordering", [None, maintenance.ORDERING])
def test_incompatible_or_incomplete_complete_checkpoint_is_preserved_and_replayed(
    tmp_path, monkeypatch, ordering
):
    root, source, paths = prefix_queues(tmp_path)
    output = tmp_path / "report"
    maintenance.share_capture_storage(root, output, source=source)
    checkpoint = output / (source + ".private.json")
    state = json.loads(checkpoint.read_bytes())
    state.update(processed_files=2, last_file=paths[-1].relative_to(root).as_posix())
    if ordering is None:
        state.pop("ordering")
    raw = json.dumps(state, sort_keys=True).encode()
    atomic_private(checkpoint, raw)
    original_bytes = {path: path.read_bytes() for path in paths}
    records = {path: path.read_bytes() for path in root.glob("*/*/inventory.sqlite")}
    original = maintenance.share_file
    visited = []

    def share(store, path, expected):
        visited.append(path)
        return original(store, path, expected)

    monkeypatch.setattr(maintenance, "share_file", share)
    result = maintenance.share_capture_storage(root, output, source=source)["sources"][0]
    assert visited == paths
    assert (
        result["state"] == "complete"
        and result["processed_files"] == result["available_files"] == 8
    )
    assert (output / "history" / (sha(raw) + ".json")).read_bytes() == raw
    assert {path: path.read_bytes() for path in paths} == original_bytes
    assert {path: path.read_bytes() for path in records} == records
