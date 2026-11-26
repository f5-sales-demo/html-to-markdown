"""Proposal recovery streams unrelated histories without weakening custody guards."""

# pylint: disable=protected-access

import json
import os
from pathlib import Path

import pytest

from html_to_markdown import baseline_review_renewal as renewal
from html_to_markdown import baseline_source_review as source
from html_to_markdown.baseline_history_lookup import HistoryLookup
from html_to_markdown.baseline_proposal_history import ProposalHistoryLookup
from html_to_markdown.curation import json_bytes
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.history_preservation import history_transaction
from html_to_markdown.models import PageMetadata
from html_to_markdown.render import serialize_document

pytest_plugins = ["test_baseline_source_review"]


def binding(article="article-a"):
    return {
        "input_kind": "retained_verified_baseline",
        "baseline_receipt_sha256": "a" * 64,
        "baseline_article": article,
        "baseline_article_sha256": "b" * 64,
        "source_files_sha256": "c" * 64,
    }


def preserved(output, value):
    data = json_bytes(value)
    path = output / "history" / (sha(data) + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def proposal(article="article-a", feedback=None):
    return {
        "identity": "https://docs.cloud.f5.com/docs-v2/" + article,
        "binding": binding(article),
        "phases": {"privacy": {"complete_original_proposal": True}},
        "privacy_feedback": feedback or [],
    }


def test_unrelated_history_is_never_fully_materialized(tmp_path, monkeypatch):
    output = tmp_path / "privacy"
    wanted = proposal()
    preserved(output, wanted)
    unrelated = preserved(output, proposal("unrelated") | {"response": "x" * (2 * 1024 * 1024)})
    original = Path.read_bytes

    def read(path):
        if path == unrelated:
            raise AssertionError("unrelated historical response was fully materialized")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", read)
    previous = proposal() | {"phases": {}}
    assert renewal.original_proposal(output, previous, binding()) == wanted


@pytest.mark.parametrize("field", list(binding()))
def test_every_source_identity_field_remains_required(tmp_path, field):
    output = tmp_path / "privacy"
    saved = proposal()
    preserved(output, saved)
    changed = dict(binding())
    changed[field] = "different"
    previous = proposal() | {"phases": {}}
    assert renewal.original_proposal(output, previous, changed) is None


def test_proposal_selection_matches_exhausted_legacy_selection(tmp_path):
    output = tmp_path / "privacy"
    for feedback in [[], ["first"], ["first", "second", "exhausted"]]:
        preserved(output, proposal(feedback=feedback))
    preserved(output, proposal("unrelated", ["one", "two", "three", "four"]))
    for path in (output / "history").glob("*.json"):
        os.utime(path, ns=(123456789, 123456789))
    eligible = []
    for path in sorted((output / "history").glob("*.json")):
        value = json.loads(path.read_bytes())
        if value["identity"] == proposal()["identity"] and renewal.same_source(
            value["binding"], binding()
        ):
            eligible.append(((len(value["privacy_feedback"]), path.stat().st_mtime_ns), value))
    expected = max(eligible, key=lambda item: item[0])[1]
    previous = proposal() | {"phases": {}}
    assert renewal.original_proposal(output, previous, binding()) == expected
    renewed = renewal.prepare_existing_proposal(output, previous, binding(), "d" * 64)
    assert renewed["privacy_feedback"] == ["first", "second", "exhausted"]
    assert renewed["phases"] == expected["phases"]
    assert renewed["existing_proposal_pending"]["new_editor_proposals"] == 0


def test_multiple_articles_parse_each_history_only_once(tmp_path, monkeypatch):
    output = tmp_path / "privacy"
    for article in ["article-a", "article-b", "unrelated"]:
        preserved(output, proposal(article))
    calls = []
    original = HistoryLookup._load

    def loaded(self, path):
        calls.append(path)
        return original(self, path)

    monkeypatch.setattr(HistoryLookup, "_load", loaded)
    lookup = ProposalHistoryLookup(output)
    for article in ["article-a", "article-b", "article-a"]:
        previous = proposal(article) | {"phases": {}}
        assert renewal.original_proposal(
            output, previous, binding(article), lookup=lookup
        ) == proposal(article)
    lookup.verify()
    assert len(calls) == 3 and len(set(calls)) == 3


@pytest.mark.parametrize("damage", ["mutated", "missing", "added", "symlink"])
def test_final_custody_checks_reject_changed_unrelated_history(tmp_path, damage):
    output = tmp_path / "privacy"
    preserved(output, proposal())
    path = preserved(output, proposal("unrelated"))
    lookup = ProposalHistoryLookup(output)
    previous = proposal() | {"phases": {}}
    assert renewal.original_proposal(output, previous, binding(), lookup=lookup) == proposal()
    if damage == "mutated":
        before = path.stat()
        data = path.read_bytes().replace(b"unrelated", b"different")
        assert len(data) == before.st_size
        path.write_bytes(data)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    elif damage == "missing":
        path.unlink()
    elif damage == "added":
        preserved(output, proposal("late"))
    else:
        target = output / "original.json"
        path.rename(target)
        path.symlink_to(target)
    with pytest.raises(ValueError, match="digest changed|custody changed|unsafe"):
        lookup.verify()


@pytest.mark.parametrize("damage", ["digest", "malformed"])
def test_invalid_unrelated_history_cannot_be_skipped(tmp_path, damage):
    output = tmp_path / "privacy"
    preserved(output, proposal())
    if damage == "digest":
        path = preserved(output, proposal("unrelated"))
        path.write_bytes(path.read_bytes() + b" ")
    else:
        data = b'{"binding":{},"unclosed":'
        (output / "history" / (sha(data) + ".json")).write_bytes(data)
    with pytest.raises(ValueError):
        renewal.original_proposal(output, proposal() | {"phases": {}}, binding())


def test_foreign_history_cannot_supply_local_proposal(tmp_path):
    output = tmp_path / "privacy"
    foreign = output / "other" / "source" / "curation-renewal" / "dependency-privacy"
    preserved(foreign, proposal(feedback=["foreign", "strongest", "exhausted"]))
    assert renewal.original_proposal(output, proposal() | {"phases": {}}, binding()) is None


def test_current_original_does_not_need_unrelated_discovery(tmp_path, monkeypatch):
    output = tmp_path / "privacy"
    saved = proposal(feedback=["first", "second", "exhausted"])

    def unexpected(*_args):
        raise AssertionError("current exact proposal must retain its direct recovery path")

    monkeypatch.setattr(ProposalHistoryLookup, "paths", unexpected)
    assert renewal.original_proposal(output, saved, binding()) is saved


def test_owned_new_generation_is_registered_in_both_history_scopes(tmp_path):
    output = tmp_path / "privacy"
    preserved(output, proposal())
    lookup = ProposalHistoryLookup(output)
    observer = HistoryLookup(output, output)
    lookup.paths(binding())
    observer.paths(binding())
    with history_transaction(observer):
        path = lookup.preserve(json_bytes(proposal(feedback=["existing correction"])))
    assert path.exists()
    lookup.verify()
    observer.verify()


def test_preparation_cohort_reuses_streaming_identity_inventory(tmp_path, monkeypatch):
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    (baseline / "publication.json").write_text(json.dumps({"published_at": "2026-11-10T19:49:38Z"}))
    verified = {}
    for slug in ["article-a", "article-b"]:
        metadata = PageMetadata(
            sourceId="docs-cloud-f5-com",
            title=slug,
            slug=slug,
            category="Guides",
            url="https://docs.cloud.f5.com/docs-v2/how-to/" + slug,
        )
        article = "content/docs-cloud-f5-com/how-to/" + slug + "/index.md"
        verified[article] = serialize_document(
            metadata, "# Cloud origin health\n\nUse port 8443 after configuring the origin pool.\n"
        ).encode()
    monkeypatch.setattr(source, "_verified_baseline", lambda *_args: verified)
    output = tmp_path / "privacy"
    inventory = source.prepare_baseline_reviews(baseline, "a" * 64, list(verified), output)
    for item in inventory["documents"]:
        record = output / item["path"]
        saved = json.loads(record.read_bytes())
        saved["binding"]["source_privacy_controller_sha256"] = "d" * 64
        saved["binding_sha256"] = sha(json_bytes(saved["binding"]))
        saved["phases"] = {"privacy": {"complete_original_proposal": True}}
        saved["privacy_feedback"] = ["first", "second", "exhausted"]
        preserved(output, saved)
        record.write_bytes(json_bytes(saved | {"phases": {}}))
    unrelated = preserved(output, proposal("unrelated") | {"response": "x" * 1024})
    calls = []
    original = HistoryLookup._load

    def loaded(self, path):
        calls.append(path)
        return original(self, path)

    monkeypatch.setattr(HistoryLookup, "_load", loaded)
    result = source.prepare_baseline_reviews(baseline, "a" * 64, list(verified), output)
    assert calls.count(unrelated) == 1
    assert len(calls) == len(set(calls))
    for item in result["documents"]:
        saved = json.loads((output / item["path"]).read_bytes())
        assert saved["privacy_feedback"] == ["first", "second", "exhausted"]
        assert saved["existing_proposal_pending"]["new_editor_proposals"] == 0


def test_standalone_result_cannot_bypass_final_verification(tmp_path, monkeypatch):
    output = tmp_path / "privacy"
    preserved(output, proposal())

    def rejected(_self):
        raise ValueError("complete final verification failed")

    monkeypatch.setattr(ProposalHistoryLookup, "verify", rejected)
    with pytest.raises(ValueError, match="complete final verification"):
        renewal.original_proposal(output, proposal() | {"phases": {}}, binding())


def test_prepared_inventory_is_not_written_before_final_verification(prepared, monkeypatch):
    baseline, output, _, article = prepared
    before = (output / "inventory.json").read_bytes()

    def rejected(_self):
        raise ValueError("complete final verification failed")

    monkeypatch.setattr(ProposalHistoryLookup, "verify", rejected)
    with pytest.raises(ValueError, match="complete final verification"):
        source.prepare_baseline_reviews(baseline, "a" * 64, [article], output)
    assert (output / "inventory.json").read_bytes() == before
