"""Current independent checks cannot renew editors or accept divergent proposals."""

import json

import pytest
from test_source_existing_selection import seal
from test_source_review_jobs import LocalClient

from html_to_markdown import source_existing_validation as review
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.enrichment.contracts import ResponseEvidence
from html_to_markdown.enrichment.gates import request_hash


class IndependentClient(LocalClient):
    def __init__(self, *, failure=None):
        self.calls = []
        self.failure = failure
        self.invalidated = []

    def invalidate(self, evidence):
        self.invalidated.append(evidence)

    def execute(self, body):
        self.calls.append(body)
        payload = json.loads(body["input"][0]["content"][0]["text"])
        phase = body["text"]["format"]["name"]
        assert body["model"] == "gpt-6.1-sol"
        assert body["reasoning"]["effort"] == "high"
        assert not body.get("tools")
        if phase == "sourceledgerresolution":
            valid = [
                r for r in payload["candidates"] if r["disposition"] == "independently_validated"
            ]
            selected = valid[-1]["record_sha256"] if valid else None
            value = {
                "identity": payload["identity"],
                "binding_sha256": payload["binding_sha256"],
                "selected_record_sha256": selected,
                "complete_evidence_reviewed": True,
                "dispositions": [
                    {
                        "record_sha256": row["record_sha256"],
                        "disposition": "select"
                        if row["record_sha256"] == selected
                        else "equivalent_validated"
                        if row in valid
                        else "reject",
                        "reason": "Current complete independent validation passed"
                        if row in valid
                        else "Original input failed its complete source guard",
                    }
                    for row in payload["candidates"]
                ],
                "unresolved": [],
            }
            if self.failure == "selection_coverage":
                value["dispositions"].pop()
        else:
            evidence = super().execute(body)
            value = json.loads(evidence.response["output"][0]["content"][0]["text"])
            if phase == "admissionvalidation":
                assert payload["existing_source_proposal"]["new_editor_allowances"] == 0
                original = payload["original_privacy_decision"]["binding_sha256"]
                assert original != payload["binding_sha256"]
                assert payload["existing_source_proposal"]["original_binding_sha256"] == original
                assert "curation_candidate" in payload
                if self.failure in {
                    "privacy_valid",
                    "media_valid",
                    "retirement_valid",
                    "technical_relationships_preserved",
                    "complete_document_reviewed",
                    "complete_media_reviewed",
                    "extraction_valid",
                }:
                    value[self.failure] = False
                    value["approved"] = False
                    value["failures"] = ["Independent current check failed"]
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


def test_current_validation_selects_only_a_completed_original_and_replays_read_only(
    capture, tmp_path
):
    journal, current, original, root = seal(capture, tmp_path, second=True)
    originals = {p: p.read_bytes() for p in [current, *root.glob("*.json")]}
    output = tmp_path / "validation"
    client = IndependentClient()
    result = review.review_source_selection(journal, output, client)
    assert result["new_editor_allowances"] == 0
    assert result["admission_approved"] is False
    assert len(result["results"]) == 2
    assert all(r["editor_corrections_consumed"] == 2 for r in result["results"])
    assert [b["text"]["format"]["name"] for b in client.calls].count("privacydecision") == 0
    assert all(p.read_bytes() == raw for p, raw in originals.items())
    files = {p: sha(p.read_bytes()) for p in output.rglob("*") if p.is_file()}
    assert review.review_source_selection(journal, output) == result
    assert files == {p: sha(p.read_bytes()) for p in output.rglob("*") if p.is_file()}
    assert not (output / "reviewed").exists()
    assert original.exists()


@pytest.mark.parametrize(
    "failure",
    [
        "privacy_valid",
        "media_valid",
        "retirement_valid",
        "technical_relationships_preserved",
        "complete_document_reviewed",
        "complete_media_reviewed",
        "extraction_valid",
        "selection_coverage",
    ],
)
def test_failed_complete_checks_leave_current_records_and_allowances_unchanged(
    capture, tmp_path, failure
):
    journal, current, original, _ = seal(capture, tmp_path)
    before = current.read_bytes(), original.read_bytes()
    client = IndependentClient(failure=failure)
    with pytest.raises(ValueError, match="selection|histories|validation"):
        review.review_source_selection(journal, tmp_path / "validation", client)
    assert (current.read_bytes(), original.read_bytes()) == before
    assert all(b["text"]["format"]["name"] != "privacydecision" for b in client.calls)
    assert client.invalidated


def test_missing_selected_evidence_cannot_be_repaired_by_offline_replay(capture, tmp_path):
    journal, current, original, _ = seal(capture, tmp_path)
    output = tmp_path / "validation"
    result = review.review_source_selection(journal, output, IndependentClient())
    selected = result["selected_record_sha256"]
    evidence = next((output / journal.stem / selected / "validation").glob("*.json"))
    evidence.unlink()
    before = {p: sha(p.read_bytes()) for p in output.rglob("*") if p.is_file()}
    with pytest.raises(ValueError):
        review.review_source_selection(journal, output)
    assert before == {p: sha(p.read_bytes()) for p in output.rglob("*") if p.is_file()}
    assert current.exists() and original.exists()


def test_distinct_currently_validated_outputs_cannot_be_chosen_by_hash_order(capture, tmp_path):
    journal, _, _, _ = seal(capture, tmp_path, second=True)
    inputs = review.verify_source_selection(journal)
    strongest = [r for r in inputs["candidates"] if r["strongest"]]
    results = [
        {
            "record_sha256": row["record_sha256"],
            "disposition": "independently_validated",
            "candidate_sha256": str(index) * 64,
        }
        for index, row in enumerate(strongest)
    ]
    body = review.selector_request(journal.stem, inputs, results)
    ev = IndependentClient().execute(body)
    value = review.parse_source_response(ev, review.SourceLedgerResolution, "gpt-6.1-sol", body)
    with pytest.raises(ValueError, match="complete independent selection"):
        review.check_selection(value, body, results)
