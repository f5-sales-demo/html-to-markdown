"""Grouped replay shares lookup work and still rejects changed final inputs."""

# pylint: disable=protected-access

import json

import pytest
from test_baseline_global_ledgers import cloned, exhaust

from html_to_markdown import baseline_global_ledgers as ledger
from html_to_markdown import curation_renewal_policy as policy
from html_to_markdown.baseline_history_lookup import HistoryLookup
from html_to_markdown.baseline_ledger_scope import ledger_scope
from html_to_markdown.curation import json_bytes
from html_to_markdown.enrichment.analysis import sha

pytest_plugins = ["test_baseline_source_review"]


def history(prepared, tmp_path):
    root = tmp_path / "worker"
    first, donor = cloned(prepared, root, "first")
    output, record = cloned(prepared, root, "second")
    exhaust(first, donor)
    data = donor.read_bytes()
    path = first / "history" / (sha(data) + ".json")
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(data)
    return root, output, record, path


def test_grouped_and_nested_calls_parse_each_history_once(prepared, tmp_path, monkeypatch):
    root, output, record, _ = history(prepared, tmp_path)
    calls = []
    original = HistoryLookup._load

    def loaded(self, path):
        calls.append(path)
        return original(self, path)

    monkeypatch.setattr(HistoryLookup, "_load", loaded)
    wanted = json.loads(record.read_bytes())["binding"]
    with ledger_scope(output, root):
        first = list(ledger._candidates(output, record, root, wanted))
        with ledger_scope(output, root):
            second = list(ledger._candidates(output, record, root, wanted))
    assert [(str(p), sha(data)) for p, _, data in first] == [
        (str(p), sha(data)) for p, _, data in second
    ]
    assert len(calls) == 1


def test_post_replay_mutation_cannot_escape_scope_verification(prepared, tmp_path):
    root, output, record, path = history(prepared, tmp_path)
    wanted = json.loads(record.read_bytes())["binding"]
    with pytest.raises(ValueError, match="digest changed"), ledger_scope(output, root):
        list(ledger._candidates(output, record, root, wanted))
        path.write_bytes(path.read_bytes() + b" ")


def test_new_history_after_last_lookup_blocks_grouped_output(prepared, tmp_path):
    root, output, record, path = history(prepared, tmp_path)
    wanted = json.loads(record.read_bytes())["binding"]
    with pytest.raises(ValueError, match="custody changed"), ledger_scope(output, root):
        list(ledger._candidates(output, record, root, wanted))
        saved = json.loads(path.read_bytes())
        saved["late_original"] = True
        data = json_bytes(saved)
        (path.parent / (sha(data) + ".json")).write_bytes(data)


def test_scope_without_baseline_calls_does_not_require_history_inventory(prepared, tmp_path):
    root, output, _, _ = history(prepared, tmp_path)
    with ledger_scope(output, root):
        pass


def test_policy_group_reuses_source_custody_lookup(prepared, tmp_path, monkeypatch):

    root, output, record, _ = history(prepared, tmp_path)
    monkeypatch.setenv("HTML_TO_MARKDOWN_BASELINE_HISTORY_ROOT", str(root))
    calls = []
    original = HistoryLookup._load

    def loaded(self, path):
        calls.append(path)
        return original(self, path)

    monkeypatch.setattr(HistoryLookup, "_load", loaded)
    wanted = json.loads(record.read_bytes())["binding"]

    def checked(_output, _contexts):
        list(ledger._candidates(output, record, root, wanted))
        list(ledger._candidates(output, record, root, wanted))
        return "reviewed-policy"

    monkeypatch.setattr(policy, "_reviewed_policy", checked)
    assert policy.reviewed_policy(output.parent, [{}]) == "reviewed-policy"
    assert len(calls) == 1


def test_planner_receipt_is_not_written_before_final_source_integrity(
    prepared, tmp_path, monkeypatch
):

    root, output, record, path = history(prepared, tmp_path)
    monkeypatch.setenv("HTML_TO_MARKDOWN_BASELINE_HISTORY_ROOT", str(root))
    wanted = json.loads(record.read_bytes())["binding"]

    def planned(*_args):
        list(ledger._candidates(output, record, root, wanted))
        path.write_bytes(path.read_bytes() + b" ")
        return {"would_be_approved": True}

    monkeypatch.setattr(policy, "_qualify_policy", planned)
    with pytest.raises(ValueError, match="digest changed"):
        policy.qualify_policy(output.parent, [{}], tmp_path / "baseline", "a" * 64)
    assert not (output.parent / "planner-qualification.json").exists()
