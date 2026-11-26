"""Context renewal must preserve custody and never grant new privacy attempts."""

import copy
import json
from dataclasses import replace

import pytest
from test_cross_queue_seed_repair import failed_seed

from html_to_markdown.content_inventory import atomic_private
from html_to_markdown.cross_queue_repair_proof import _seed_wrapper_aliases
from html_to_markdown.cross_queue_seed_repair import repair_seed
from html_to_markdown.curation import json_bytes
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.source_phase_bindings import _privacy_budget

LEGACY_CONTEXT = "54292dd77740af564472e98413b1eb6a03c6b3350e2dd2420cfd7fd80d9e3de1"


def test_context_only_renewal_retains_seed_and_exhaustion():
    old = {
        "binding": {
            "identity": "example",
            "article_sha256": "a" * 64,
            "request_context_sha256": "b" * 64,
        },
        "cross_queue_evidence_blocked": True,
        "cross_queue_seed": {
            "record_sha256": "c" * 64,
            "prior_record": "day/review/documents/x.json",
        },
        "privacy_feedback": ["first", "second"],
        "privacy_exhausted": True,
    }
    current = {**old["binding"], "request_context_sha256": "d" * 64}
    retained = _privacy_budget(old, current)
    assert retained["cross_queue_seed"] == old["cross_queue_seed"]
    assert retained["privacy_feedback"] == ["first", "second"] and retained["privacy_exhausted"]
    retained["cross_queue_seed"]["prior_record"] = "changed"
    assert old["cross_queue_seed"]["prior_record"] == "day/review/documents/x.json"
    assert _privacy_budget(old, {**current, "article_sha256": "e" * 64}) == {}


def test_exhausted_block_without_feedback_cannot_reset():
    old = {
        "binding": {"identity": "example"},
        "cross_queue_evidence_blocked": True,
        "privacy_exhausted": True,
    }
    assert _privacy_budget(old, old["binding"])["privacy_exhausted"] is True


@pytest.fixture
def renewed_seed(capture, tmp_path):
    inputs, record, original_path, _ = failed_seed.__wrapped__(capture, tmp_path)
    for stale in (inputs.review / "reuse-evidence").glob("*.json"):
        stale.unlink()
    original = json.loads(original_path.read_bytes())
    original["binding"].update(
        request_context_sha256="a" * 64,
        model_policy_version=2,
        editor_model="gpt-6.1-sol",
        validator_model="gpt-6.1-sol",
    )
    original["binding_sha256"] = sha(json_bytes(original["binding"]))
    original.update(
        privacy_feedback=["first original failure", "second original failure"],
        privacy_exhausted=True,
    )
    original_raw = json_bytes(original)
    atomic_private(original_path, original_raw)
    terminal = inputs.worker / "day-2/my-f5-com/review"
    atomic_private(terminal / "reuse-evidence" / (sha(original_raw) + ".json"), original_raw)
    prior = json.loads(record.read_bytes())
    prior["binding"] = copy.deepcopy(original["binding"])
    prior["binding_sha256"] = original["binding_sha256"]
    prior["attempted_binding_sha256"] = prior["binding_sha256"]
    prior.pop("media_encoding_version", None)
    terminal_record = terminal / "documents" / record.name
    terminal_value = copy.deepcopy(prior)
    terminal_value.pop("cross_queue_seed")
    atomic_private(terminal_record, json_bytes(terminal_value))
    terminal_raw = terminal_record.read_bytes()
    atomic_private(inputs.review / "reuse-evidence" / (sha(terminal_raw) + ".json"), terminal_raw)
    prior.update(
        reason="prior_review_evidence_requires_repair",
        error_class="CrossQueueEvidenceError",
        retryable=False,
    )
    prior["cross_queue_seed"] = {
        "record_sha256": sha(terminal_raw),
        "prior_record": terminal_record.relative_to(inputs.worker).as_posix(),
    }
    prior_raw = json_bytes(prior)
    snapshot = inputs.review / "history" / (sha(prior_raw) + ".json")
    atomic_private(snapshot, prior_raw)
    binding = {**prior["binding"], "request_context_sha256": LEGACY_CONTEXT}
    digest = sha(json_bytes(binding))
    current = {
        "schema_version": 1,
        "version": "source-admission/1",
        "item": prior["item"],
        "binding": binding,
        "binding_sha256": digest,
        "phases": {},
        "disposition": "queued",
        "reason": "admission_phase_failed",
        "cross_queue_evidence_blocked": True,
        "attempted_binding_sha256": digest,
        "error_class": "ValueError",
        "retryable": False,
    }
    atomic_private(record, json_bytes(current))
    return (
        replace(inputs, current_sha256=sha(record.read_bytes()), prior_sha256=sha(original_raw)),
        record,
        original_path,
        snapshot,
    )


def test_exact_archived_context_renewal_restores_original_exhaustion(renewed_seed):
    inputs, record, original_path, snapshot = renewed_seed
    before = snapshot.read_bytes()
    original_raw = original_path.read_bytes()
    result = repair_seed(inputs)
    current = json.loads(record.read_bytes())
    assert current["privacy_feedback"] == ["first original failure", "second original failure"]
    assert current["privacy_exhausted"] and current["disposition"] == "queued"
    assert result["custody"]["context_renewals"][0]["record_sha256"] == sha(before)
    assert original_path.read_bytes() == original_raw and snapshot.read_bytes() == before
    assert not result["publication_approved"] and result["inference_calls"] == 0


@pytest.mark.parametrize(
    "mutation",
    [
        "context",
        "model",
        "source",
        "metadata",
        "feedback",
        "attempt",
        "missing",
        "digest",
        "pending",
        "ambiguous",
    ],
)
def test_unqualified_renewal_rejects_without_record_write(renewed_seed, mutation):
    inputs, record, _, snapshot = renewed_seed
    current = json.loads(record.read_bytes())
    old = json.loads(snapshot.read_bytes())
    if mutation == "missing":
        snapshot.unlink()
    elif mutation == "digest":
        atomic_private(snapshot, json_bytes({**old, "unknown": True}))
    elif mutation == "ambiguous":
        old["error_class"] = "AnotherError"
        raw = json_bytes(old)
        atomic_private(snapshot.parent / (sha(raw) + ".json"), raw)
    elif mutation == "metadata":
        old["item"]["title"] = "Changed title"
        snapshot.unlink()
        raw = json_bytes(old)
        atomic_private(snapshot.parent / (sha(raw) + ".json"), raw)
    else:
        if mutation in {"context", "model", "source"}:
            field = {
                "context": "request_context_sha256",
                "model": "validator_model",
                "source": "article_sha256",
            }[mutation]
            current["binding"][field] = "gpt-6-astra" if mutation == "model" else "f" * 64
            current["binding_sha256"] = sha(json_bytes(current["binding"]))
            current["attempted_binding_sha256"] = current["binding_sha256"]
        elif mutation == "feedback":
            current["privacy_feedback"] = ["invented"]
        elif mutation == "pending":
            current["boundary_pending"] = {"privacy": {"pending": True}}
        else:
            current["attempted_binding_sha256"] = "f" * 64
        atomic_private(record, json_bytes(current))
        inputs = replace(inputs, current_sha256=sha(record.read_bytes()))
    before = record.read_bytes()
    with pytest.raises((ValueError, FileNotFoundError)):
        repair_seed(inputs)
    assert record.read_bytes() == before


def test_exact_seed_wrapper_requires_both_immutable_custody_paths():
    source = {"cross_queue_evidence_blocked": True, "binding": {"identity": "example"}}
    source_pin = sha(json_bytes(source))
    prior = "day-1/source/review/documents/x.json"
    seed = {"record_sha256": source_pin, "prior_record": prior}
    wrapper = {**source, "cross_queue_seed": seed}
    pin = sha(json_bytes(wrapper))
    path = "day-2/source/review/history/" + pin + ".json"
    snapshot = "day-2/source/review/reuse-evidence/" + source_pin + ".json"
    records = {source_pin: source, pin: wrapper}
    rows = [{"path": path, "record_sha256": pin}]
    inspected = {prior: source_pin, snapshot: source_pin}
    aliases = _seed_wrapper_aliases(records, {source_pin}, rows, inspected)
    assert aliases[0]["historical_sha256"] == pin
    assert aliases[0]["custody_snapshot"] == snapshot
    assert not _seed_wrapper_aliases(records, {source_pin}, rows, {prior: source_pin})
    assert not _seed_wrapper_aliases(records, {source_pin}, rows, {snapshot: source_pin})
    records[pin] = {**wrapper, "privacy_feedback": ["invented"]}
    assert not _seed_wrapper_aliases(records, {source_pin}, rows, inspected)
