"""Prerequisite scheduling preserves admission gates and source evidence."""

import json
import shutil
from pathlib import Path

import pytest
from test_source_review_jobs import LocalClient
from test_worker_admission_roundtrip import _source_capture
from typer.testing import CliRunner

import html_to_markdown.source_review_jobs as jobs
from html_to_markdown import cli
from html_to_markdown.cross_queue_reviews import _eligible
from html_to_markdown.curation import json_bytes
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.review_priorities import ReviewPriorities, load_priorities, select_records


def records(root: Path) -> list[Path]:
    root.mkdir()
    entries = []
    paths = []
    for index, (identity, published) in enumerate(
        [
            ("my-f5-com:K000100", True),
            ("community-f5-com:t/77234", False),
            ("my-f5-com:K000200", False),
        ]
    ):
        source, value = identity.split(":", 1)
        url = (
            "https://community.f5.com/t/77234"
            if source == "community-f5-com"
            else "https://my.f5.com/manage/s/article/" + value
        )
        path = root / f"document-{index}.json"
        path.write_bytes(
            json_bytes(
                {
                    "item": {
                        "identity": identity,
                        "url": url,
                        "origins": ["previous_publication"] if published else [],
                    },
                    "binding_sha256": "a" * 64,
                    "disposition": "queued",
                    "phases": {},
                }
            )
        )
        entries.append({"path": path.name})
        paths.append(path)
    (root / "review-inventory.json").write_bytes(json_bytes({"documents": entries}))
    return paths


def test_prerequisite_precedes_published_without_changing_source_records(tmp_path):
    output = tmp_path / "review"
    paths = records(output)
    original = {path: path.read_bytes() for path in paths}
    priority = ReviewPriorities(identities=frozenset({"community-f5-com:t/77234"}))
    assert select_records(output, 2) == [paths[0], paths[1]]
    assert select_records(output, 2, priority) == [paths[1], paths[0]]
    assert _eligible(output, 2, priorities=priority) == select_records(output, 2, priority)
    assert {path: path.read_bytes() for path in paths} == original


def test_unknown_priorities_do_not_create_or_exclude_candidates(tmp_path):
    output = tmp_path / "review"
    paths = records(output)
    priority = ReviewPriorities(urls=frozenset({"https://my.f5.com/manage/s/article/K999999"}))
    assert select_records(output, 20, priority) == [paths[0], paths[1], paths[2]]


def test_priority_cannot_reopen_accepted_excluded_or_exhausted_records(tmp_path):
    output = tmp_path / "review"
    paths = records(output)
    for path, disposition in zip(paths, ["accepted", "policy_excluded", "queued"], strict=True):
        saved = json.loads(path.read_bytes())
        saved.update(
            disposition=disposition,
            attempted_binding_sha256=saved["binding_sha256"],
            privacy_feedback=["first", "second", "third"],
            privacy_editor_allowance=0,
        )
        path.write_bytes(json_bytes(saved))
    before = {path: path.read_bytes() for path in paths}
    priority = ReviewPriorities(
        identities=frozenset(json.loads(path.read_bytes())["item"]["identity"] for path in paths)
    )
    assert not select_records(output, 8, priority)
    assert {path: path.read_bytes() for path in paths} == before


def test_unselected_candidates_advance_after_priority_attempt(tmp_path):
    output = tmp_path / "review"
    paths = records(output)
    priority = ReviewPriorities(urls=frozenset({"https://community.f5.com/t/77234"}))
    assert select_records(output, 1, priority) == [paths[1]]
    saved = json.loads(paths[1].read_bytes())
    saved["attempted_binding_sha256"] = saved["binding_sha256"]
    paths[1].write_bytes(json_bytes(saved))
    assert select_records(output, 2, priority) == [paths[0], paths[2]]


def test_pending_validation_keeps_zero_editor_history_when_scheduled(tmp_path):
    output = tmp_path / "review"
    paths = records(output)
    saved = json.loads(paths[1].read_bytes())
    saved.update(
        privacy_feedback=["first", "second", "third"],
        privacy_editor_allowance=0,
        privacy_proposal_pending_validation={"request_sha256": "b" * 64},
    )
    paths[1].write_bytes(json_bytes(saved))
    before = paths[1].read_bytes()
    assert select_records(
        output, 1, ReviewPriorities(identities=frozenset({saved["item"]["identity"]}))
    ) == [paths[1]]
    assert paths[1].read_bytes() == before


def test_valid_configuration_is_pinned_and_missing_configuration_preserves_defaults(tmp_path):
    path = tmp_path / "priorities.json"
    assert load_priorities(path) == ReviewPriorities()
    data = json_bytes(
        {
            "schema_version": 1,
            "identities": ["community-f5-com:t/77234"],
            "urls": ["https://docs.cloud.f5.com/docs-v2/platform/concepts/core-concepts"],
        }
    )
    path.write_bytes(data)
    priorities = load_priorities(path, expected_sha256=sha(data))
    assert priorities.sha256 == sha(data)
    assert priorities.identities == frozenset({"community-f5-com:t/77234"})
    with pytest.raises(ValueError, match="digest"):
        load_priorities(path, expected_sha256="0" * 64)


@pytest.mark.parametrize(
    "data",
    [
        b'{"schema_version":1,"schema_version":1,"identities":[],"urls":[]}',
        b'{"schema_version":true,"identities":[],"urls":[]}',
        b'{"schema_version":1,"identities":[],"urls":[],"approval":true}',
        json_bytes({"schema_version": 1, "identities": ["unknown:article"], "urls": []}),
        json_bytes(
            {
                "schema_version": 1,
                "identities": ["community-f5-com:t/1", "community-f5-com:t/1"],
                "urls": [],
            }
        ),
        *[
            json_bytes({"schema_version": 1, "identities": [], "urls": [url]})
            for url in [
                "http://community.f5.com/t/1",
                "https://other.example/article",
                "https://user:secret@my.f5.com/manage/s/article/K123",
                "https://my.f5.com/manage/s/article/K123?token=secret",
                "https://www.f5.com/../private",
                "https://www.f5.com/%0asecret",
            ]
        ],
    ],
)
def test_invalid_configuration_cannot_become_scheduling_or_model_data(tmp_path, data):
    path = tmp_path / "priorities.json"
    path.write_bytes(data)
    with pytest.raises(ValueError):
        load_priorities(path)


def test_symlink_and_oversized_configuration_are_rejected(tmp_path):
    original = tmp_path / "original"
    original.write_text("{}")
    link = tmp_path / "link"
    link.symlink_to(original)
    with pytest.raises(ValueError, match="link"):
        load_priorities(link)
    original.write_bytes(b" " * (65536 + 1))
    with pytest.raises(ValueError, match="size"):
        load_priorities(original)


def _joined_capture(capture, tmp_path):
    first = _source_capture(
        capture, tmp_path / "first", "my-f5-com", "https://my.f5.com/manage/s/article/K000100"
    )
    second = _source_capture(
        capture, tmp_path / "second", "my-f5-com", "https://my.f5.com/manage/s/article/K000200"
    )
    combined = tmp_path / "combined"
    shutil.copytree(first, combined)
    before = json.loads((first / "inventory.json").read_bytes())
    after = json.loads((second / "inventory.json").read_bytes())
    item = after["documents"][0]
    target = combined / item["article_path"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes((second / item["article_path"]).read_bytes())
    before["documents"].extend(after["documents"])
    (combined / "inventory.json").write_bytes(json_bytes(before))
    return combined, before["documents"]


def test_real_admission_and_reuse_follow_priority_without_rebinding_requests(
    capture, tmp_path, monkeypatch
):
    combined, items = _joined_capture(capture, tmp_path)
    item = items[1]
    source_bytes = (combined / "inventory.json").read_bytes()
    requests = []

    class CountingClient(LocalClient):
        def execute(self, body):
            requests.append(body)
            return super().execute(body)

    monkeypatch.setattr(jobs, "CodexClient", CountingClient)
    output = tmp_path / "review"
    jobs.prepare_reviews(combined, output)
    priority = ReviewPriorities(urls=frozenset({item["url"]}))
    assert _eligible(output, 1, priorities=priority) == select_records(output, 1, priority)
    result = jobs.run_reviews(
        combined, output, phase="all", journal=tmp_path / "journal", limit=1, priorities=priority
    )
    assert len(requests) == 3
    assert {d["url"] for d in result["documents"] if d["disposition"] == "accepted"} == {
        item["url"]
    }
    approved_path = output / "documents" / (sha(item["identity"]) + ".json")
    approved_bytes = approved_path.read_bytes()
    other_priority = ReviewPriorities(urls=frozenset({items[0]["url"]}))
    result = jobs.run_reviews(
        combined,
        output,
        phase="all",
        journal=tmp_path / "journal",
        limit=1,
        priorities=other_priority,
    )
    assert len(requests) == 6 and not result["exceptions"]
    assert len(jobs.verify_reviews(combined, output)["accepted_files"]) == 2
    assert approved_path.read_bytes() == approved_bytes
    assert (combined / "inventory.json").read_bytes() == source_bytes


def test_cli_requires_priority_digest_and_passes_only_scheduling_options(tmp_path, monkeypatch):
    priority_file = tmp_path / "priorities.json"
    data = json_bytes({"schema_version": 1, "identities": ["community-f5-com:t/77234"], "urls": []})
    priority_file.write_bytes(data)
    calls = []
    monkeypatch.setattr(
        cli,
        "run_reviews",
        lambda *args, **kwargs: calls.append(kwargs) or {"documents": [], "exceptions": {}},
    )
    args = [
        "review-content",
        "--capture",
        str(tmp_path),
        "--output",
        str(tmp_path / "output"),
        "--limit",
        "1",
        "--priority-input",
        str(priority_file),
    ]
    runner = CliRunner()
    assert runner.invoke(cli.app, args).exit_code != 0
    assert not calls
    result = runner.invoke(cli.app, args + ["--priority-sha256", sha(data)])
    assert result.exit_code == 0, result.output
    assert calls[0]["phase"] == "prepare" and "priorities" not in calls[0]
    assert calls[1]["priorities"].sha256 == sha(data)
