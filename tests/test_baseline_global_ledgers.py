"""Retained privacy histories follow the exact source across queue changes."""

import json
import shutil
import threading

import pytest
from test_baseline_source_review import Client

from html_to_markdown import baseline_global_ledgers as ledger
from html_to_markdown import baseline_source_review as source
from html_to_markdown.baseline_late_validation import _execution_digest as execution_digest
from html_to_markdown.curation import json_bytes
from html_to_markdown.enrichment.analysis import sha

pytest_plugins = ["test_baseline_source_review"]


def cloned(prepared, root, queue):
    _, original, record, _ = prepared
    output = root / queue / "docs-cloud-f5-com/curation-renewal/dependency-privacy"
    shutil.copytree(original, output)
    return output, output / "documents" / record.name


def exhaust(output, record):
    with pytest.raises(ValueError, match="exhausted"):
        source.review_baseline_privacy(output, record, Client(reject=True))
    result = json.loads(record.read_bytes())
    assert len(result["privacy_feedback"]) == 3
    return result


def test_other_queue_carries_exhausted_original_without_new_editor(prepared, tmp_path):
    root = tmp_path / "worker"
    first, old_record = cloned(prepared, root, "first")
    second, record = cloned(prepared, root, "second")
    previous = exhaust(first, old_record)
    original_bytes = old_record.read_bytes()
    result = ledger.reconcile_record(second, record, root, execution_digest())
    current = json.loads(record.read_bytes())
    assert result["changed"] and result["strongest_feedback_entries"] == 3
    assert current["privacy_feedback"] == previous["privacy_feedback"]
    assert current["phases"] == previous["phases"]
    assert current["existing_proposal_pending"]["new_editor_proposals"] == 0
    client = Client()
    checked = source.review_baseline_privacy(second, record, client)
    assert checked["status"] == "privacy_only_validated"
    assert all(body["text"]["format"]["name"] != "privacydecision" for body in client.calls)
    assert old_record.read_bytes() == original_bytes


def test_exhausted_history_cannot_be_hidden_by_weaker_current_success(prepared, tmp_path):
    root = tmp_path / "worker"
    first, old_record = cloned(prepared, root, "first")
    second, record = cloned(prepared, root, "second")
    previous = exhaust(first, old_record)
    old_bytes = old_record.read_bytes()
    path = first / "history" / (sha(old_bytes) + ".json")
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(old_bytes)
    # A copied blank record's later success must not erase the earlier budget.
    successful, successful_record = cloned(prepared, tmp_path / "separate", "success")
    source.review_baseline_privacy(successful, successful_record, Client())
    old_record.write_bytes(successful_record.read_bytes())
    result = ledger.reconcile_record(second, record, root, execution_digest())
    assert result["strongest_feedback_entries"] == 3
    assert json.loads(record.read_bytes())["privacy_feedback"] == previous["privacy_feedback"]
    assert path.read_bytes() == old_bytes


@pytest.mark.parametrize("change", ["controller", "policy", "relevance"])
def test_context_changes_preserve_matching_source_allowance(prepared, tmp_path, change):
    root = tmp_path / "worker"
    first, old_record = cloned(prepared, root, "first")
    second, record = cloned(prepared, root, "second")
    previous = exhaust(first, old_record)
    current = json.loads(record.read_bytes())
    key = {
        "controller": "preparation_controller_sha256",
        "policy": "curation_policy_sha256",
        "relevance": "relevance_evidence_sha256",
    }[change]
    current["binding"][key] = "b" * 64
    current["binding_sha256"] = sha(json_bytes(current["binding"]))
    record.write_bytes(json_bytes(current))
    result = ledger.reconcile_record(second, record, root, execution_digest())
    saved = json.loads(record.read_bytes())
    assert result["changed"] and saved["privacy_feedback"] == previous["privacy_feedback"]
    assert saved["existing_proposal_pending"]["prepared_binding"][key] == "b" * 64
    assert saved["binding"] == previous["binding"]


@pytest.mark.parametrize("damage", ["source", "binding", "history", "symlink"])
def test_unsafe_matching_history_blocks_reconciliation_without_grant(prepared, tmp_path, damage):
    root = tmp_path / "worker"
    first, old_record = cloned(prepared, root, "first")
    second, record = cloned(prepared, root, "second")
    saved = exhaust(first, old_record)
    before = record.read_bytes()
    if damage == "source":
        path = first / saved["source"] / saved["item"]["article_path"]
        path.write_bytes(path.read_bytes() + b" changed")
    elif damage == "binding":
        saved["binding_sha256"] = "b" * 64
        old_record.write_bytes(json_bytes(saved))
    elif damage == "history":
        path = first / "history" / ("b" * 64 + ".json")
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(json_bytes(saved))
    else:
        moved = old_record.with_suffix(".saved")
        old_record.rename(moved)
        old_record.symlink_to(moved)
    with pytest.raises(ValueError):
        ledger.reconcile_record(second, record, root, execution_digest())
    assert record.read_bytes() == before


def test_genuinely_different_source_is_not_rebound_to_an_old_ledger(prepared, tmp_path):
    root = tmp_path / "worker"
    first, old_record = cloned(prepared, root, "first")
    second, record = cloned(prepared, root, "second")
    exhaust(first, old_record)
    current = json.loads(record.read_bytes())
    article = current["item"]["article_path"]
    path = second / current["source"] / article
    path.write_bytes(path.read_bytes() + b"\nA new authoritative source condition.\n")
    current["input_file_hashes"][article] = sha(path.read_bytes())
    current["binding"]["baseline_article_sha256"] = sha(path.read_bytes())
    current["binding"]["source_files_sha256"] = sha(json_bytes(current["input_file_hashes"]))
    current["binding_sha256"] = sha(json_bytes(current["binding"]))
    record.write_bytes(json_bytes(current))
    before = record.read_bytes()
    result = ledger.reconcile_record(second, record, root, execution_digest())
    assert not result["changed"] and record.read_bytes() == before


def test_configured_history_root_covers_an_isolated_qualification(prepared, tmp_path, monkeypatch):
    root = tmp_path / "worker"
    first, old_record = cloned(prepared, root, "first")
    second, record = cloned(prepared, tmp_path / "qualification", "second")
    exhaust(first, old_record)
    monkeypatch.setenv("HTML_TO_MARKDOWN_BASELINE_HISTORY_ROOT", str(root))
    result = ledger.reconcile_record(
        second, record, ledger.history_root(tmp_path / "journal"), execution_digest()
    )
    assert result["strongest_feedback_entries"] == 3


def test_read_only_replay_rejects_a_weaker_later_success(prepared, tmp_path):
    root = tmp_path / "worker"
    first, old_record = cloned(prepared, root, "first")
    second, record = cloned(prepared, root, "second")
    exhaust(first, old_record)
    source.review_baseline_privacy(second, record, Client())
    before = record.read_bytes()
    with pytest.raises(ValueError, match="stronger prior"):
        ledger.verify_strongest_ledger(second, record, root)
    assert record.read_bytes() == before


@pytest.mark.parametrize("damage", ["feedback", "proposal", "allowance", "origin"])
def test_reconciled_original_and_budget_cannot_be_replaced(prepared, tmp_path, damage):
    root = tmp_path / "worker"
    first, old_record = cloned(prepared, root, "first")
    second, record = cloned(prepared, root, "second")
    exhaust(first, old_record)
    ledger.reconcile_record(second, record, root, execution_digest())
    saved = json.loads(record.read_bytes())
    if damage == "feedback":
        saved["privacy_feedback"] = []
    elif damage == "proposal":
        saved["phases"]["privacy"] = {}
    elif damage == "allowance":
        saved["retained_privacy_ledger"]["new_editor_proposals"] = 1
    else:
        path = (
            second
            / "history"
            / (saved["retained_privacy_ledger"]["original_record_sha256"] + ".json")
        )
        path.write_bytes(path.read_bytes() + b" ")
    record.write_bytes(json_bytes(saved))
    client = Client()
    with pytest.raises(ValueError):
        source.review_baseline_privacy(second, record, client)
    assert not client.calls


def test_identical_source_is_serialized_across_different_queues(prepared, tmp_path):
    root = tmp_path / "worker"
    _, first = cloned(prepared, root, "first")
    _, second = cloned(prepared, root, "second")
    acquired = threading.Event()
    entering = threading.Event()

    def enter():
        entering.set()
        with ledger.source_lock(second, root):
            acquired.set()

    with ledger.source_lock(first, root):
        thread = threading.Thread(target=enter)
        thread.start()
        assert entering.wait(2)
        assert not acquired.wait(0.1)
    thread.join(timeout=2)
    assert acquired.is_set() and not thread.is_alive()


def test_existing_validated_proposal_is_rechecked_without_a_duplicate_editor(prepared, tmp_path):
    root = tmp_path / "worker"
    first, old_record = cloned(prepared, root, "first")
    second, record = cloned(prepared, root, "second")
    original = source.review_baseline_privacy(first, old_record, Client())
    result = ledger.reconcile_record(second, record, root, execution_digest())
    assert result["changed"] and result["strongest_feedback_entries"] == 0
    client = Client()
    reviewed = source.review_baseline_privacy(second, record, client)
    assert reviewed["phases"]["privacy"] == original["phases"]["privacy"]
    assert [body["text"]["format"]["name"] for body in client.calls] == ["admissionvalidation"]


def test_batch_replay_keeps_a_weaker_source_queued_without_mutation(prepared, tmp_path):
    root = tmp_path / "worker"
    first, old_record = cloned(prepared, root, "first")
    second, record = cloned(prepared, root, "second")
    exhaust(first, old_record)
    source.review_baseline_privacy(second, record, Client())
    original = record.read_bytes()
    inventory = {
        "documents": [
            {"identity": json.loads(original)["identity"], "path": "documents/" + record.name}
        ]
    }
    result = source.review_baseline_batch(second, inventory, root / "journal", replay=True)
    assert result["exceptions"] == 1 and result["documents"][0]["disposition"] == "queued"
    assert record.read_bytes() == original


def test_archive_only_donor_keeps_exhausted_budget(prepared, tmp_path):
    root = tmp_path / "worker"
    first, old_record = cloned(prepared, root, "first")
    second, record = cloned(prepared, root, "second")
    previous = exhaust(first, old_record)
    data = old_record.read_bytes()
    history = first / "history" / (sha(data) + ".json")
    history.parent.mkdir(exist_ok=True)
    history.write_bytes(data)
    old_record.unlink()
    before = record.read_bytes()
    with pytest.raises(ValueError, match="stronger prior"):
        ledger.verify_strongest_ledger(second, record, root)
    assert record.read_bytes() == before
    result = ledger.reconcile_record(second, record, root, execution_digest())
    assert result["strongest_feedback_entries"] == 3
    assert json.loads(record.read_bytes())["privacy_feedback"] == previous["privacy_feedback"]
    assert history.read_bytes() == data


def test_equal_exhausted_divergent_proposals_are_not_selected_by_hash(prepared, tmp_path):
    root = tmp_path / "worker"
    first, old_record = cloned(prepared, root, "first")
    second, record = cloned(prepared, root, "second")
    previous = exhaust(first, old_record)
    record.write_bytes(old_record.read_bytes())
    later = json.loads(old_record.read_bytes())
    later["phases"]["privacy"]["request_sha256"] = "b" * 64
    old_record.write_bytes(json_bytes(later))
    before = record.read_bytes()
    client = Client()
    with pytest.raises(ValueError, match="conflicting strongest"):
        ledger.reconcile_record(second, record, root, execution_digest())
    with pytest.raises(ValueError, match="conflicting strongest"):
        ledger.verify_strongest_ledger(second, record, root)
    assert record.read_bytes() == before
    assert json.loads(before)["privacy_feedback"] == previous["privacy_feedback"]
    assert not client.calls


def test_equal_exhausted_same_proposal_keeps_original(prepared, tmp_path):
    root = tmp_path / "worker"
    first, old_record = cloned(prepared, root, "first")
    second, record = cloned(prepared, root, "second")
    exhaust(first, old_record)
    record.write_bytes(old_record.read_bytes())
    before = record.read_bytes()
    result = ledger.reconcile_record(second, record, root, execution_digest())
    assert not result["changed"]
    ledger.verify_strongest_ledger(second, record, root)
    assert record.read_bytes() == before
