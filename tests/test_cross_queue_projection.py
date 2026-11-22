"""Supported logical evidence wrappers retain their exact daily checkpoints."""

import copy
from pathlib import Path
from typing import Any

import pytest
from test_source_input_projection import Client as ProjectionClient
from test_source_input_projection import logical_request
from test_source_native_boundaries import Client as NativeClient
from test_source_native_boundaries import Probe, image_content
from test_source_native_boundaries import body as native_body

import html_to_markdown.enrichment.native_visuals as native
import html_to_markdown.source_boundary_requests as boundaries
from html_to_markdown.cross_queue_reviews import _snapshot_boundaries
from html_to_markdown.curation import json_bytes
from html_to_markdown.enrichment.contracts import EDITOR_MODEL, ResponseEvidence
from html_to_markdown.enrichment.gates import request_hash
from html_to_markdown.source_boundary_review import execute_source_request, parse_source_response
from html_to_markdown.source_review import PrivacyDecision


class CompleteClient(NativeClient):
    def execute(self, body: dict[str, Any]) -> ResponseEvidence:
        if body["text"]["format"]["name"] == "privacydecision":
            self.calls.append(body)
            return ProjectionClient().execute(body)
        return super().execute(body)


@pytest.fixture(params=["direct", "projected", "projected_native"])
def checkpoint(request, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    kind = request.param
    monkeypatch.setattr(native, "MAX_DECODED_BYTES", 600)
    monkeypatch.setattr(boundaries, "MAX_DECODED_BYTES", 600)
    logical = native_body() if kind == "direct" else logical_request()
    if kind == "projected_native":
        logical["input"][0]["content"].extend(image_content(index) for index in range(4))
    worker = tmp_path / "worker"
    old = worker / "day-1/www-f5-com/review"
    destination = worker / "day-2/www-f5-com/review"
    client = CompleteClient()
    result = execute_source_request(
        logical, client, old / "native-boundaries" / request_hash(logical)
    )
    previous = {
        "phases": {"privacy": result.model_dump(mode="json")},
        "privacy_feedback": ["first independent rejection", "second independent rejection"],
        "privacy_exhausted": True,
    }
    prior_path = old / "documents/article.json"
    prior_path.parent.mkdir(parents=True)
    prior_path.write_bytes(json_bytes(previous))
    schema = Probe if kind == "direct" else PrivacyDecision
    return worker, old, destination, prior_path, previous, logical, result, schema, client


def test_supported_proofs_copy_complete_checkpoints_and_replay_without_inference(checkpoint):
    worker, old, destination, prior_path, previous, logical, result, schema, client = checkpoint
    original_record = prior_path.read_bytes()
    originals = {
        path.relative_to(old): path.read_bytes()
        for path in (old / "native-boundaries").rglob("*")
        if path.is_file()
    }
    calls = len(client.calls)
    _snapshot_boundaries(worker, destination, prior_path, previous)
    assert originals
    assert all((destination / name).read_bytes() == raw for name, raw in originals.items())
    assert all((old / name).read_bytes() == raw for name, raw in originals.items())
    assert prior_path.read_bytes() == original_record
    assert previous["privacy_exhausted"] and len(previous["privacy_feedback"]) == 2
    assert not (destination / "documents").exists()
    replayed = parse_source_response(result, schema, EDITOR_MODEL, request_hash(logical))
    assert replayed.identity
    assert len(client.calls) == calls
    _snapshot_boundaries(worker, destination, prior_path, previous)
    assert all((destination / name).read_bytes() == raw for name, raw in originals.items())


@pytest.mark.parametrize(
    "change", ["actual_request", "logical_request", "unknown_version", "malformed"]
)
def test_changed_or_unknown_source_proofs_do_not_grant_replay(checkpoint, change):
    worker, old, destination, prior_path, previous, logical, result, schema, _ = checkpoint
    changed = copy.deepcopy(result.model_dump(mode="json"))
    if change == "actual_request":
        changed["request"]["input"][0]["content"][0]["text"] += "changed source or media"
    elif change == "logical_request":
        changed["source_boundary"]["logical_request"]["model"] = "unknown-model"
    elif change == "unknown_version":
        changed["source_boundary"]["version"] = "unknown-proof/1"
    else:
        changed["source_boundary"].pop("logical_request")
    evidence = ResponseEvidence.model_validate(changed)
    with pytest.raises(ValueError):
        parse_source_response(evidence, schema, EDITOR_MODEL, request_hash(logical))
    if change in {"unknown_version", "malformed"}:
        previous["phases"]["privacy"] = changed
        with pytest.raises(ValueError):
            _snapshot_boundaries(worker, destination, prior_path, previous)
        assert not destination.exists()
    assert prior_path.is_file() and (old / "native-boundaries").is_dir()


def test_pending_native_part_checkpoint_is_preserved(tmp_path: Path):
    worker = tmp_path / "worker"
    old = worker / "day-1/www-f5-com/review"
    destination = worker / "day-2/www-f5-com/review"
    digest = "a" * 64
    cache = old / "native-boundaries" / digest / "0/analysis.json"
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b'{"private_failed_attempt":"preserved"}\n')
    previous = {"boundary_pending": {"relevance": {"logical_request_sha256": digest}}}
    _snapshot_boundaries(worker, destination, old / "documents/article.json", previous)
    assert (destination / cache.relative_to(old)).read_bytes() == cache.read_bytes()


def test_source_checkpoint_symlinks_remain_rejected(checkpoint, tmp_path: Path):
    worker, old, destination, prior_path, previous, logical, _, _, _ = checkpoint
    directory = old / "native-boundaries" / request_hash(logical)
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"private outside data")
    (directory / "linked.json").symlink_to(outside)
    with pytest.raises(ValueError, match="link"):
        _snapshot_boundaries(worker, destination, prior_path, previous)
    assert outside.read_bytes() == b"private outside data"
    assert not (destination / directory.relative_to(old) / "linked.json").exists()
