"""Retained baseline privacy is explicit, independently checked and never publication approval."""

import io
import json

import pytest
from PIL import Image

from html_to_markdown import baseline_source_review as source
from html_to_markdown import source_review_jobs as jobs
from html_to_markdown import source_review_reconciliation
from html_to_markdown.curation import json_bytes
from html_to_markdown.curation_renewal_dependencies import prepare_dependencies
from html_to_markdown.curation_renewal_jobs import TransportFailure
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.enrichment.contracts import ResponseEvidence
from html_to_markdown.enrichment.gates import request_hash
from html_to_markdown.models import PageMetadata
from html_to_markdown.render import serialize_document
from html_to_markdown.source_phase_bindings import privacy_context
from html_to_markdown.source_review_jobs import _compile_admission, _run_record


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    metadata = PageMetadata(
        sourceId="docs-cloud-f5-com",
        title="Cloud origin health",
        slug="origin-health",
        category="Guides",
        url="https://docs.cloud.f5.com/docs-v2/how-to/origin-health",
    )
    article = "content/docs-cloud-f5-com/how-to/origin-health/index.md"
    data = serialize_document(
        metadata,
        "# Cloud origin health\n\nConfigure the origin pool first. Use port 8443 and wait 30 seconds.\n",
    ).encode()
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    (baseline / "publication.json").write_text(json.dumps({"published_at": "2026-11-10T19:49:38Z"}))
    monkeypatch.setattr(source, "_verified_baseline", lambda *args: {article: data})
    output = tmp_path / "privacy"
    inventory = source.prepare_baseline_reviews(baseline, "a" * 64, [article], output)
    return baseline, output, output / inventory["documents"][0]["path"], article


def test_relevance_reconciliation_changes_request_binding_preserves_budget_and_rejects_tampering(
    prepared,
):
    baseline, output, record, article = prepared
    before = json.loads(record.read_bytes())
    before["privacy_feedback"] = ["first correction", "second correction"]
    record.write_bytes(json_bytes(before))
    path = output / "relevance-evidence" / (sha(article) + ".json")
    path.parent.mkdir()
    evidence = {
        "baseline_article": article,
        "baseline_article_sha256": before["binding"]["baseline_article_sha256"],
        "baseline_receipt_sha256": "a" * 64,
        "prior_necessary_facts": ["Configure the origin pool first"],
        "prior_dispositions": ["include", "exclude"],
    }
    path.write_bytes(json_bytes(evidence))
    source.prepare_baseline_reviews(baseline, "a" * 64, [article], output)
    after = json.loads(record.read_bytes())
    assert after["binding_sha256"] != before["binding_sha256"]
    assert after["privacy_feedback"] == before["privacy_feedback"]
    context, _ = source.baseline_context(output / after["source"], output, after)
    assert context["prior_relevance_evidence"] == evidence
    path.write_bytes(json_bytes({**evidence, "prior_dispositions": ["include"]}))
    with pytest.raises(ValueError, match="evidence changed"):
        source.baseline_context(output / after["source"], output, after)


class Client:
    def __init__(
        self,
        reject=False,
        approve_publication=False,
        unresolved=None,
        fail_check=None,
        require_phase_link=False,
    ):
        self.reject = reject
        self.approve_publication = approve_publication
        self.unresolved = unresolved or []
        self.fail_check = fail_check
        self.require_phase_link = require_phase_link
        self.tamper_phase = None
        self.calls = []

    def invalidate(self, response):
        pass

    def execute(self, body):
        self.calls.append(body)
        payload = json.loads(body["input"][0]["content"][0]["text"])
        value = {"identity": payload["identity"], "binding_sha256": payload["binding_sha256"]}
        kind = body["text"]["format"]["name"]
        if kind == "relevancedecision":
            value.update(
                disposition="include",
                extraction="complete",
                retirement="independent",
                reason="Complete relevant archived cloud procedure",
                evidence_addresses=["b:1"],
            )
        elif kind == "privacydecision":
            value.update(findings=[], values=[], text_changes=[], media_changes=[], unresolved=[])
        elif kind == "mediaobservations":
            value.update(observations=[], complete_media_reviewed=True, unresolved=[])
        elif kind == "observationvalidation":
            value.update(
                observations_sha256=payload["observations"]["observations_sha256"],
                approved_indices=[],
                complete_media_reviewed=True,
                failures=[],
                unresolved=[],
            )
        else:
            assert kind == "admissionvalidation"
            assert (
                payload["source_article_html"] == ""
                and payload["binding"]["input_kind"] == "retained_verified_baseline"
            )
            value.update(
                output_sha256=payload["candidate"]["output_sha256"],
                approved=self.approve_publication,
                retirement_valid=self.approve_publication,
                relevance_valid=True,
                privacy_valid=not self.reject,
                extraction_valid=True,
                technical_relationships_preserved=True,
                media_valid=True,
                complete_document_reviewed=True,
                complete_media_reviewed=True,
                approved_safe_findings=[],
                failures=["unsafe candidate"] if self.reject else [],
                unresolved=self.unresolved,
            )
            if self.fail_check:
                value[self.fail_check] = False
            if self.require_phase_link:
                links = payload.get("phase_bindings", {})
                private = links.get("source_privacy", {})
                linked = (
                    private.get("sha256") == payload["privacy_decision"]["binding_sha256"]
                    and private.get("sha256") == sha(json_bytes(private.get("binding", {})))
                    and links.get("baseline_review_sha256") == payload["binding_sha256"]
                    and links.get("relevance_sha256")
                    == payload["relevance_decision"]["binding_sha256"]
                )
                if not linked:
                    value.update(
                        privacy_valid=False,
                        failures=[
                            "Missing immutable source-privacy to baseline-review binding link"
                        ],
                        unresolved=["Privacy decision applicability remains unverified"],
                    )
        if kind == "privacydecision" and self.tamper_phase == "privacy_binding":
            value["binding_sha256"] = "f" * 64
        elif kind == "privacydecision" and self.tamper_phase == "privacy_identity":
            value["identity"] = "https://docs.cloud.f5.com/docs-v2/other"
        elif kind == "relevancedecision" and self.tamper_phase == "relevance_binding":
            value["binding_sha256"] = "f" * 64
        elif kind == "relevancedecision" and self.tamper_phase == "relevance_identity":
            value["identity"] = "https://docs.cloud.f5.com/docs-v2/other"
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


def test_baseline_requires_independent_privacy_and_preserves_source_dates(prepared):
    _, output, record, _ = prepared
    initial = json.loads(record.read_bytes())
    assert initial["status"] == "privacy_review_required" and not initial["fresh_capture"]
    assert initial["source_published_at"] == "2026-11-10T19:49:38Z"
    client = Client()
    result = source.review_baseline_privacy(output, record, client)
    assert result["status"] == "privacy_only_validated" and result["retirement_review_required"]
    assert result["enforcement_findings"] == 0
    assert "Use port 8443 and wait 30 seconds." in result["candidate"]["body"]
    assert len(client.calls) == 3
    before = record.read_bytes()
    assert source.review_baseline_privacy(output, record) == result
    assert record.read_bytes() == before


def test_privacy_validator_contract_identifies_downstream_checks_as_separate(prepared):
    _, output, record, _ = prepared
    client = Client()
    source.review_baseline_privacy(output, record, client)
    request = client.calls[-1]
    properties = request["text"]["format"]["schema"]["properties"]
    assert properties["approved"]["enum"] == [False]
    assert properties["retirement_valid"]["enum"] == [False]
    for name in ("failures", "unresolved"):
        assert "privacy-only" in properties[name]["description"]
        assert "retirement" in properties[name]["description"]
    assert "failures=[] and unresolved=[]" in request["input"][0]["content"][0]["text"] or (
        "failures=[] and unresolved=[]" in request["instructions"]
    )


def test_baseline_validator_receives_and_checks_distinct_phase_bindings(prepared):
    _, output, record, _ = prepared
    client = Client(require_phase_link=True)
    result = source.review_baseline_privacy(output, record, client)
    assert result["status"] == "privacy_only_validated"
    payload = json.loads(client.calls[-1]["input"][0]["content"][0]["text"])
    links = payload["phase_bindings"]
    private = links["source_privacy"]
    expected = {
        k: v
        for k, v in payload["binding"].items()
        if k not in {"curation_policy_sha256", "api_policy_sha256"}
    }
    expected["privacy_version"] = "source-privacy/2"
    assert private["binding"] == expected
    assert private["sha256"] == sha(json_bytes(expected))
    assert private["sha256"] != payload["binding_sha256"]
    assert links["baseline_review_sha256"] == links["relevance_sha256"]
    before = record.read_bytes()
    assert source.review_baseline_privacy(output, record) == result
    assert record.read_bytes() == before


@pytest.mark.parametrize(
    "field", ["privacy_binding", "privacy_identity", "relevance_binding", "relevance_identity"]
)
def test_baseline_validator_rejects_stale_phase_data_before_inference(prepared, field):
    _, output, record, _ = prepared
    client = Client(require_phase_link=True)
    client.tamper_phase = field
    with pytest.raises(ValueError, match="exhausted"):
        source.review_baseline_privacy(output, record, client)
    saved = json.loads(record.read_bytes())
    assert saved["status"] == "privacy_review_required"
    assert all("stale" in feedback for feedback in saved["privacy_feedback"])
    assert not (output / "reviewed").exists()
    assert all(c["text"]["format"]["name"] != "admissionvalidation" for c in client.calls)


def test_pending_retirement_response_is_preserved_and_never_silently_accepted(prepared):
    _, output, record, _ = prepared
    unresolved = ["Mandatory retirement/API curation remains pending."]
    with pytest.raises(ValueError, match="exhausted"):
        source.review_baseline_privacy(output, record, Client(unresolved=unresolved))
    saved = json.loads(record.read_bytes())
    assert saved["status"] == "privacy_review_required"
    assert len(saved["privacy_feedback"]) == 3
    assert all(unresolved[0] in feedback for feedback in saved["privacy_feedback"])
    assert not (output / "reviewed").exists()
    assert len(list((output / "attempts" / record.stem).glob("attempt-*/validator.json"))) == 3


@pytest.mark.parametrize(
    "check",
    [
        "relevance_valid",
        "privacy_valid",
        "extraction_valid",
        "technical_relationships_preserved",
        "media_valid",
        "complete_document_reviewed",
        "complete_media_reviewed",
    ],
)
def test_each_in_scope_failure_blocks_privacy_intermediate_with_precise_feedback(prepared, check):
    _, output, record, _ = prepared
    with pytest.raises(ValueError, match="exhausted"):
        source.review_baseline_privacy(output, record, Client(fail_check=check))
    saved = json.loads(record.read_bytes())
    assert saved["status"] == "privacy_review_required"
    assert all(check in feedback for feedback in saved["privacy_feedback"])
    assert not (output / "reviewed").exists()


@pytest.mark.parametrize("case", ["privacy", "publication"])
def test_failed_privacy_or_invented_publication_approval_never_emits_inputs(prepared, case):
    _, output, record, _ = prepared
    with pytest.raises(ValueError, match="exhausted"):
        source.review_baseline_privacy(
            output,
            record,
            Client(reject=case == "privacy", approve_publication=case == "publication"),
        )
    assert not (output / "reviewed").exists()
    assert (
        len(list((output / "attempts" / record.stem).glob("attempt-*/failure.private.json"))) == 3
    )
    assert len(json.loads(record.read_bytes())["privacy_feedback"]) == 3


def test_baseline_cannot_reuse_changed_original_or_frame_inputs(prepared):
    _, output, record, _ = prepared
    saved = json.loads(record.read_bytes())
    path = output / saved["source"] / saved["item"]["article_path"]
    path.write_bytes(path.read_bytes().replace(b"30 seconds", b"60 seconds"))
    with pytest.raises(ValueError, match="stale or unsafe"):
        source.review_baseline_privacy(output, record, Client())
    assert not (output / "reviewed").exists()


def test_dependency_preparation_requires_privacy_evidence_and_rejects_tampering(prepared, tmp_path):
    baseline, _, _, article = prepared
    output = tmp_path / "renewal"
    first = prepare_dependencies(baseline, "a" * 64, [article], output)
    item = first["documents"][0]
    assert "context_path" not in item
    privacy = output / "dependency-privacy"
    record = output / item["source_privacy_path"]
    source.review_baseline_privacy(privacy, record, Client())
    ready = prepare_dependencies(baseline, "a" * 64, [article], output)
    context = json.loads((output / ready["documents"][0]["context_path"]).read_bytes())
    source.verify_baseline_prerequisite(output, context)
    assert "Use port 8443 and wait 30 seconds." in context["body"]
    saved = json.loads(record.read_bytes())
    saved["candidate"]["body"] = saved["candidate"]["body"].replace("30 seconds", "60 seconds")
    record.write_bytes(json_bytes(saved))
    with pytest.raises(ValueError, match="evidence changed"):
        source.verify_baseline_prerequisite(output, context)


def test_native_media_prerequisite_covers_full_frame_and_replays(tmp_path, monkeypatch):
    stream = io.BytesIO()
    Image.new("RGB", (20, 20), "white").save(stream, format="PNG")
    raw = stream.getvalue()
    digest = sha(raw)
    article = "content/docs-cloud-f5-com/how-to/origin-health/index.md"
    asset = article.removesuffix("index.md") + "assets/" + digest + ".png"
    meta = PageMetadata(
        sourceId="docs-cloud-f5-com",
        title="Cloud origin health",
        slug="origin-health",
        category="Guides",
        url="https://docs.cloud.f5.com/docs-v2/how-to/origin-health",
    )
    body = (
        "# Cloud origin health\n\nWait 30 seconds.\n\n![Current controls](assets/"
        + digest
        + ".png)\n"
    )
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    (baseline / "publication.json").write_text(json.dumps({"published_at": "2026-11-10T19:49:38Z"}))
    monkeypatch.setattr(
        source,
        "_verified_baseline",
        lambda *a: {article: serialize_document(meta, body).encode(), asset: raw},
    )
    output = tmp_path / "privacy"
    inventory = source.prepare_baseline_reviews(baseline, "a" * 64, [article], output)
    record = output / inventory["documents"][0]["path"]
    client = Client()
    result = source.review_baseline_privacy(output, record, client)
    assert result["candidate"]["media"][digest]["frame_count"] == 1
    assert len(client.calls) == 5
    assert source.review_baseline_privacy(output, record) == result


@pytest.mark.parametrize("kind", ["privacydecision", "admissionvalidation", "finalclassification"])
def test_transport_resume_preserves_semantic_budget_and_exact_completed_proposal(
    prepared, monkeypatch, kind
):

    _, output, record, _ = prepared
    saved = json.loads(record.read_bytes())
    saved["privacy_feedback"] = ["first semantic correction", "second semantic correction"]
    record.write_bytes(json_bytes(saved))
    original_compile = _compile_admission
    if kind == "finalclassification":

        def compile_with_reconciliation(capture, root, entry, context, visual, proposal, client):
            result = original_compile(capture, root, entry, context, visual, proposal, client)
            checkpoint = json.loads(entry.read_bytes())
            source_review_reconciliation.review_final_findings(
                checkpoint,
                entry,
                root,
                privacy_context(context),
                result[0],
                visual,
                client,
                proposal,
            )
            return result

        monkeypatch.setattr(jobs, "_compile_admission", compile_with_reconciliation)

    class InterruptedClient(Client):
        def __init__(self):
            super().__init__()
            self.failed = []
            self.invalidated = []

        def invalidate(self, response):
            self.invalidated.append(response)

        def execute(self, body):
            name = body["text"]["format"]["name"]
            if name == kind and len(self.failed) < 3:
                self.calls.append(body)
                evidence = ResponseEvidence(
                    request_sha256=request_hash(body),
                    response_sha256=sha(json_bytes({})),
                    model=body["model"],
                    reasoning="high",
                    prompt_version=body["instructions"].rsplit("Prompt version: ", 1)[-1].strip(),
                    request=body,
                    response={},
                    error="transient failure " + str(len(self.failed)),
                    transport="codex_litellm",
                    runtime_version="test",
                )
                self.failed.append(evidence)
                return evidence
            if name == "finalclassification":
                self.calls.append(body)
                payload = json.loads(body["input"][0]["content"][0]["text"])
                value = {
                    "identity": payload["source"]["identity"],
                    "binding_sha256": payload["source"]["binding_sha256"],
                    "candidate_sha256": payload["candidate_sha256"],
                    "findings": [],
                }
                response = {
                    "status": "completed",
                    "model": body["model"],
                    "output": [{"content": [{"type": "output_text", "text": json.dumps(value)}]}],
                }
                return ResponseEvidence(
                    request_sha256=request_hash(body),
                    response_sha256=sha(
                        json.dumps(response, sort_keys=True, separators=(",", ":"))
                    ),
                    model=body["model"],
                    reasoning="high",
                    prompt_version=body["instructions"].rsplit("Prompt version: ", 1)[-1].strip(),
                    request=body,
                    response=response,
                    error=None,
                    transport="codex_litellm",
                    runtime_version="test",
                )
            return super().execute(body)

    client = InterruptedClient()
    for _ in range(3):
        with pytest.raises(TransportFailure):
            source.review_baseline_privacy(output, record, client)
        checkpoint = json.loads(record.read_bytes())
        assert checkpoint["privacy_feedback"] == saved["privacy_feedback"]
        assert checkpoint["status"] == "privacy_review_required"
        assert checkpoint["privacy_retryable"] is True
        assert not (output / "reviewed").exists()
    assert len({item.request_sha256 for item in client.failed}) == 1
    assert not client.invalidated
    assert not list((output / "attempts").rglob("failure.private.json"))
    result = source.review_baseline_privacy(output, record, client)
    assert result["status"] == "privacy_only_validated"
    assert result["privacy_feedback"] == saved["privacy_feedback"]
    assert result["privacy_retryable"] is False
    attempts = list((output / "attempts" / record.stem).glob("attempt-*"))
    assert [p.name for p in attempts] == ["attempt-002"]
    for evidence in client.failed:
        assert any(
            (
                json.loads(p.read_bytes()) == evidence.model_dump()
                or evidence.model_dump() in json.loads(p.read_bytes()).get("phases", {}).values()
            )
            for p in output.rglob("*.json")
        ), "Every failed raw transport must remain preserved after resume"
    if kind != "privacydecision":
        assert sum(b["text"]["format"]["name"] == "privacydecision" for b in client.calls) == 1


def test_source_transport_does_not_invalidate_or_repropose(prepared, monkeypatch):

    _, output, record, _ = prepared
    original = record.read_bytes()
    calls = []

    def interrupted(*args):
        calls.append(args[3])
        if args[3] == "validation":
            raise TransportFailure(
                "final privacy finding transport failed; private evidence preserved"
            )

    def forbidden(*args):
        pytest.fail("Transport failure cannot invalidate a completed semantic proposal")

    monkeypatch.setattr(jobs, "review_one", interrupted)
    monkeypatch.setattr(jobs, "invalidate_classification", forbidden)
    monkeypatch.setattr(jobs, "invalidate_failed_review", forbidden)
    with pytest.raises(TransportFailure):
        _run_record(output, output, record, "all", Client())
    assert calls == ["relevance", "privacy", "validation"]
    assert record.read_bytes() == original
