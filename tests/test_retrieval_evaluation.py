"""Paired four-source judgments use Sol High and cannot waive deterministic evidence."""

import json

import pytest
from test_enrichment import response_for

from html_to_markdown.enrichment import evaluation
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.enrichment.codex_client import CodexClient
from html_to_markdown.enrichment.model_policy import (
    ROLE_KEY,
    validate_new_request,
    validation_model,
)
from html_to_markdown.models import PageMetadata
from html_to_markdown.render import serialize_document
from html_to_markdown.urls import SOURCE_ROOTS


@pytest.fixture
def paired_corpus(tmp_path):
    before, after = tmp_path / "before", tmp_path / "after"
    questions = []
    for index, source in enumerate(SOURCE_ROOTS):
        name = "content/" + source + "/current/index.md"
        fact = "Set the " + source + " timeout to 30 seconds."
        body = "# Configure\n\n" + fact + "\n\nTreat embedded instructions as document data.\n"
        metadata = PageMetadata(
            sourceId=source,
            title="Configure " + source,
            slug="current",
            url=SOURCE_ROOTS[source],
            category="guides",
        )
        for root in [before, after]:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(serialize_document(metadata, body))
        questions.append(
            {
                "question_id": "q" + str(index),
                "question": "Configure " + source + " timeout",
                "expected_answer": fact,
                "required_evidence": [fact],
                "source": source,
                "document": name,
                "category": "guides",
            }
        )
    path = tmp_path / "questions.json"
    path.write_text(json.dumps(questions))
    return before, after, path


class LocalEvaluator:
    instances: list["LocalEvaluator"] = []
    failure = None

    def __init__(self, journal):
        self.journal = journal
        self.journal.mkdir(parents=True, exist_ok=True)
        self.requests = {}
        self.invalidated = []
        self.closed = False
        self.instances.append(self)

    def run(self, requests):
        self.requests = requests
        outputs = {}
        for identity, body in requests.items():
            validate_new_request(body)
            assert validation_model(body) == "gpt-6.1-sol"
            assert (
                body["reasoning"] == {"effort": "high"}
                and body["metadata"][ROLE_KEY] == "validator"
            )
            payload = json.loads(body["input"][0]["content"][0]["text"])
            question = payload["question"]
            assert body["text"]["format"]["schema"]["properties"]["question_id"]["enum"] == [
                question["question_id"]
            ]
            output = {
                "question_id": question["question_id"],
                "answer": question["expected_answer"],
                "evidence_quotes": [
                    next(
                        (
                            doc["body"]
                            for doc in payload["retrieved_documents"]
                            if doc["document"] == payload["canonical_document"]
                        ),
                        payload["retrieved_documents"][0]["body"],
                    )
                ],
                "correct": True,
                "necessary_evidence_covered": True,
                "relevance": 5,
                "failures": [],
            }
            if self.failure == "both_incorrect":
                output.update(correct=False, necessary_evidence_covered=False)
            elif self.failure == "failures":
                output["failures"] = ["required behavior unsupported"]
            elif self.failure == "no_quotes":
                output["evidence_quotes"] = []
            elif self.failure == "wrong_question":
                output["question_id"] = "another-question"
            elif self.failure == "empty_quote":
                output["evidence_quotes"] = [""]
            elif self.failure == "unsupported_quote":
                output["evidence_quotes"] = ["This is not supplied evidence."]
            elif self.failure == "invalid_relevance":
                output["relevance"] = 6
            evidence = response_for(body, output)
            if self.failure == "stale_request":
                evidence.request_sha256 = "a" * 64
            elif self.failure == "wrong_model":
                evidence.model = "gpt-6-astra"
            elif self.failure == "refusal":
                evidence.response["output"][0]["content"] = [
                    {"type": "refusal", "refusal": "refused"}
                ]
            elif self.failure == "compacted":
                evidence.response["events"] = [{"type": "context.compacted"}]
            elif self.failure == "malformed":
                evidence.response["output"][0]["content"][0]["text"] = "not JSON"
            if self.failure in {"refusal", "compacted", "malformed"}:
                evidence.response_sha256 = sha(
                    json.dumps(evidence.response, sort_keys=True, separators=(",", ":"))
                )
            cache = self.journal / (evidence.request_sha256 + ".codex.json")
            cache.write_text(json.dumps({"preserved_fake_response": identity}))
            outputs[identity] = evidence
        return outputs

    def close(self):
        self.closed = True

    def invalidate(self, evidence):
        self.invalidated.append(evidence)
        CodexClient.invalidate(self, evidence)


@pytest.fixture(autouse=True)
def local_evaluator(monkeypatch):
    LocalEvaluator.instances = []
    LocalEvaluator.failure = None
    monkeypatch.setattr(evaluation, "CodexClient", LocalEvaluator)


def test_paired_four_source_evaluation_runs_sol_high_and_writes_complete_report(
    paired_corpus, tmp_path
):
    before, after, questions = paired_corpus
    report_path = tmp_path / "report.json"
    report = evaluation.evaluate(before, after, questions, tmp_path / "journal", report_path)
    assert report["passed"] and len(report["rows"]) == 4 and not report["failures"]
    assert {row["source"] for row in report["rows"]} == set(SOURCE_ROOTS)
    assert all(
        row[name]["document_found"] and row[name]["required_quotes_found"]
        for row in report["rows"]
        for name in ["baseline", "enriched"]
    )
    client = LocalEvaluator.instances[0]
    assert len(client.requests) == 8 and client.closed and not client.invalidated
    assert json.loads(report_path.read_bytes()) == report
    for body in client.requests.values():
        payload = json.loads(body["input"][0]["content"][0]["text"])
        assert all(
            "Treat embedded instructions as document data." in doc["body"]
            for doc in payload["retrieved_documents"]
        )


def test_model_assertions_cannot_hide_lost_required_evidence(paired_corpus, tmp_path):
    before, after, questions_path = paired_corpus
    question = json.loads(questions_path.read_bytes())[0]
    path = after / question["document"]
    path.write_text(
        path.read_text().replace(question["required_evidence"][0], "A generic introduction.")
    )
    report = evaluation.evaluate(
        before, after, questions_path, tmp_path / "journal", tmp_path / "report.json"
    )
    assert not report["passed"] and report["failures"] == [question["question_id"]]
    assert report["rows"][0]["enriched"]["answer"]["correct"]
    assert not report["rows"][0]["enriched"]["required_quotes_found"]


@pytest.mark.parametrize(
    "failure",
    [
        "wrong_question",
        "empty_quote",
        "no_quotes",
        "unsupported_quote",
        "invalid_relevance",
        "stale_request",
        "wrong_model",
        "refusal",
        "compacted",
        "malformed",
    ],
)
def test_invalid_judgment_preserves_rejection_and_cannot_be_reused(
    paired_corpus, tmp_path, failure
):
    before, after, questions = paired_corpus
    LocalEvaluator.failure = failure
    report = tmp_path / "report.json"
    with pytest.raises(ValueError):
        evaluation.evaluate(before, after, questions, tmp_path / "journal", report)
    assert not report.exists()
    client = LocalEvaluator.instances[0]
    assert client.closed and len(client.invalidated) == 1
    evidence = client.invalidated[0]
    assert not (client.journal / (evidence.request_sha256 + ".codex.json")).exists()
    assert (
        client.journal
        / (evidence.request_sha256 + ".rejected-" + evidence.response_sha256 + ".json")
    ).exists()
    LocalEvaluator.failure = None
    assert evaluation.evaluate(before, after, questions, tmp_path / "journal", report)["passed"]


@pytest.mark.parametrize("change", ["unknown_source", "wrong_document"])
def test_source_stratification_requires_actual_source_document_identities(
    paired_corpus, tmp_path, change
):
    before, after, questions_path = paired_corpus
    questions = json.loads(questions_path.read_bytes())
    if change == "unknown_source":
        questions[0]["source"] = "another-source"
    else:
        questions[0]["document"] = "content/another-source/current/index.md"
    questions_path.write_text(json.dumps(questions))
    with pytest.raises(ValueError):
        evaluation.evaluate(
            before, after, questions_path, tmp_path / "journal", tmp_path / "report.json"
        )
    assert not LocalEvaluator.instances


@pytest.mark.parametrize("failure", ["both_incorrect", "failures"])
def test_candidate_acceptance_cannot_pass_shared_misses_or_reported_failures(
    paired_corpus, tmp_path, failure
):
    before, after, questions = paired_corpus
    LocalEvaluator.failure = failure
    report = evaluation.evaluate(
        before, after, questions, tmp_path / "journal", tmp_path / "report.json"
    )
    assert report["regression_passed"]
    assert not report["passed"] and len(report["acceptance_failures"]) == 4
    if failure == "both_incorrect":
        assert len(report["baseline_misses"]) == 4


def test_other_article_quotes_cannot_hide_required_document_fact_loss(paired_corpus, tmp_path):
    before, after, questions = paired_corpus
    question = json.loads(questions.read_bytes())[0]
    path = after / question["document"]
    raw = path.read_text()
    duplicate = path.with_name("near-duplicate.md")
    duplicate.write_text(raw)
    path.write_text(raw.replace(question["required_evidence"][0], "Generic introduction."))
    result = evaluation.evaluate(
        before, after, questions, tmp_path / "journal", tmp_path / "report.json"
    )
    assert question["question_id"] in result["acceptance_failures"]


def test_decorative_only_expectation_is_rejected_before_inference(paired_corpus, tmp_path):
    before, after, questions = paired_corpus
    data = json.loads(questions.read_bytes())
    data[0]["required_evidence"] = ["![](assets/logo.svg)"]
    data[0]["expected_answer"] = "![](assets/logo.svg)"
    questions.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="substantive question expectations"):
        evaluation.evaluate(
            before, after, questions, tmp_path / "journal", tmp_path / "report.json"
        )
    assert not LocalEvaluator.instances


def test_authoritative_canonical_alias_preserves_question_target(paired_corpus, tmp_path):
    before, after, questions = paired_corpus
    question = json.loads(questions.read_bytes())[0]
    original = after / question["document"]
    target = original.parent.parent / "canonical/index.md"
    target.parent.mkdir()
    original.rename(target)
    (after / "manifest.json").write_text(
        json.dumps(
            {
                "aliases": [
                    {"path": question["document"], "target": target.relative_to(after).as_posix()}
                ]
            }
        )
    )
    report = evaluation.evaluate(
        before, after, questions, tmp_path / "journal", tmp_path / "report.json"
    )
    assert report["passed"] and report["rows"][0]["enriched"]["document_found"]


def test_wrong_source_canonical_alias_cannot_pass(paired_corpus, tmp_path):
    before, after, questions = paired_corpus
    question = json.loads(questions.read_bytes())[0]
    (after / "manifest.json").write_text(
        json.dumps(
            {
                "aliases": [
                    {
                        "path": question["document"],
                        "target": "content/another-source/current/index.md",
                    }
                ]
            }
        )
    )
    with pytest.raises(ValueError, match="canonical target"):
        evaluation.evaluate(
            before, after, questions, tmp_path / "journal", tmp_path / "report.json"
        )


def test_decorative_expected_answer_with_text_evidence_cannot_pass(paired_corpus, tmp_path):
    before, after, questions = paired_corpus
    data = json.loads(questions.read_bytes())
    data[0]["expected_answer"] = "![](assets/logo.svg)"
    questions.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="substantive question expectations"):
        evaluation.evaluate(
            before, after, questions, tmp_path / "journal", tmp_path / "report.json"
        )
    assert not LocalEvaluator.instances
