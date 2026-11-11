"""New scanner findings require explicit classification and independent admission."""

import json
from copy import deepcopy
from pathlib import Path

import pytest
from test_source_review_jobs import LocalClient

from html_to_markdown.content_inventory import atomic_private
from html_to_markdown.curation import json_bytes
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.enrichment.contracts import ResponseEvidence
from html_to_markdown.enrichment.gates import request_hash
from html_to_markdown.models import PageMetadata
from html_to_markdown.render import serialize_document
from html_to_markdown.source_review import PrivacyDecision
from html_to_markdown.source_review_jobs import prepare_reviews, run_reviews, verify_reviews
from html_to_markdown.source_review_reconciliation import (
    FinalClassification,
    check_classification,
    final_request,
    guard_label_quotes,
)


def test_final_classification_is_bound_to_complete_candidate_and_every_finding():
    context = {"identity": "article", "binding_sha256": "a" * 64, "body": "original"}
    candidate = {
        "body": "final",
        "metadata": {"title": "source"},
        "media": {},
        "findings": [{"id": "f", "kind": "cloud-account"}],
    }
    body = final_request(context, candidate, [])
    payload = json.loads(body["input"][0]["content"][0]["text"])
    assert payload["source"] == context and payload["candidate"] == candidate
    classification = FinalClassification(
        identity="article",
        binding_sha256="a" * 64,
        candidate_sha256=sha(json_bytes(candidate)),
        findings=[
            {
                "finding_id": "f",
                "disposition": "documentation_example",
                "reason": "The match occurs inside the guarded synthetic site UUID.",
            }
        ],
    )
    check_classification(context, candidate, classification)
    for field in ["body", "metadata", "media", "findings"]:
        changed = deepcopy(candidate)
        changed[field] = "changed"
        with pytest.raises(ValueError, match="stale"):
            check_classification(context, changed, classification)
    with pytest.raises(ValueError, match="coverage"):
        check_classification(context, candidate, classification.model_copy(update={"findings": []}))
    with pytest.raises(ValueError, match="coverage"):
        check_classification(
            context,
            candidate,
            classification.model_copy(
                update={"findings": [classification.findings[0], classification.findings[0]]}
            ),
        )


@pytest.mark.parametrize(
    "kind", ["credential", "private-key", "authorization-header", "session-cookie"]
)
def test_final_classification_never_approves_credentials(kind):
    context = {"identity": "article", "binding_sha256": "a" * 64}
    candidate = {"findings": [{"id": "f", "kind": kind}]}
    classification = FinalClassification(
        identity="article",
        binding_sha256="a" * 64,
        candidate_sha256=sha(json_bytes(candidate)),
        findings=[
            {
                "finding_id": "f",
                "disposition": "documentation_example",
                "reason": "incorrect waiver",
            }
        ],
    )
    with pytest.raises(ValueError, match="credential enforcement"):
        check_classification(context, candidate, classification)


@pytest.mark.parametrize("approved", [True, False])
def test_synthetic_uuid_finding_needs_fresh_classification_and_independent_validation(
    capture: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    approved: bool,
):
    inventory = json.loads((capture / "inventory.json").read_bytes())
    item = inventory["documents"][0]
    original_body = "# Configure service\n\nRun `curl --retry 3` within 30 seconds.\n\nSite name: SOURCE_SITE_A.\n"
    article = serialize_document(
        PageMetadata.model_validate(item["metadata"]), original_body
    ).encode()
    atomic_private(capture / item["article_path"], article)
    item["article_sha256"] = sha(article)
    atomic_private(capture / "inventory.json", json_bytes(inventory))
    calls = []
    invalidated = []

    def response(body, output, version="source-admission/1"):
        raw = {
            "status": "completed",
            "model": body["model"],
            "output": [{"content": [{"type": "output_text", "text": json.dumps(output)}]}],
        }
        return ResponseEvidence(
            request_sha256=request_hash(body),
            response_sha256=sha(json.dumps(raw, sort_keys=True, separators=(",", ":"))),
            model=body["model"],
            reasoning="high",
            prompt_version=version,
            request=body,
            response=raw,
            error=None,
            transport="codex_litellm",
            runtime_version="test",
        )

    class FindingClient(LocalClient):
        def invalidate(self, evidence):
            invalidated.append(evidence.request_sha256)

        def execute(self, body):
            phase = body["text"]["format"]["name"]
            calls.append(phase)
            payload = json.loads(body["input"][0]["content"][0]["text"])
            if phase == "finalclassification":
                assert payload["candidate"]["finding_decisions"][0]["disposition"] == "unresolved"
                assert {f["kind"] for f in payload["candidate"]["findings"]} == {
                    "cloud-account",
                    "uuid",
                }
                return response(
                    body,
                    {
                        "identity": payload["source"]["identity"],
                        "binding_sha256": payload["source"]["binding_sha256"],
                        "candidate_sha256": payload["candidate_sha256"],
                        "findings": [
                            {
                                "finding_id": f["id"],
                                "disposition": "documentation_example",
                                "reason": "Digit sequence occurs within the guarded synthetic site UUID.",
                            }
                            for f in payload["candidate"]["findings"]
                        ],
                    },
                    "final-privacy-findings/1",
                )
            existing = super().execute(body)
            output = json.loads(existing.response["output"][0]["content"][0]["text"])
            if phase == "privacydecision":
                value = "SOURCE_SITE_A"
                replacement = "00000000-0000-4000-8000-000000000001"
                start = payload["body"].index(value)
                output.update(
                    values=[{"source_sha256": sha(value), "replacement": replacement}],
                    text_changes=[
                        {
                            "field": "body",
                            "start": start,
                            "end": start + len(value),
                            "source_sha256": sha(value),
                            "replacement": replacement,
                            "reason": "Use a consistent synthetic site identity.",
                        }
                    ],
                )
            elif phase == "admissionvalidation":
                assert payload["candidate"]["finding_classification"]["request_sha256"]
                output.update(
                    approved=approved,
                    privacy_valid=approved,
                    approved_safe_findings=[f["id"] for f in payload["candidate"]["findings"]],
                    failures=[] if approved else ["Identifying value is not a safe example."],
                )
            return response(body, output)

    monkeypatch.setattr("html_to_markdown.source_review_jobs.CodexClient", FindingClient)
    output = tmp_path / "review"
    prepare_reviews(capture, output)
    report = run_reviews(capture, output, phase="all", journal=tmp_path / "journal")
    assert report["documents"][0]["disposition"] == ("accepted" if approved else "queued")
    expected_calls = ["relevancedecision"] + [
        "privacydecision",
        "finalclassification",
        "admissionvalidation",
    ] * (1 if approved else 3)
    assert calls == expected_calls
    assert len(invalidated) == (0 if approved else 3)
    assert len(set(invalidated)) == len(invalidated)
    verified = verify_reviews(capture, output)
    assert bool(verified["accepted_files"]) is approved
    if approved:
        record = json.loads(next((output / "documents").glob("*.json")).read_bytes())
        assert "privacy_reconciliation" in record["phases"]
        assert "curl --retry 3" in next((output / "reviewed").rglob("*.md")).read_text()
        assert calls == expected_calls
        record_path = next((output / "documents").glob("*.json"))
        record["phases"].pop("privacy_reconciliation")
        atomic_private(record_path, json_bytes(record))
        with pytest.raises(ValueError, match="new final privacy findings"):
            verify_reviews(capture, output)
        assert calls == expected_calls


def quoted_proposal(text, guard):
    return PrivacyDecision(
        identity="source",
        binding_sha256="b" * 64,
        findings=[],
        values=[{"source_sha256": guard, "replacement": text}],
        text_changes=[],
        media_changes=[
            {
                "source_sha256": "a" * 64,
                "rectangles": [[512, 752, 666, 763]],
                "labels": [
                    {
                        "rectangle": [512, 752, 666, 763],
                        "source_value_sha256": guard,
                        "text": text,
                        "size": 10,
                    }
                ],
                "frames": [],
                "strip_metadata": False,
                "reason": "Replace the exact identifying MAC value, preserving JSON punctuation.",
            }
        ],
        unresolved=[],
    )


def test_quote_labels_need_the_same_literal_punctuation_and_exact_native_placement():
    original = chr(34) + "02:00:00:00:00:01" + chr(34)
    text = chr(34) + "02:00:00:00:00:02" + chr(34)
    observation = {
        "media_sha256": "a" * 64,
        "frame": 0,
        "rectangle": [512, 752, 666, 763],
        "value": original,
        "value_sha256": sha(original),
    }
    proposal = quoted_proposal(text, sha(original))
    guard_label_quotes({"confirmed_media_values": [observation]}, proposal)
    for field, value in [
        ("value", None),
        ("value", original.strip(chr(34))),
        ("rectangle", [513, 752, 666, 763]),
        ("frame", 1),
        ("media_sha256", "c" * 64),
        ("value_sha256", "d" * 64),
    ]:
        changed = {**observation, field: value}
        with pytest.raises(ValueError, match="unguarded quote punctuation"):
            guard_label_quotes({"confirmed_media_values": [changed]}, proposal)
    with pytest.raises(ValueError, match="unguarded quote punctuation"):
        guard_label_quotes({}, proposal)
    with pytest.raises(ValueError, match="unguarded quote punctuation"):
        guard_label_quotes(
            {"confirmed_media_values": [observation]},
            quoted_proposal(chr(34) + text, sha(original)),
        )


def test_clipped_mac_region_does_not_authorize_an_added_json_quote():
    guard = "b" * 64
    null_region = {
        "media_sha256": "a" * 64,
        "frame": 0,
        "rectangle": [512, 752, 666, 763],
        "value": None,
        "value_sha256": guard,
    }
    context = {"confirmed_media_values": [null_region]}
    with pytest.raises(ValueError, match="Null/region guards cannot authorize quotes"):
        guard_label_quotes(context, quoted_proposal(chr(34) + "02:00:00:00:00:02" + chr(34), guard))
    guard_label_quotes(context, quoted_proposal("02:00:00:00:00:02", guard))
