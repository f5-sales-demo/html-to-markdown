"""A renewed source proof cannot authorize changed curation input or waive privacy."""

import json

import pytest
import test_baseline_source_review as fixtures
from test_curation_renewal import validation
from test_curation_renewal_publication import evidence as model_evidence

from html_to_markdown.baseline_late_validation import validate_existing_baseline
from html_to_markdown.baseline_source_review import (
    review_baseline_privacy,
    verify_baseline_prerequisite,
)
from html_to_markdown.curation import json_bytes
from html_to_markdown.curation_existing_validation import validate_existing_curation
from html_to_markdown.curation_renewal import TOPICS
from html_to_markdown.curation_renewal_dependencies import prepare_dependencies
from html_to_markdown.curation_renewal_jobs import replay_renewal, run_renewal
from html_to_markdown.curation_source_proof import current_source_evidence
from html_to_markdown.enrichment.analysis import sha


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    baseline, privacy, record, article = fixtures.prepared.__wrapped__(tmp_path, monkeypatch)
    output = tmp_path / "renewal"
    output.mkdir()
    privacy.rename(output / "dependency-privacy")
    privacy = output / "dependency-privacy"
    record = privacy / "documents" / record.name
    review_baseline_privacy(privacy, record, fixtures.Client())
    inventory = prepare_dependencies(baseline, "a" * 64, [article], output)
    context = json.loads((output / inventory["documents"][0]["context_path"]).read_bytes())
    original = record.read_bytes()
    validate_existing_baseline(privacy, record, fixtures.Client())
    assert sha(original) != sha(record.read_bytes())
    return output, privacy, record, context, original


def test_verified_renewal_composes_without_changing_old_input_or_source_proposals(prepared):
    output, privacy, record, context, original = prepared
    old_context = json_bytes(context)
    before = json.loads(original)
    evidence = current_source_evidence(output, context)
    assert evidence["baseline_source_privacy_record_sha256"] == sha(record.read_bytes())
    assert evidence["source_proof_transition"]["original_record_sha256"] == sha(original)
    assert (privacy / "history" / (sha(original) + ".json")).read_bytes() == original
    assert json_bytes(context) == old_context
    current = json.loads(record.read_bytes())
    assert current["phases"] == before["phases"]
    assert current["privacy_feedback"] == before["privacy_feedback"]
    verify_baseline_prerequisite(output, {**context, "evidence": evidence})
    assert current_source_evidence(output, context) == evidence


@pytest.mark.parametrize("field", ["body", "source_files_sha256", "metadata", "media"])
def test_changed_intermediate_cannot_be_rebound(prepared, field):
    output, _, _, context, _ = prepared
    if field == "body":
        context[field] = context[field].replace("8443", "443")
    elif field == "source_files_sha256":
        context[field] = "f" * 64
    elif field == "metadata":
        context[field]["title"] = "A different cloud procedure"
    else:
        context[field] = {"f" * 64: {}}
    with pytest.raises(ValueError, match="intermediate mismatch"):
        current_source_evidence(output, context)


def test_missing_original_history_rejects_renewal(prepared):
    output, privacy, _, context, original = prepared
    (privacy / "history" / (sha(original) + ".json")).unlink()
    with pytest.raises(ValueError, match="original source prerequisite"):
        current_source_evidence(output, context)


def test_failed_current_source_cannot_use_final_curation_approval(prepared):
    output, _, record, context, _ = prepared
    current = json.loads(record.read_bytes())
    current["privacy_validation_evidence"]["error"] = "failed independent source response"
    record.write_bytes(json_bytes(current))
    with pytest.raises(ValueError):
        current_source_evidence(output, context)


def test_changed_baseline_receipt_rejects_renewal(prepared):
    output, _, _, context, _ = prepared
    context["evidence"]["baseline_receipt_sha256"] = "f" * 64
    with pytest.raises(ValueError, match="different baseline"):
        current_source_evidence(output, context)


def test_existing_curation_selection_uses_the_verified_current_source_record(prepared):
    output, _, record, context, original = prepared

    class CurationClient:
        def __init__(self):
            self.calls = []

        def execute(self, body):
            self.calls.append(body)
            payload = json.loads(body["input"][0]["content"][0]["text"])
            if body["metadata"]["content_review_role"] == "editor":
                decision = {
                    "identity": payload["identity"],
                    "binding_sha256": payload["binding_sha256"],
                    "topics": [
                        {
                            "topic": t,
                            "disposition": "keep",
                            "reason": "current origin procedure",
                            "removals": [],
                            "media": [],
                            "reference_edits": [],
                        }
                        for t in TOPICS
                    ],
                    "unresolved": [],
                }
            else:
                decision = validation(payload, payload["candidate"])
                if "independent_review_selection" in payload:
                    addresses = {f["address"] for f in payload["source_fact_blocks"]}
                    decision["facts"] = [
                        f for f in decision["facts"] if f["source_address"] in addresses
                    ]
            evidence = model_evidence(body, decision)
            evidence.prompt_version = body["instructions"].rsplit("Prompt version: ", 1)[-1]
            return evidence

    old = run_renewal(context, [], output, CurationClient())
    client = CurationClient()
    result = validate_existing_curation(output, context["binding_sha256"], 0, client)
    assert not any(b["metadata"]["content_review_role"] == "editor" for b in client.calls)
    selected_context = json.loads((output / result["binding_sha256"] / "context.json").read_bytes())
    assert selected_context["evidence"]["baseline_source_privacy_record_sha256"] == sha(
        record.read_bytes()
    )
    assert old["candidate"]["body"] == result["candidate"]["body"]
    assert context["evidence"]["baseline_source_privacy_record_sha256"] == sha(original)
    verify_baseline_prerequisite(output, selected_context)
    assert replay_renewal(selected_context, [], output) == result
