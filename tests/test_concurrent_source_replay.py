"""Independent article replay stays complete, isolated, deterministic and bounded."""

import json
import threading
import time
from collections import Counter
from copy import deepcopy

import pytest

from html_to_markdown import source_review_jobs as jobs
from html_to_markdown.curation import json_bytes
from html_to_markdown.enrichment.analysis import sha


def prepared(tmp_path, count=12):
    output = tmp_path / "review"
    output.mkdir()
    inventory = []
    for i in range(count):
        body = f"# Article {i}\n\nKeep port {8443 + i}.\n"
        relative = f"content/docs-cloud-f5-com/fixture-{i}/index.md"
        path = output / "reviewed" / relative
        path.parent.mkdir(parents=True)
        path.write_text(body)
        record = {
            "disposition": "accepted",
            "binding": {"request_context_sha256": "a" * 64},
            "item": {"identity": str(i), "media": []},
            "file_hashes": {"articles/" + relative: sha(body)},
        }
        name = f"documents/{i}.json"
        saved = output / name
        saved.parent.mkdir(exist_ok=True)
        saved.write_bytes(json_bytes(record))
        inventory.append({"path": name})
    (output / "review-inventory.json").write_bytes(json_bytes({"documents": inventory}))
    return output


def test_independent_replay_progress_is_bounded_to_four_jobs(tmp_path, monkeypatch):
    output = prepared(tmp_path)
    guard = threading.Lock()
    ready = threading.Event()
    calls = Counter()
    active = 0
    maximum = 0
    scratch_paths = set()

    def replay(capture, scratch, path, phase, client):
        nonlocal active, maximum
        assert phase == "replay" and client is None
        saved = json.loads(path.read_bytes())
        identity = saved["item"]["identity"]
        with guard:
            active += 1
            maximum = max(maximum, active)
            calls[identity] += 1
            assert scratch not in scratch_paths
            scratch_paths.add(scratch)
            if active == 4:
                ready.set()
        try:
            assert ready.wait(2), "Four independent article replays never progressed concurrently"
            time.sleep(0.02)
            return deepcopy(saved)
        finally:
            with guard:
                active -= 1

    monkeypatch.setattr(jobs, "review_one", replay)
    original = {
        p.relative_to(output).as_posix(): p.read_bytes() for p in output.rglob("*") if p.is_file()
    }
    result = jobs.verify_reviews(tmp_path / output.name, output)
    assert maximum == 4 and set(calls.values()) == {1} and len(calls) == 12
    assert len(result["accepted_files"]) == 12
    assert all((output / name).read_bytes() == data for name, data in original.items())
    assert not any(p.exists() for p in scratch_paths)


def test_concurrent_results_match_sequential_receipt_bytes(tmp_path, monkeypatch):
    output = prepared(tmp_path)
    monkeypatch.setattr(
        jobs,
        "review_one",
        lambda capture, scratch, path, phase, client: json.loads(path.read_bytes()),
    )
    result = jobs.verify_reviews(tmp_path, output)
    expected = {}
    for item in json.loads((output / "review-inventory.json").read_bytes())["documents"]:
        saved = json.loads((output / item["path"]).read_bytes())
        expected.update(
            {name.removeprefix("articles/"): pin for name, pin in saved["file_hashes"].items()}
        )
    assert json_bytes(result) == json_bytes(
        {
            "accepted_files": expected,
            "inventory_sha256": sha((output / "review-inventory.json").read_bytes()),
        }
    )


@pytest.mark.parametrize("failure", ["changed_replay", "changed_output"])
def test_failure_cannot_produce_an_accepted_receipt_or_repair_originals(
    tmp_path, monkeypatch, failure
):
    output = prepared(tmp_path)
    original = {
        p.relative_to(output).as_posix(): p.read_bytes() for p in output.rglob("*") if p.is_file()
    }

    def replay(capture, scratch, path, phase, client):
        saved = json.loads(path.read_bytes())
        if saved["item"]["identity"] == "3" and failure == "changed_replay":
            saved["binding"]["request_context_sha256"] = "b" * 64
        return saved

    monkeypatch.setattr(jobs, "review_one", replay)
    if failure == "changed_output":
        path = output / "reviewed/content/docs-cloud-f5-com/fixture-3/index.md"
        path.write_text(path.read_text().replace("8446", "80"))
        original[path.relative_to(output).as_posix()] = path.read_bytes()
    with pytest.raises(ValueError, match="differs|changed"):
        jobs.verify_reviews(tmp_path, output)
    assert all((output / name).read_bytes() == data for name, data in original.items())
