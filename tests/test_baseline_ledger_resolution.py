"""Conflict selection requires independent complete checks and immutable histories."""

# Exercise exact selection guards against malformed independent evidence.
# pylint: disable=protected-access

import json

import pytest
from test_baseline_global_ledgers import cloned, exhaust
from test_baseline_source_review import Client

from html_to_markdown import baseline_global_ledgers as ledger
from html_to_markdown import baseline_ledger_resolution as resolution
from html_to_markdown.baseline_late_validation import _execution_digest
from html_to_markdown.curation import json_bytes
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.enrichment.contracts import ResponseEvidence
from html_to_markdown.enrichment.gates import request_hash

pytest_plugins = ["test_baseline_source_review"]


class IndependentClient(Client):
    def __init__(self, *, refuse=False, tamper=False, reject=False):
        super().__init__(reject=reject)
        self.refuse = refuse
        self.tamper = tamper
        self.selected = None

    def execute(self, body):
        if body["text"]["format"]["name"] != "ledgerresolutionvalidation":
            return super().execute(body)
        self.calls.append(body)
        payload = json.loads(body["input"][0]["content"][0]["text"])
        eligible = [
            item
            for item in payload["candidates"]
            if item["disposition"] == "independently_validated"
        ]
        self.selected = eligible[-1]["record_sha256"]
        value = {
            "identity": payload["identity"],
            "binding_sha256": "f" * 64 if self.tamper else payload["binding_sha256"],
            "selected_record_sha256": None if self.refuse else self.selected,
            "complete_evidence_reviewed": True,
            "dispositions": [
                {
                    "record_sha256": item["record_sha256"],
                    "disposition": "select"
                    if item["record_sha256"] == self.selected
                    else "equivalent_validated"
                    if item in eligible
                    else "reject",
                    "reason": "Independently validated identical output"
                    if item in eligible
                    else "Exact source/controller validation failed",
                }
                for item in payload["candidates"]
            ],
            "unresolved": ["Ambiguous"] if self.refuse else [],
        }
        response = {
            "status": "completed",
            "model": body["model"],
            "output": [{"content": [{"type": "output_text", "text": json.dumps(value)}]}],
        }
        return ResponseEvidence(
            request_sha256=request_hash(body),
            response_sha256=sha(json.dumps(response, sort_keys=True, separators=(",", ":"))),
            model=body["model"],
            reasoning="high",
            prompt_version=body["instructions"].rsplit("Prompt version: ", 1)[-1].strip(),
            request=body,
            response=response,
            error=None,
            transport="codex_litellm",
            runtime_version="test",
        )


def conflicting(prepared, tmp_path):
    root = tmp_path / "worker"
    first, donor = cloned(prepared, root, "first")
    second, other = cloned(prepared, root, "second")
    output, record = cloned(prepared, root, "third")
    exhaust(first, donor)
    original = json.loads(donor.read_bytes())
    divergent = json.loads(donor.read_bytes())
    divergent["privacy_feedback"][-1] = "A distinct preserved historical validator failure."
    other.write_bytes(json_bytes(divergent))
    return root, output, record, {donor: donor.read_bytes(), other: other.read_bytes()}, original


def test_independent_selection_preserves_every_exhausted_ledger(prepared, tmp_path):
    root, output, record, originals, previous = conflicting(prepared, tmp_path)
    client = IndependentClient()
    result = ledger.reconcile_record(output, record, root, _execution_digest(), client=client)
    saved = json.loads(record.read_bytes())
    assert result["conflict_independently_resolved"] and result["new_editor_proposals"] == 0
    assert result["article_approved"] is False and saved["retirement_review_required"]
    assert len(saved["privacy_feedback"]) == len(previous["privacy_feedback"]) == 3
    assert saved["retained_privacy_conflict"]["selected_record_sha256"] == client.selected
    assert [body["text"]["format"]["name"] for body in client.calls] == [
        "admissionvalidation",
        "admissionvalidation",
        "ledgerresolutionvalidation",
    ]
    assert all(path.read_bytes() == data for path, data in originals.items())
    before = record.read_bytes()
    files = {
        path.relative_to(output): sha(path.read_bytes())
        for path in output.rglob("*")
        if path.is_file()
    }
    ledger.verify_strongest_ledger(output, record, root)
    assert record.read_bytes() == before
    assert files == {
        path.relative_to(output): sha(path.read_bytes())
        for path in output.rglob("*")
        if path.is_file()
    }
    result = ledger.reconcile_record(output, record, root, _execution_digest())
    assert not result["changed"]


@pytest.mark.parametrize("damage", ["refuse", "tamper", "reject"])
def test_failed_validation_or_selection_cannot_change_current_record(prepared, tmp_path, damage):
    root, output, record, originals, _ = conflicting(prepared, tmp_path)
    before = record.read_bytes()
    client = IndependentClient(**{damage: True})
    with pytest.raises(ValueError, match="conflicting strongest"):
        ledger.reconcile_record(output, record, root, _execution_digest(), client=client)
    assert record.read_bytes() == before
    assert all(path.read_bytes() == data for path, data in originals.items())
    assert all(body["text"]["format"]["name"] != "privacydecision" for body in client.calls)


def test_unusable_original_is_explicitly_rejected_without_an_editor(prepared, tmp_path):
    root, output, record, originals, _ = conflicting(prepared, tmp_path)
    donor = next(iter(originals))
    bad = json.loads(donor.read_bytes())
    bad["phases"]["privacy"]["request_sha256"] = "b" * 64
    donor.write_bytes(json_bytes(bad))
    result = ledger.reconcile_record(
        output, record, root, _execution_digest(), client=IndependentClient()
    )
    assert result["conflict_independently_resolved"]
    saved = json.loads(record.read_bytes())
    outcomes = saved["retained_privacy_conflict"]["results"]
    assert [value["disposition"] for value in outcomes].count("queued") == 1
    assert all(
        len(value["privacy_feedback"]) == 3
        for value in [json.loads(path.read_bytes()) for path in originals]
    )


@pytest.mark.parametrize("damage", ["stronger", "same_strength", "policy", "response", "allowance"])
def test_saved_resolution_cannot_bypass_changed_evidence(prepared, tmp_path, monkeypatch, damage):
    root, output, record, originals, _ = conflicting(prepared, tmp_path)
    ledger.reconcile_record(output, record, root, _execution_digest(), client=IndependentClient())
    saved = json.loads(record.read_bytes())
    if damage in {"stronger", "same_strength"}:
        donor = next(iter(originals))
        extra = json.loads(donor.read_bytes())
        extra["privacy_feedback"].append("New independently retained failure.")
        if damage == "same_strength":
            extra["privacy_feedback"].pop(0)
        data = json_bytes(extra)
        path = donor.parent.parent / "history" / (sha(data) + ".json")
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(data)
    elif damage == "policy":
        monkeypatch.setattr(resolution, "review_context_digest", lambda: "e" * 64)
    elif damage == "response":
        proof = saved["retained_privacy_conflict"]
        directory = resolution.resolution_root(output, record) / sha(json_bytes(proof["binding"]))
        path = directory / "selection.json"
        value = json.loads(path.read_bytes())
        value["response_sha256"] = "f" * 64
        path.write_bytes(json_bytes(value))
    else:
        saved["privacy_feedback"] = []
        record.write_bytes(json_bytes(saved))
    with pytest.raises(ValueError):
        ledger.verify_strongest_ledger(output, record, root)


def test_distinct_validated_outputs_stay_ambiguous(prepared, tmp_path):
    root, output, record, _, _ = conflicting(prepared, tmp_path)
    inventory = resolution.inventory_record(output, record, root)
    binding = resolution.strongest_binding(inventory, _execution_digest())
    results = [
        {
            "record_sha256": item["record_sha256"],
            "disposition": "independently_validated",
            "candidate_sha256": str(index) * 64,
        }
        for index, item in enumerate(inventory["strongest_records"])
    ]
    body = resolution._selector(binding, results)
    evidence = IndependentClient().execute(body)
    with pytest.raises(ValueError, match="complete independent"):
        resolution._decision(evidence, body, results)


def test_symlinked_candidate_evidence_cannot_be_selected(prepared, tmp_path):
    root, output, record, originals, _ = conflicting(prepared, tmp_path)
    ledger.reconcile_record(output, record, root, _execution_digest(), client=IndependentClient())
    saved = json.loads(record.read_bytes())
    proof = saved["retained_privacy_conflict"]
    directory = resolution.resolution_root(output, record) / sha(json_bytes(proof["binding"]))
    candidate = directory / "candidates" / proof["selected_record_sha256"]
    target = candidate.with_suffix(".preserved")
    candidate.rename(target)
    candidate.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="unsafe"):
        ledger.verify_strongest_ledger(output, record, root)
    assert all(path.read_bytes() == data for path, data in originals.items())
