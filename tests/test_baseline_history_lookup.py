"""A batch lookup amortizes history parsing without approving changed custody."""

# pylint: disable=protected-access

import json
import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_baseline_global_ledgers import cloned, exhaust

from html_to_markdown import baseline_global_ledgers as ledger
from html_to_markdown import baseline_source_review as source
from html_to_markdown.baseline_history_lookup import HistoryLookup
from html_to_markdown.curation import json_bytes
from html_to_markdown.enrichment.analysis import sha

pytest_plugins = ["test_baseline_source_review"]


def archive(output, record):
    data = record.read_bytes()
    path = output / "history" / (sha(data) + ".json")
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(data)
    return path


def setup_history(prepared, tmp_path):
    root = tmp_path / "worker"
    first, donor = cloned(prepared, root, "first")
    second, current = cloned(prepared, root, "second")
    exhaust(first, donor)
    path = archive(first, donor)
    donor.unlink()
    return root, first, second, current, path


def signatures(candidates):
    return [(str(path), sha(data)) for path, _, data in candidates]


def test_batch_matches_exhaustive_archive_only_custody(prepared, tmp_path):
    root, _, output, record, _ = setup_history(prepared, tmp_path)
    wanted = json.loads(record.read_bytes())["binding"]
    expected = signatures(ledger._candidates(output, record, root, wanted))
    lookup = HistoryLookup(output, root)
    actual = signatures(ledger._candidates(output, record, root, wanted, lookup=lookup))
    lookup.verify()
    assert actual == expected
    assert lookup.metrics["files_parsed"] == 1


def test_concurrent_lookups_parse_each_unrelated_history_once(prepared, tmp_path):
    root, first, output, record, _ = setup_history(prepared, tmp_path)
    data = json.loads(record.read_bytes())
    data["binding"]["baseline_article_sha256"] = "b" * 64
    data["large_untrusted_response"] = "private-response-marker" * 1000
    unrelated = json_bytes(data)
    (first / "history" / (sha(unrelated) + ".json")).write_bytes(unrelated)
    lookup = HistoryLookup(output, root)
    wanted = json.loads(record.read_bytes())["binding"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: list(lookup.paths(wanted)), range(24)))
    assert all(result == results[0] for result in results)
    assert lookup.metrics["files_parsed"] == 2
    assert "private-response-marker" not in repr(vars(lookup))
    lookup.verify()


def test_new_matching_history_cannot_escape_a_later_lookup(prepared, tmp_path):
    root, first, output, record, path = setup_history(prepared, tmp_path)
    lookup = HistoryLookup(output, root)
    wanted = json.loads(record.read_bytes())["binding"]
    assert path in list(lookup.paths(wanted))
    data = json.loads(path.read_bytes())
    data["new_history_marker"] = True
    extra = json_bytes(data)
    added = first / "history" / (sha(extra) + ".json")
    added.write_bytes(extra)
    assert added in list(lookup.paths(wanted))
    lookup.verify()


def test_new_unobserved_history_invalidates_final_batch(prepared, tmp_path):
    root, first, output, record, path = setup_history(prepared, tmp_path)
    lookup = HistoryLookup(output, root)
    list(lookup.paths(json.loads(record.read_bytes())["binding"]))
    data = json.loads(path.read_bytes())
    data["late_history_marker"] = True
    extra = json_bytes(data)
    (first / "history" / (sha(extra) + ".json")).write_bytes(extra)
    with pytest.raises(ValueError, match="custody changed"):
        lookup.verify()


def test_unrelated_mutation_rejected_by_bytes_even_with_restored_mtime(prepared, tmp_path):
    root, _, output, record, path = setup_history(prepared, tmp_path)
    lookup = HistoryLookup(output, root)
    list(lookup.paths({}))
    before = path.stat()
    data = path.read_bytes()
    path.write_bytes(data.replace(b"privacy", b"prIvacy", 1))
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(ValueError, match="digest changed"):
        lookup.verify()


@pytest.mark.parametrize("damage", ["history", "directory", "source", "deleted"])
def test_cached_lookup_keeps_custody_checks(prepared, tmp_path, damage):
    root, first, output, record, path = setup_history(prepared, tmp_path)
    lookup = HistoryLookup(output, root)
    wanted = json.loads(record.read_bytes())["binding"]
    list(lookup.paths(wanted))
    if damage == "source":
        saved = json.loads(path.read_bytes())
        source = first / saved["source"] / saved["item"]["article_path"]
        source.write_bytes(source.read_bytes() + b" changed")
        with pytest.raises(ValueError, match="source bytes changed"):
            list(ledger._candidates(output, record, root, wanted, lookup=lookup))
        return
    target = first / "history" if damage == "directory" else path
    if damage == "deleted":
        target.unlink()
    else:
        moved = target.with_name(target.name + ".preserved")
        target.rename(moved)
        target.symlink_to(moved)
    with pytest.raises(ValueError):
        lookup.verify()


def test_batch_final_custody_failure_queues_every_result(prepared, tmp_path, monkeypatch):
    root, _, output, record, _ = setup_history(prepared, tmp_path)
    monkeypatch.setenv("HTML_TO_MARKDOWN_BASELINE_HISTORY_ROOT", str(root))
    monkeypatch.setattr(
        source, "review_baseline_privacy", lambda *args: {"status": "privacy_only_validated"}
    )

    def fail(_self):
        raise ValueError("retained privacy history custody changed")

    monkeypatch.setattr(HistoryLookup, "verify", fail)
    inventory = {"documents": [{"identity": "fixture", "path": "documents/" + record.name}]}
    result = source.review_baseline_batch(output, inventory, root / "journal", replay=True)
    assert result["exceptions"] == 1
    assert result["documents"][0]["disposition"] == "queued"
    assert result["history_integrity_verified"] is False


def test_own_exact_preserved_history_keeps_batch_valid(prepared, tmp_path, monkeypatch):
    root, _, output, record, _ = setup_history(prepared, tmp_path)
    monkeypatch.setenv("HTML_TO_MARKDOWN_BASELINE_HISTORY_ROOT", str(root))

    def reviewed(_output, current, _client):
        archive(_output, current)
        return {"status": "privacy_only_validated"}

    monkeypatch.setattr(source, "review_baseline_privacy", reviewed)
    inventory = {"documents": [{"identity": "fixture", "path": "documents/" + record.name}]}
    result = source.review_baseline_batch(output, inventory, root / "journal", replay=True)
    assert result["exceptions"] == 1  # stronger original still prevents replay
    # Restore the exact strongest original, preserving the normal replay checks.
    history = next(
        path
        for path in root.glob(
            "*/docs-cloud-f5-com/curation-renewal/dependency-privacy/history/*.json"
        )
    )
    record.write_bytes(history.read_bytes())
    result = source.review_baseline_batch(output, inventory, root / "journal", replay=True)
    assert result["exceptions"] == 0
    assert result["history_integrity_verified"] is True


def test_lookup_rejects_wrong_custody_scope(prepared, tmp_path):
    root, first, output, record, _ = setup_history(prepared, tmp_path)
    lookup = HistoryLookup(first, root)
    with pytest.raises(ValueError, match="custody mismatch"):
        list(ledger._candidates(output, record, root, {}, lookup=lookup))


@pytest.mark.parametrize("damage", ["replace", "create", "root"])
def test_final_guard_rejects_concurrent_current_custody(prepared, tmp_path, damage):
    root, first, output, record, _ = setup_history(prepared, tmp_path)
    lookup = HistoryLookup(output, root)
    wanted = json.loads(record.read_bytes())["binding"]
    list(ledger._candidates(output, record, root, wanted, lookup=lookup))
    if damage == "replace":
        record.write_bytes(record.read_bytes() + b" ")
    elif damage == "create":
        (first / "documents" / record.name).write_bytes(record.read_bytes())
    else:
        (root / "new/docs-cloud-f5-com/curation-renewal/dependency-privacy").mkdir(parents=True)
    with pytest.raises(ValueError, match="changed"):
        lookup.verify()


def test_mutation_after_an_early_hash_invalidates_the_whole_verification(
    prepared, tmp_path, monkeypatch
):
    root, first, output, record, path = setup_history(prepared, tmp_path)
    data = json.loads(path.read_bytes())
    data["second_history"] = True
    other = json_bytes(data)
    (first / "history" / (sha(other) + ".json")).write_bytes(other)
    lookup = HistoryLookup(output, root)
    lookup.paths(json.loads(record.read_bytes())["binding"])
    hashed = lookup._file_digest
    modified = []

    def mutate_after_hash(current):
        digest = hashed(current)
        if not modified:
            before = current.stat()
            current.write_bytes(current.read_bytes() + b" ")
            os.utime(current, ns=(before.st_atime_ns, before.st_mtime_ns))
            modified.append(current)
        return digest

    monkeypatch.setattr(lookup, "_file_digest", mutate_after_hash)
    with pytest.raises(ValueError, match="changed"):
        lookup.verify()


def test_identical_history_digests_parse_once_across_custody_roots(prepared, tmp_path):
    root, _, output, record, path = setup_history(prepared, tmp_path)
    duplicate = output / "history" / path.name
    duplicate.parent.mkdir(exist_ok=True)
    duplicate.write_bytes(path.read_bytes())
    lookup = HistoryLookup(output, root)
    found = lookup.paths(json.loads(record.read_bytes())["binding"])
    lookup.verify()
    assert set(found) == {path, duplicate}
    assert lookup.metrics["files_parsed"] == 1
    assert lookup.metrics["bytes_parsed"] == path.stat().st_size


def test_known_digest_never_grants_unverified_duplicate_bytes(prepared, tmp_path):
    root, _, output, record, path = setup_history(prepared, tmp_path)
    lookup = HistoryLookup(output, root)
    wanted = json.loads(record.read_bytes())["binding"]
    lookup.paths(wanted)
    duplicate = output / "history" / path.name
    duplicate.parent.mkdir(exist_ok=True)
    duplicate.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="digest changed"):
        lookup.paths(wanted)
