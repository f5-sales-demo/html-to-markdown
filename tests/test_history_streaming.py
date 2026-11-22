"""Large immutable histories must not be materialized to discover source identity."""

import hashlib
import json
import tracemalloc

import pytest
from test_baseline_global_ledgers import cloned
from test_baseline_history_lookup import archive

from html_to_markdown import baseline_source_review as source
from html_to_markdown.baseline_history_lookup import HistoryLookup
from html_to_markdown.curation import json_bytes
from html_to_markdown.enrichment.analysis import sha

pytest_plugins = ["test_baseline_source_review"]


def history(tmp_path, data):
    output = tmp_path / "privacy"
    directory = output / "history"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (hashlib.sha256(data).hexdigest() + ".json")
    path.write_bytes(data)
    return output, path


def test_large_nested_history_discovery_uses_bounded_memory(tmp_path):
    output = tmp_path / "privacy"
    directory = output / "history"
    directory.mkdir(parents=True)
    path = directory / "building"
    binding = {"input_kind": "retained_verified_baseline", "baseline_article": "fixture"}
    payload = b'"' + b"x" * (32 * 1024) + b'"'
    with path.open("wb") as stream:
        stream.write(b'{"binding":' + json_bytes(binding) + b',"untrusted":[{"responses":[')
        for index in range(1024):
            if index:
                stream.write(b",")
            stream.write(payload)
        stream.write(b"]}]}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    target = path.with_name(digest.hexdigest() + ".json")
    path.rename(target)
    lookup = HistoryLookup(output, tmp_path / "worker")
    tracemalloc.start()
    try:
        assert lookup.paths(binding) == [target]
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 6 * 1024 * 1024, f"History discovery allocated {peak} bytes"
    lookup.verify()
    assert lookup.metrics["bytes_parsed"] == target.stat().st_size
    assert "xxxxxxxxxxxxxxxx" not in repr(vars(lookup))


@pytest.mark.parametrize(
    "document",
    [
        '{"binding":{"input_kind":"fixture","baseline_article":"\\u00e9/\\ud83d\\ude00"},"raw":{"a":[true,false,null,1,-2.5e4,{}]}}',
        '{"raw":[{"binding":"untrusted nested value"}],"binding":{"input_kind":"fixture"}}',
        '{"binding":null,"binding":{"input_kind":"fixture"}}',
        '{"binding":{"input_kind":[],"input_kind":"fixture"}}',
        '{"binding.input_kind":"untrusted dotted key","binding":{"input_kind":"fixture"}}',
        '{"untrusted":"\\ud800","binding":{"input_kind":"fixture"}}',
        '{"untrusted":[[],{"nested":{"binding":false}}]}',
    ],
)
def test_streamed_identity_matches_complete_standard_json(tmp_path, document):
    data = document.encode()
    output, path = history(tmp_path, data)
    wanted = json.loads(data).get("binding", {})
    lookup = HistoryLookup(output, tmp_path / "worker")
    assert lookup.paths(wanted) == [path]
    lookup.verify()


@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16", "utf-32"])
def test_existing_json_byte_encodings_remain_valid(tmp_path, encoding):
    wanted = {"input_kind": "fixture", "baseline_article": "\u00e9"}
    data = json.dumps({"binding": wanted}).encode(encoding)
    output, path = history(tmp_path, data)
    lookup = HistoryLookup(output, tmp_path / "worker")
    assert lookup.paths(wanted) == [path]
    lookup.verify()


@pytest.mark.parametrize(
    "data",
    [
        b'{"binding":{}} trailing',
        b'{"binding":{},"untrusted":[true,]}',
        b'{"binding":{},"untrusted":"control\x01"}',
        b'{"binding":null}',
        b'{"binding":[]}',
        b'{"binding":{"input_kind":42}}',
        b'{"binding":{"input_kind":{"nested":"invalid"}}}',
        b"[]",
        b'{"binding":{}} {}',
        b'{"binding":{},"untrusted":"\\q"}',
        b'{"binding":{},"untrusted":"\xff"}',
    ],
)
def test_invalid_complete_history_cannot_supply_identity(tmp_path, data):
    output, _ = history(tmp_path, data)
    with pytest.raises(ValueError):
        HistoryLookup(output, tmp_path / "worker").paths({})


def test_final_integrity_failure_retains_private_digest_bound_details(
    prepared, tmp_path, monkeypatch
):
    worker = tmp_path / "worker"
    output, record = cloned(prepared, worker, "current")
    archive(output, record)
    monkeypatch.setenv("HTML_TO_MARKDOWN_BASELINE_HISTORY_ROOT", str(worker))
    monkeypatch.setattr(
        source, "review_baseline_privacy", lambda *args: {"status": "privacy_only_validated"}
    )
    error = "exact private custody defect: identifying-fixture@example.com"

    def fail(_self):
        raise ValueError(error)

    monkeypatch.setattr(HistoryLookup, "verify", fail)
    inventory = {"documents": [{"identity": "fixture", "path": "documents/" + record.name}]}
    result = source.review_baseline_batch(output, inventory, worker / "journal", replay=True)
    assert result["history_integrity_verified"] is False
    assert result["exceptions"] == 1
    pin = result["history_integrity_failure_sha256"]
    raw = (output / "history-integrity-failures" / (pin + ".private.json")).read_bytes()
    assert sha(raw) == pin
    assert json.loads(raw)["error"] == error
    assert result["history_integrity_error_class"] == "ValueError"
    assert error not in json.dumps(result)
    assert "identifying-fixture@example.com" not in json.dumps(result)
    monkeypatch.setattr(HistoryLookup, "verify", lambda _self: None)
    passed = source.review_baseline_batch(output, inventory, worker / "journal", replay=True)
    assert passed["history_integrity_verified"] is True
    assert "history_integrity_failure_sha256" not in passed
    assert (output / "history-integrity-failures" / (pin + ".private.json")).is_file()


def test_article_failure_retains_its_exact_private_cause(prepared, tmp_path, monkeypatch):
    worker = tmp_path / "worker"
    output, record = cloned(prepared, worker, "current")
    archive(output, record)
    monkeypatch.setenv("HTML_TO_MARKDOWN_BASELINE_HISTORY_ROOT", str(worker))
    error = "specific prerequisite failure: identifying-fixture@example.com"

    def fail(*_args):
        raise ValueError(error)

    monkeypatch.setattr(source, "review_baseline_privacy", fail)
    inventory = {"documents": [{"identity": "fixture", "path": "documents/" + record.name}]}
    before = record.read_bytes()
    result = source.review_baseline_batch(output, inventory, worker / "journal", replay=True)
    assert result["history_integrity_verified"] is True
    assert result["exceptions"] == 1
    item = result["documents"][0]
    pin = item["failure_sha256"]
    raw = (output / "review-failures" / (pin + ".private.json")).read_bytes()
    assert sha(raw) == pin
    assert json.loads(raw)["error"] == error
    assert error not in json.dumps(result)
    assert "identifying-fixture@example.com" not in json.dumps(result)
    assert record.read_bytes() == before
