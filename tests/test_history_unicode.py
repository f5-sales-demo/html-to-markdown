"""Unicode histories retain exact identity without redundant complete parsing."""

import json

import pytest
from test_history_streaming import history

from html_to_markdown import history_streaming
from html_to_markdown.baseline_history_lookup import HistoryLookup


@pytest.mark.parametrize("read_size", range(1, 18))
@pytest.mark.parametrize("escaped", [r"\ud83d\ude00", r"\uD800\uDC00", r"\uDBFF\udfff"])
def test_valid_pairs_use_one_parse_at_every_small_read_boundary(
    tmp_path, monkeypatch, read_size, escaped
):
    monkeypatch.setattr(history_streaming, "READ_SIZE", read_size)
    data = ('{"binding":{"input_kind":"fixture","baseline_article":"' + escaped + '"}}').encode()
    output, path = history(tmp_path, data)
    wanted = json.loads(data)["binding"]
    lookup = HistoryLookup(output, tmp_path / "worker")
    assert lookup.paths(wanted) == [path]
    assert lookup.metrics["bytes_parsed"] == len(data)
    lookup.verify()
    assert lookup.metrics["bytes_hashed"] == 2 * len(data)


@pytest.mark.parametrize(
    "escaped",
    [r"\ud800", r"\udc00", r"\ud800\ud800", r"\\ud800\udc00", r"\\\ud83d\ude00"],
)
@pytest.mark.parametrize("read_size", [1, 5, 11, 17, 1048576])
def test_isolated_or_literal_surrogate_escapes_preserve_standard_identity(
    tmp_path, monkeypatch, escaped, read_size
):
    monkeypatch.setattr(history_streaming, "READ_SIZE", read_size)
    data = ('{"binding":{"input_kind":"fixture","baseline_article":"' + escaped + '"}}').encode()
    wanted = json.loads(data)["binding"]
    output, path = history(tmp_path, data)
    lookup = HistoryLookup(output, tmp_path / "worker")
    assert lookup.paths(wanted) == [path]
    lookup.verify()


@pytest.mark.parametrize("slashes", [1, 2, 3, 32, 33, 104, 105])
def test_long_backslash_runs_preserve_pair_escape_parity(tmp_path, monkeypatch, slashes):
    monkeypatch.setattr(history_streaming, "READ_SIZE", 7)
    escaped = "\\" * slashes + "ud83d\\ude00"
    data = ('{"binding":{"input_kind":"fixture","baseline_article":"' + escaped + '"}}').encode()
    output, path = history(tmp_path, data)
    wanted = json.loads(data)["binding"]
    lookup = HistoryLookup(output, tmp_path / "worker")
    assert lookup.paths(wanted) == [path]
    lookup.verify()


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16", "utf-32"])
def test_valid_pairs_preserve_original_encoding_and_digest(tmp_path, monkeypatch, encoding):
    monkeypatch.setattr(history_streaming, "READ_SIZE", 13)
    document = r'{"binding":{"input_kind":"fixture","baseline_article":"\ud83d\ude00"}}'
    data = document.encode(encoding)
    output, path = history(tmp_path, data)
    lookup = HistoryLookup(output, tmp_path / "worker")
    assert lookup.paths(json.loads(data)["binding"]) == [path]
    assert lookup.metrics["bytes_parsed"] == len(data)
    lookup.verify()


@pytest.mark.parametrize("bad", [r"\uD83", r"\ud800\q", r"\ud83d\ude00"])
def test_invalid_complete_unicode_history_never_supplies_identity(tmp_path, bad):
    data = ('{"binding":{"input_kind":"fixture"},"raw":"' + bad + '"} trailing').encode()
    output, _ = history(tmp_path, data)
    lookup = HistoryLookup(output, tmp_path / "worker")
    with pytest.raises(ValueError):
        lookup.paths({"input_kind": "fixture"})
    assert lookup.metrics["files_parsed"] == 0


def test_large_unrelated_valid_unicode_payload_is_parsed_once(tmp_path):
    fragment = '"' + "x" * 32768 + r"\ud83d\ude00" + '"'
    data = (
        '{"binding":{"input_kind":"fixture"},"raw":[' + ",".join([fragment] * 128) + "]}"
    ).encode()
    output, path = history(tmp_path, data)
    lookup = HistoryLookup(output, tmp_path / "worker")
    assert lookup.paths({"input_kind": "fixture"}) == [path]
    assert lookup.metrics["bytes_parsed"] == len(data)
    lookup.verify()
