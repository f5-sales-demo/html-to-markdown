"""Catalog promotion requires real planner replay and independently protected facts."""

import json
import shutil
from pathlib import Path

import pytest
from test_curation_renewal import validation
from test_source_refresh import prepare

from html_to_markdown.curation import blocks, json_bytes, load_curation_policy
from html_to_markdown.curation_renewal import TOPICS, proposal_context
from html_to_markdown.curation_renewal_jobs import run_renewal
from html_to_markdown.curation_renewal_policy import qualify_policy, reviewed_policy
from html_to_markdown.curation_renewal_publication import (
    PlannerValidation,
    stage_policy,
    validate_planner,
    verify_planner_validation,
)
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.enrichment.contracts import ResponseEvidence
from html_to_markdown.enrichment.gates import request_hash
from html_to_markdown.models import PageMetadata
from html_to_markdown.render import serialize_document, split_document


def evidence(body, value):
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
        prompt_version="curation-renewal/1",
        request=body,
        response=response,
        error=None,
        transport="codex_litellm",
    )


class Client:
    def __init__(self, source, proposal):
        self.source, self.proposal = source, proposal
        self.calls = []
        self.reject = False

    def execute(self, body):
        self.calls.append(body)
        payload = json.loads(body["input"][0]["content"][0]["text"])
        if body["model"] == "gpt-6.1-sol":
            return evidence(body, self.proposal)
        if "qualification" not in payload:
            return evidence(body, validation(self.source, payload["candidate"]))
        candidate = payload["candidate"]
        actual = {"body": candidate["body"], "output_sha256": sha(json_bytes(candidate))}
        decision = validation(self.source, actual)
        decision.update(
            qualification_sha256=payload["qualification"]["qualification_sha256"],
            action_order_preserved=True,
            coherent_standalone=True,
            safe_findings=[],
        )
        if self.reject:
            decision.update(approved=False, failures=["necessary prerequisite lost"])
        return evidence(body, decision)


def prepared(tmp_path):
    baseline, _, pin = prepare(tmp_path)
    metadata = PageMetadata(
        sourceId="my-f5-com",
        title="Cloud services",
        slug="cloud-services",
        category="knowledge",
        url="https://my.f5.com/manage/s/article/K000123456",
    )
    article = serialize_document(
        metadata,
        "# Cloud services\n\nOrchestrate workloads with independent origin pools.\n\nConfigure the origin pool first. Expect healthy origins within 30 seconds.\n\n```sh\ncurl --fail https://example.com/health\n```\n",
    )
    fields, body = split_document(article)
    relative = "articles/content/my-f5-com/K000123456/index.md"
    source = proposal_context(
        fields, body, {}, sha(json_bytes({relative: sha(article)})), load_curation_policy().sha256
    )
    proposal = {
        "identity": source["identity"],
        "binding_sha256": source["binding_sha256"],
        "topics": [
            {
                "topic": topic,
                "disposition": "keep",
                "reason": "independent current cloud explanation",
                "removals": [],
                "media": [],
                "reference_edits": [],
            }
            for topic in TOPICS
        ],
        "unresolved": [],
    }
    output = tmp_path / "renewals"
    directory = output / source["binding_sha256"]
    target = directory / "inputs" / relative
    target.parent.mkdir(parents=True)
    target.write_text(article)
    (directory / "visual.json").write_bytes(json_bytes([]))
    client = Client(source, proposal)
    result = run_renewal(source, [], output, client)
    return output, source, result, baseline, pin, client


def test_actual_planner_replay_and_catalog_pins_are_deterministic(tmp_path):
    output, source, _, baseline, pin, client = prepared(tmp_path)
    result = validate_planner(output, [source], baseline, pin, client)
    assert result["post_planner_validated"] and result["enforcement_findings"] == 0
    assert len(client.calls) == 3
    assert validate_planner(output, [source], baseline, pin) == result
    destination = tmp_path / "staged"
    source_dir = Path(__file__).parents[1] / "src/html_to_markdown"
    shutil.copytree(source_dir, destination)
    receipt = stage_policy(output, [source], baseline, pin, destination)
    first = {
        name: (destination / name).read_bytes()
        for name in [*receipt["files"], "curation-renewal-receipt.json"]
    }
    assert stage_policy(output, [source], baseline, pin, destination) == receipt
    assert all((destination / name).read_bytes() == data for name, data in first.items())
    staged = load_curation_policy(destination / "curation_policy.json")
    assert staged.sha256 == result["policy_sha256"] == reviewed_policy(output, [source]).sha256
    assert all(topic["version"].endswith(".1") for topic in staged.topics)
    # Complete source content and model prose never enter the public receipt/catalog.
    assert all(
        b"curl --fail" not in data and b"Expect healthy origins" not in data
        for data in first.values()
    )


def test_policy_cannot_stage_without_actual_independent_planner_approval(tmp_path):
    output, source, _, baseline, pin, client = prepared(tmp_path)
    destination = tmp_path / "public"
    with pytest.raises(OSError):
        stage_policy(output, [source], baseline, pin, destination)
    assert not destination.exists()
    client.reject = True
    with pytest.raises(ValueError, match="rejected"):
        validate_planner(output, [source], baseline, pin, client)
    assert not destination.exists()


@pytest.mark.parametrize("change", ["source", "baseline", "policy", "duplicate"])
def test_changed_or_superseded_policy_inputs_fail_before_promotion(tmp_path, change):
    output, source, _, baseline, pin, _ = prepared(tmp_path)
    contexts = [source]
    if change == "source":
        target = next((output / source["binding_sha256"] / "inputs").rglob("index.md"))
        target.write_text(target.read_text().replace("30 seconds", "60 seconds"))
    elif change == "baseline":
        pin = "f" * 64
    elif change == "policy":
        source["policy_sha256"] = "f" * 64
    else:
        contexts *= 2
    with pytest.raises(ValueError):
        qualify_policy(output, contexts, baseline, pin)


@pytest.mark.parametrize(
    "change", ["necessary", "payload", "privacy", "credential", "digest", "coverage"]
)
def test_actual_planner_cannot_waive_protected_facts_or_privacy(tmp_path, change):
    output, source, reviewed, baseline, pin, client = prepared(tmp_path)
    validate_planner(output, [source], baseline, pin, client)
    qualification = json.loads((output / "planner-qualification.json").read_bytes())
    candidate = next(iter(qualification["candidates"].values()))
    parsed = json.loads(client.calls[-1]["input"][0]["content"][0]["text"])
    good = Client(source, client.proposal).execute(client.calls[-1])
    decision = json.loads(good.response["output"][0]["content"][0]["text"])
    if change == "necessary":
        decision["facts"][0]["necessary"] = False
    elif change == "payload":
        candidate["body"] = candidate["body"].replace("30 seconds", "60 seconds")
        retained = {b.sha256: b for b in blocks(candidate["body"])}
        for fact in decision["facts"]:
            if fact["source_sha256"] not in retained:
                replacement = next(
                    b
                    for b in blocks(candidate["body"])
                    if "60 seconds" in b.text and b.kind == "paragraph"
                )
                fact.update(
                    candidate_addresses=[replacement.location],
                    candidate_sha256=[replacement.sha256],
                )
        decision["output_sha256"] = sha(json_bytes(candidate))
    elif change in {"privacy", "credential"}:
        candidate["findings"] = [
            {"id": "a" * 64, "kind": "credential" if change == "credential" else "email"}
        ]
        decision["safe_findings"] = [
            {
                "finding_id": "a" * 64,
                "disposition": "documentation_example" if change == "credential" else "unresolved",
                "reason": "cannot waive final enforcement",
            }
        ]
        decision["output_sha256"] = sha(json_bytes(candidate))
    elif change == "digest":
        decision["qualification_sha256"] = "f" * 64
    else:
        decision["facts"].pop()
    with pytest.raises(ValueError):
        verify_planner_validation(
            source, reviewed, qualification, candidate, PlannerValidation.model_validate(decision)
        )
    assert "candidates" not in parsed["qualification"]


def test_offline_actual_planner_replay_rejects_changed_model_evidence(tmp_path):
    output, source, _, baseline, pin, client = prepared(tmp_path)
    validate_planner(output, [source], baseline, pin, client)
    path = next((output / source["binding_sha256"] / "planner").glob("*.json"))
    response = json.loads(path.read_bytes())
    response["response"]["model"] = "gpt-6.1-sol"
    path.write_bytes(json_bytes(response))
    with pytest.raises(ValueError, match="digest"):
        validate_planner(output, [source], baseline, pin)
