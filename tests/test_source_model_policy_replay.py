"""Reconstruct complete historical Sol source requests without rebinding evidence."""

import json
from copy import deepcopy

import pytest
from test_review_controller_compatibility import registry
from test_source_input_projection import Client as ProjectionClient
from test_source_input_projection import logical_request
from test_source_metadata_boundaries import MetadataClient, metadata_body
from test_source_native_boundaries import Client, Probe, _capture_with_images, body

from html_to_markdown import source_boundary_requests, source_review, source_review_jobs
from html_to_markdown.curation import json_bytes
from html_to_markdown.curation_renewal_inputs import prepare_renewals
from html_to_markdown.enrichment import model_policy, native_visuals, requests
from html_to_markdown.enrichment.gates import request_hash
from html_to_markdown.source_boundary_review import (
    execute_cached_source,
    execute_source_request,
    parse_source_response,
)
from html_to_markdown.source_review import PrivacyDecision


def legacy_body(current):
    historical = deepcopy(current)
    historical["instructions"] = historical["instructions"].replace(
        model_policy.ANNOTATION, model_policy.LEGACY_ANNOTATION, 1
    )
    historical["metadata"][model_policy.POLICY_KEY] = "2"
    return historical


@pytest.fixture
def legacy_policy(monkeypatch):
    current = requests.apply_policy

    def apply(body, role):
        return legacy_body(current(body, role))

    with monkeypatch.context() as patch:
        patch.setattr(requests, "apply_policy", apply)
        yield


def prepare_case(kind, monkeypatch):
    if kind in {"native", "metadata_native"}:
        monkeypatch.setattr(native_visuals, "MAX_DECODED_BYTES", 600)
        monkeypatch.setattr(source_boundary_requests, "MAX_DECODED_BYTES", 600)
    if kind.startswith("metadata"):
        return metadata_body.__wrapped__(monkeypatch), MetadataClient(), Probe
    if kind == "projection":
        return logical_request(), ProjectionClient(), PrivacyDecision
    return body(4 if kind == "native" else 1), Client(), Probe


@pytest.mark.parametrize("kind", ["direct", "native", "projection", "metadata", "metadata_native"])
def test_historical_complete_source_replays_under_current_policy(kind, monkeypatch, tmp_path):
    expected, client, schema = prepare_case(kind, monkeypatch)
    historical = legacy_body(expected)
    current = requests.apply_policy
    with monkeypatch.context() as patch:
        patch.setattr(requests, "apply_policy", lambda body, role: legacy_body(current(body, role)))
        evidence = execute_source_request(historical, client, tmp_path)
    before = evidence.model_dump_json()
    calls = len(client.calls) if hasattr(client, "calls") else len(client.requests)
    assert parse_source_response(evidence, schema, expected["model"], expected)
    assert parse_source_response(evidence, schema, historical["model"], request_hash(historical))
    assert evidence.model_dump_json() == before
    assert calls == (len(client.calls) if hasattr(client, "calls") else len(client.requests))


@pytest.mark.parametrize("kind", ["direct", "native", "projection", "metadata", "metadata_native"])
def test_historical_source_cache_requires_complete_expected_request(kind, monkeypatch, tmp_path):
    expected, client, _ = prepare_case(kind, monkeypatch)
    current = requests.apply_policy
    path = tmp_path / "cached.json"
    with monkeypatch.context() as patch:
        patch.setattr(requests, "apply_policy", lambda body, role: legacy_body(current(body, role)))
        old = execute_cached_source(path, legacy_body(expected), client)
    before = path.read_bytes()
    assert execute_cached_source(path, expected, None) == old
    assert path.read_bytes() == before
    changed = deepcopy(expected)
    changed["instructions"] = "Changed policy or editorial instruction. " + changed["instructions"]
    with pytest.raises(ValueError):
        execute_cached_source(path, changed, None)


@pytest.mark.parametrize("fault", ["text", "policy", "model", "role", "reasoning", "schema"])
def test_policy_compatibility_never_accepts_changed_source_requests(fault, legacy_policy, tmp_path):
    original = body(1)
    evidence = execute_source_request(original, Client(), tmp_path)
    expected = deepcopy(original)
    # Restore policy 3 generation only; the captured request stays policy 2.
    expected["instructions"] = expected["instructions"].replace(
        model_policy.LEGACY_ANNOTATION, model_policy.ANNOTATION, 1
    )
    expected["metadata"][model_policy.POLICY_KEY] = model_policy.POLICY_VERSION
    if fault == "text":
        expected["input"][0]["content"][0]["text"] += " "
    elif fault == "policy":
        evidence.request["metadata"][model_policy.POLICY_KEY] = "unknown"
        evidence.request_sha256 = request_hash(evidence.request)
    elif fault == "model":
        evidence.request["model"] = model_policy.ASTRA_MODEL
        evidence.request_sha256 = request_hash(evidence.request)
    elif fault == "role":
        evidence.request["metadata"][model_policy.ROLE_KEY] = "validator"
        evidence.request_sha256 = request_hash(evidence.request)
    elif fault == "reasoning":
        expected["reasoning"]["effort"] = "low"
    else:
        expected["text"]["format"]["schema"]["properties"]["identity"]["enum"] = ["changed"]
    with pytest.raises(ValueError):
        parse_source_response(evidence, Probe, expected["model"], expected)


def test_complete_admission_migration_keeps_original_source_decisions_and_budget(
    capture, monkeypatch, tmp_path
):
    _capture_with_images(capture)
    monkeypatch.setattr(native_visuals, "MAX_DECODED_BYTES", 600)
    monkeypatch.setattr(source_boundary_requests, "MAX_DECODED_BYTES", 600)
    output = tmp_path / "review"
    client = Client()
    monkeypatch.setattr(source_review_jobs, "CodexClient", lambda *args, **kwargs: client)
    current = requests.apply_policy
    with monkeypatch.context() as patch:
        patch.setattr(requests, "apply_policy", lambda body, role: legacy_body(current(body, role)))
        patch.setattr(source_review, "POLICY_VERSION", "2")
        patch.setattr(source_review, "review_context_digest", lambda: "a" * 64)
        source_review_jobs.prepare_reviews(capture, output)
        record = next((output / "documents").glob("*.json"))
        saved = json.loads(record.read_bytes())
        saved["privacy_feedback"] = ["First corrected proposal", "Second corrected proposal"]
        record.write_bytes(json_bytes(saved))
        report = source_review_jobs.run_reviews(
            capture, output, phase="all", journal=tmp_path / "journal"
        )
    assert report["documents"][0]["disposition"] == "accepted"
    original = json.loads(record.read_bytes())
    original_phases = deepcopy(original["phases"])
    calls = len(client.calls)
    registry.__wrapped__(monkeypatch)
    source_review_jobs.prepare_reviews(capture, output)
    result = source_review_jobs.run_reviews(
        capture, output, phase="replay", journal=tmp_path / "offline"
    )
    assert result["documents"][0]["disposition"] == "accepted"
    replayed = json.loads(record.read_bytes())
    assert replayed["binding"] == original["binding"]
    assert replayed["phases"] == original_phases
    assert replayed["privacy_feedback"] == original["privacy_feedback"]
    assert replayed["output_sha256"] == original["output_sha256"]
    assert replayed["disposition"] == "accepted"
    assert len(client.calls) == calls

    replayed["reason"] = "source_curation_requires_review"
    record.write_bytes(json_bytes(replayed))
    renewals = prepare_renewals(capture, output, tmp_path / "renewal")
    assert renewals["documents"][0]["reason"] == "independent_catalog_review_required"
    assert json.loads(record.read_bytes())["phases"] == original_phases
    assert len(client.calls) == calls
