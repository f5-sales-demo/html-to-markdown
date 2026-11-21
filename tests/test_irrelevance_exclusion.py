"""Unrelated private captures need independent exclusion, without privacy rewrites."""

import io
import json
from pathlib import Path

import pytest
from PIL import Image
from test_source_review_jobs import LocalClient

from html_to_markdown.content_inventory import atomic_private
from html_to_markdown.curation import json_bytes
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.models import PageMetadata
from html_to_markdown.render import serialize_document
from html_to_markdown.source_exclusion import PHASE, VERSION
from html_to_markdown.source_review_jobs import prepare_reviews, run_reviews, verify_reviews


@pytest.mark.parametrize("confirmed", [True, False])
def test_independent_irrelevance_does_not_enter_privacy_transformation(
    capture: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, confirmed: bool
) -> None:
    inventory = json.loads((capture / "inventory.json").read_bytes())
    item = inventory["documents"][0]
    metadata = PageMetadata.model_validate(item["metadata"])
    metadata.title = "BIG-IP virtual server settings"
    prose = "# BIG-IP virtual server settings\n\nConfigure the local virtual server and pool.\n"
    article = serialize_document(metadata, prose).encode()
    source = b"<article><h1>BIG-IP virtual server settings</h1><p>Configure the local virtual server and pool.</p></article>"
    atomic_private(capture / item["article_path"], article)
    atomic_private(capture / item["source_html_path"], source)
    item.update(
        article_sha256=sha(article),
        source_html_sha256=sha(source),
        metadata=metadata.model_dump(by_alias=True),
    )
    atomic_private(capture / "inventory.json", json_bytes(inventory))
    privacy_calls = []

    class IrrelevantClient(LocalClient):
        def execute(self, body: dict):
            phase = body["text"]["format"]["name"]
            if phase in {"privacydecision", "mediaobservations", "observationvalidation"}:
                privacy_calls.append(phase)
                raise AssertionError("Unrelated content must not enter privacy transformations")
            result = super().execute(body)
            if phase in {"relevancedecision", "irrelevancevalidation"}:
                raw = result.response
                value = json.loads(raw["output"][0]["content"][0]["text"])
                if phase == "relevancedecision":
                    value.update(
                        disposition="exclude", reason="The complete source concerns BIG-IP alone"
                    )
                else:
                    value.update(
                        exclusion_basis="unrelated_content" if confirmed else "relevant_content",
                        approved=confirmed,
                    )
                raw["output"][0]["content"][0]["text"] = json.dumps(value)
                return result.model_copy(
                    update={
                        "response_sha256": sha(
                            json.dumps(raw, sort_keys=True, separators=(",", ":"))
                        )
                    }
                )
            return result

    monkeypatch.setattr("html_to_markdown.source_review_jobs.CodexClient", IrrelevantClient)
    output = tmp_path / "review"
    prepare_reviews(capture, output)
    report = run_reviews(capture, output, phase="all", journal=tmp_path / "journal")
    assert not privacy_calls, "Independently irrelevant source entered privacy inference"
    assert report["documents"][0]["disposition"] == ("policy_excluded" if confirmed else "queued")
    assert bool(report["exceptions"]) is not confirmed
    assert not verify_reviews(capture, output)["accepted_files"]
    assert not (output / "reviewed").exists()


@pytest.mark.parametrize(
    "tamper", ["accepted", "missing_confirmation", "privacy_history", "version"]
)
def test_exclusion_cannot_be_promoted_or_replayed_with_weakened_evidence(
    capture: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str
) -> None:
    class ExcludingClient(LocalClient):
        def execute(self, body: dict):
            result = super().execute(body)
            if body["text"]["format"]["name"] == "relevancedecision":
                raw = result.response
                value = json.loads(raw["output"][0]["content"][0]["text"])
                value.update(disposition="exclude", reason="Entire source unrelated to cloud")
                raw["output"][0]["content"][0]["text"] = json.dumps(value)
                return result.model_copy(
                    update={
                        "response_sha256": sha(
                            json.dumps(raw, sort_keys=True, separators=(",", ":"))
                        )
                    }
                )
            return result

    monkeypatch.setattr("html_to_markdown.source_review_jobs.CodexClient", ExcludingClient)
    output = tmp_path / "review"
    prepare_reviews(capture, output)
    run_reviews(capture, output, phase="all", journal=tmp_path / "journal")
    record = next((output / "documents").glob("*.json"))
    saved = json.loads(record.read_bytes())
    assert saved["irrelevance_exclusion_version"] == VERSION
    if tamper == "accepted":
        saved["disposition"] = "accepted"
    elif tamper == "missing_confirmation":
        saved["phases"].pop(PHASE)
    elif tamper == "privacy_history":
        saved["privacy_feedback"] = ["invented replacement history"]
    else:
        saved["irrelevance_exclusion_version"] = "unknown"
    atomic_private(record, json_bytes(saved))
    original = record.read_bytes()
    with pytest.raises((ValueError, KeyError)):
        verify_reviews(capture, output)
    assert record.read_bytes() == original


def exclusion_client(monkeypatch, updates=None, calls=None):
    calls = calls if calls is not None else []

    class Client(LocalClient):
        def execute(self, body: dict):
            calls.append(body)
            result = super().execute(body)
            phase = body["text"]["format"]["name"]
            raw = result.response
            value = json.loads(raw["output"][0]["content"][0]["text"])
            if phase == "relevancedecision":
                value.update(disposition="exclude", reason="Complete original source unrelated")
            elif phase == "irrelevancevalidation":
                value.update(updates or {})
            raw["output"][0]["content"][0]["text"] = json.dumps(value)
            return result.model_copy(
                update={
                    "response_sha256": sha(json.dumps(raw, sort_keys=True, separators=(",", ":")))
                }
            )

    monkeypatch.setattr("html_to_markdown.source_review_jobs.CodexClient", Client)
    return calls


def animated_capture(capture):
    image = io.BytesIO()
    Image.new("RGB", (40, 30), "white").save(
        image,
        format="GIF",
        save_all=True,
        append_images=[Image.new("RGB", (40, 30), "blue")],
        duration=[100, 200],
        loop=0,
    )
    raw = image.getvalue()
    inventory = json.loads((capture / "inventory.json").read_bytes())
    inventory["documents"][0]["media"] = [
        {
            "path": "captures/figure.gif",
            "sha256": sha(raw),
            "url": "https://www.f5.com/example/figure.gif",
        }
    ]
    atomic_private(capture / "captures/figure.gif", raw)
    atomic_private(capture / "inventory.json", json_bytes(inventory))
    return raw


def test_complete_animated_media_is_reviewed_twice_without_transformations(
    capture, tmp_path, monkeypatch
):
    original = animated_capture(capture)
    calls = exclusion_client(monkeypatch)
    output = tmp_path / "review"
    prepare_reviews(capture, output)
    result = run_reviews(capture, output, phase="all", journal=tmp_path / "journal")
    assert result["documents"][0]["disposition"] == "policy_excluded"
    assert [body["text"]["format"]["name"] for body in calls] == [
        "relevancedecision",
        "irrelevancevalidation",
    ]
    assert calls[1]["metadata"]["content_review_role"] == "validator"
    for body in calls:
        assert body["model"] == "gpt-6.1-sol" and body["reasoning"]["effort"] == "high"
        assert sum(c["type"] == "input_image" for c in body["input"][0]["content"]) == 2
    payload = json.loads(calls[1]["input"][0]["content"][0]["text"])
    assert payload["required_media_coverage"] == [
        {"source_sha256": sha(original), "frame": 0},
        {"source_sha256": sha(original), "frame": 1},
    ]
    assert (capture / "captures/figure.gif").read_bytes() == original
    assert not verify_reviews(capture, output)["accepted_files"]
    assert len(calls) == 2 and not (output / "reviewed").exists()


@pytest.mark.parametrize(
    "updates",
    [
        {"media_coverage": []},
        {"complete_document_reviewed": False},
        {"complete_media_reviewed": False},
        {"extraction_valid": False},
        {"policy_versions_checked": False},
        {"evidence_addresses": []},
        {"unresolved": ["scope ambiguous"]},
    ],
)
def test_incomplete_or_ambiguous_independent_exclusion_remains_queued(
    capture, tmp_path, monkeypatch, updates
):
    animated_capture(capture)
    calls = exclusion_client(monkeypatch, updates)
    output = tmp_path / "review"
    prepare_reviews(capture, output)
    result = run_reviews(capture, output, phase="all", journal=tmp_path / "journal")
    assert result["documents"][0]["disposition"] == "queued" and result["exceptions"]
    assert [body["text"]["format"]["name"] for body in calls] == [
        "relevancedecision",
        "irrelevancevalidation",
    ]
    assert not verify_reviews(capture, output)["accepted_files"]


def test_exclusion_preserves_existing_exhausted_privacy_history(capture, tmp_path, monkeypatch):
    calls = exclusion_client(monkeypatch)
    output = tmp_path / "review"
    prepare_reviews(capture, output)
    run_reviews(capture, output, phase="relevance", journal=tmp_path / "journal")
    record = next((output / "documents").glob("*.json"))
    saved = json.loads(record.read_bytes())
    saved["privacy_feedback"] = ["first rejected correction", "second rejected correction"]
    saved["privacy_exhausted"] = True
    atomic_private(record, json_bytes(saved))
    result = run_reviews(capture, output, phase="all", journal=tmp_path / "journal")
    current = json.loads(record.read_bytes())
    assert result["documents"][0]["disposition"] == "policy_excluded"
    assert current["privacy_feedback"] == saved["privacy_feedback"]
    assert current["privacy_exhausted"] is True
    assert len(calls) == 2
    assert not verify_reviews(capture, output)["accepted_files"]
    current["privacy_feedback"] = current["privacy_feedback"][:1]
    current["privacy_exhausted"] = False
    atomic_private(record, json_bytes(current))
    with pytest.raises(ValueError, match="privacy history changed"):
        verify_reviews(capture, output)


def test_previously_excluded_changed_source_enters_complete_admission(
    capture, tmp_path, monkeypatch
):
    exclusion_client(monkeypatch)
    output = tmp_path / "review"
    prepare_reviews(capture, output)
    run_reviews(capture, output, phase="all", journal=tmp_path / "journal")
    inventory = json.loads((capture / "inventory.json").read_bytes())
    item = inventory["documents"][0]
    metadata = PageMetadata.model_validate(item["metadata"])
    prose = "# Configure Distributed Cloud\n\nRun curl --retry 3 within 30 seconds.\n"
    article = serialize_document(metadata, prose).encode()
    source = b"<article><h1>Configure Distributed Cloud</h1><p>Run curl with three retries.</p></article>"
    atomic_private(capture / item["article_path"], article)
    atomic_private(capture / item["source_html_path"], source)
    item.update(article_sha256=sha(article), source_html_sha256=sha(source))
    atomic_private(capture / "inventory.json", json_bytes(inventory))
    calls = []

    class IncludingClient(LocalClient):
        def execute(self, body):
            calls.append(body["text"]["format"]["name"])
            return super().execute(body)

    monkeypatch.setattr("html_to_markdown.source_review_jobs.CodexClient", IncludingClient)
    prepare_reviews(capture, output)
    result = run_reviews(capture, output, phase="all", journal=tmp_path / "journal")
    record = json.loads(next((output / "documents").glob("*.json")).read_bytes())
    assert result["documents"][0]["disposition"] == "accepted"
    assert "privacydecision" in calls and "admissionvalidation" in calls
    assert "irrelevance_exclusion_version" not in record
    assert verify_reviews(capture, output)["accepted_files"]
