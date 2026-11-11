"""Version-two retries preserve attempts and resume complete source proposals."""

import json
from pathlib import Path

import pytest

from html_to_markdown.content_policy import load_content_policy
from html_to_markdown.curation import corpus_digest, json_bytes, load_curation_policy
from html_to_markdown.enrichment.analysis import analyze, sha
from html_to_markdown.enrichment.contracts import Artifact, DocumentEvidence, ResponseEvidence
from html_to_markdown.enrichment.engine import load_artifact
from html_to_markdown.enrichment.gates import request_hash
from html_to_markdown.enrichment.generate_v2 import generate_v2
from html_to_markdown.enrichment.v2 import replay_document
from html_to_markdown.models import PageMetadata
from html_to_markdown.render import serialize_document


class ModelClient:
    calls: list[str] = []
    validations = 0
    fail_until = 0

    def __init__(self, *args: object, **kwargs: object):
        pass

    def close(self) -> None:
        pass

    def invalidate(self, evidence: ResponseEvidence) -> None:
        pass

    def execute(self, body: dict) -> ResponseEvidence:
        phase = body["text"]["format"]["name"]
        self.calls.append(phase)
        data = json.loads(body["input"][0]["content"][0]["text"])
        doc = data["document"]
        identity = doc["document"] if isinstance(doc, dict) else doc
        digest = doc["input_sha256"] if isinstance(doc, dict) else data["input_sha256"]
        result = {"document": identity, "input_sha256": digest}
        if phase == "classifiedsection":
            result.update(
                section=data["section"]["id"],
                classifications=[
                    {
                        "address": block["address"],
                        "source_sha256": block["sha256"],
                        "classification": "article_explanation",
                        "evidence_addresses": [block["address"]],
                        "reason": "necessary guidance",
                        "uncertain": False,
                    }
                    for block in data["section"]["blocks"]
                ],
            )
        elif phase == "editedsection":
            result.update(
                section=data["section"]["id"],
                edits=[],
                reason="preserve complete content",
                unresolved=[],
            )
        elif phase == "documentdecisionv2":
            result.update(
                disposition="retain",
                canonical_document=None,
                description="Configure the service with the required timeout.",
                description_evidence=[doc["blocks"][0]["address"]],
                reason="retain complete procedure",
                media=[],
                privacy_findings=[],
                unresolved=[],
            )
        else:
            type(self).validations += 1
            passed = self.validations > self.fail_until
            targets = {block["address"]: block for block in data["candidate_blocks"]}
            result.update(
                candidate_sha256=data["candidate_sha256"],
                passed=passed,
                disposition_valid=True,
                description_grounded=True,
                coherent_standalone=True,
                action_order_preserved=True,
                all_necessary_facts_preserved=passed,
                media_valid=True,
                classifications_complete=True,
                privacy_valid=True,
                approved_safe_privacy_findings=[],
                protected_facts=[
                    {
                        "source_address": block["address"],
                        "source_sha256": block["sha256"],
                        "candidate_addresses": [block["address"]],
                        "candidate_sha256": [targets[block["address"]]["sha256"]],
                        "necessary": True,
                        "preserved": passed,
                        "kind": "fact",
                    }
                    for block in doc["blocks"]
                ],
                unsupported_claims=[],
                failures=[] if passed else ["preservation feedback for section:0"],
                unresolved=[],
            )
        response = {
            "status": "completed",
            "model": body["model"],
            "usage": {"input_tokens": 100, "output_tokens": 10},
            "output": [{"content": [{"type": "output_text", "text": json.dumps(result)}]}],
        }
        return ResponseEvidence(
            request_sha256=request_hash(body),
            response_sha256=sha(json.dumps(response, sort_keys=True, separators=(",", ":"))),
            model=body["model"],
            reasoning="high",
            prompt_version="article-enrichment/2",
            request=body,
            response=response,
            error=None,
            transport="codex_litellm",
            runtime_version="test",
        )


def fixture(tmp_path: Path) -> tuple[Path, Path]:
    corpus = tmp_path / "corpus"
    path = corpus / "content/my-f5-com/K000123456/index.md"
    path.parent.mkdir(parents=True)
    metadata = PageMetadata(
        sourceId="my-f5-com",
        title="Configure",
        slug="k000123456",
        url="https://my.f5.com/manage/s/article/K000123456",
        category="knowledge",
    )
    path.write_text(
        serialize_document(
            metadata, "# Configure\n\nUse a 30 second timeout and run `curl --retry 3`.\n"
        )
    )
    (corpus / "curation-audit.json").write_text("{}")
    analysis = analyze(corpus)
    artifact = Artifact(
        schema_version=2,
        prompt_version="article-enrichment/2",
        baseline_tag="content-20261110T000000Z",
        publication_sha256="a" * 64,
        baseline_manifest_sha256="b" * 64,
        curated_sha256=corpus_digest(corpus),
        analysis_sha256=sha(json_bytes(analysis)),
        policy_sha256=load_curation_policy().sha256,
        api_policy_sha256=load_content_policy().policy_digest,
        curation_audit_sha256=sha(b"{}"),
        documents=[
            DocumentEvidence(
                document=doc["document"],
                source="my-f5-com",
                input_sha256=doc["input_sha256"],
                editor=None,
                validator=None,
            )
            for doc in analysis["documents"]
        ],
        assets={},
        images={},
        repairs={},
        pricing={},
    )
    destination = tmp_path / "decisions.json"
    destination.write_bytes(json_bytes(artifact.model_dump()))
    return corpus, destination


def test_validator_feedback_has_two_corrected_proposals_then_offline_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus, destination = fixture(tmp_path)
    monkeypatch.setattr("html_to_markdown.enrichment.generate_v2.CodexClient", ModelClient)
    monkeypatch.setattr(ModelClient, "calls", [])
    monkeypatch.setattr(ModelClient, "validations", 0)
    monkeypatch.setattr(ModelClient, "fail_until", 2)
    result = generate_v2(corpus, destination, phase="all", journal=tmp_path / "journal")
    assert result["validated"] == 1
    progress = load_artifact(destination).documents[0].v2
    assert progress is not None and len(progress["attempts"]) == 3
    assert progress["selected_attempt"] == 2
    assert "preservation feedback" in progress["attempts"][1]["feedback"][0]
    docs = analyze(corpus)["documents"]
    _, body, validated = replay_document(
        docs[0], progress["attempts"][2], {doc["document"]: doc for doc in docs}
    )
    assert body == docs[0]["body"] and validated.passed
    before = len(ModelClient.calls)
    generate_v2(corpus, destination, phase="all", journal=tmp_path / "journal")
    assert len(ModelClient.calls) == before


def test_complete_editor_phase_resumes_without_repeating_model_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus, destination = fixture(tmp_path)
    monkeypatch.setattr("html_to_markdown.enrichment.generate_v2.CodexClient", ModelClient)
    monkeypatch.setattr(ModelClient, "calls", [])
    monkeypatch.setattr(ModelClient, "validations", 0)
    generate_v2(corpus, destination, phase="editor", journal=tmp_path / "journal")
    assert len(ModelClient.calls) == 3
    result = generate_v2(corpus, destination, phase="validator", journal=tmp_path / "journal")
    assert result["validated"] == 1 and len(ModelClient.calls) == 4
    cached = load_artifact(destination)
    cached.policy_sha256 = "tampered"
    assert load_artifact(destination).policy_sha256 != "tampered"
