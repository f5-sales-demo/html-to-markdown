"""Reuse exact historical sections only with a newly enforced whole-document review."""

import json
from copy import deepcopy

import pytest
import test_curation_section_validation as fixtures

import html_to_markdown.curation_section_validation as controller
from html_to_markdown.content_inventory import atomic_private
from html_to_markdown.curation import json_bytes
from html_to_markdown.curation_fact_ledger import restore_ledger
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.enrichment.gates import request_hash


def historical_sections(body, directory):
    context, candidate = controller._source(body)
    groups = controller.assignments(context)
    selection = {
        "version": controller.VERSION,
        "original_request_sha256": request_hash(body),
        "execution_sha256": controller._execution_digest(legacy=True),
        "candidate_sha256": candidate["output_sha256"],
        "assignments_sha256": sha(json_bytes(groups)),
        "new_editor_proposals": 0,
    }
    target = directory / sha(json_bytes(selection))
    atomic_private(target / "selection.json", json_bytes(selection))
    client = fixtures.Client()
    for index, addresses in enumerate(groups):
        request = controller._scope_request(body, addresses, {**selection, "section": index})
        evidence = client.execute(request)
        atomic_private(
            target / f"section-{index:04d}.json", json_bytes(evidence.model_dump(mode="json"))
        )
    return target


def test_preserved_historical_sections_require_only_a_new_pinned_final_review(tmp_path):
    _, body = fixtures.inputs()
    original = deepcopy(body)
    target = historical_sections(body, tmp_path)
    preserved = {p: p.read_bytes() for p in target.iterdir()}
    client = fixtures.Client()
    result = controller.validate_sections(body, tmp_path, client)
    assert len(client.calls) == 1
    final = fixtures.before_payload(client.calls[0])
    assert final["independent_review_selection"]["final_document_review"]
    assert final["independent_review_selection"]["final_controller_sha256"]
    assert restore_ledger(final) == result["validation"]["facts"]
    assert final["independent_review_selection"][
        "execution_sha256"
    ] == controller._execution_digest(legacy=True)
    assert (
        final["independent_review_selection"]["execution_sha256"] != controller._execution_digest()
    )
    assert all(path.read_bytes() == data for path, data in preserved.items())
    assert body == original
    assert controller.validate_sections(body, tmp_path) == result


def test_historical_sections_alone_do_not_approve_the_document(tmp_path):
    _, body = fixtures.inputs()
    historical_sections(body, tmp_path)
    with pytest.raises(ValueError, match="unavailable"):
        controller.validate_sections(body, tmp_path)
    with pytest.raises(ValueError, match="rejected"):
        controller.validate_sections(body, tmp_path, fixtures.Client("final"))
    assert not list(tmp_path.rglob("validated.json"))


def test_historical_request_body_cannot_be_rebound(tmp_path):
    _, body = fixtures.inputs()
    target = historical_sections(body, tmp_path)
    path = next(target.glob("section-*.json"))
    evidence = json.loads(path.read_bytes())
    evidence["request"]["instructions"] += " changed input context"
    atomic_private(path, json_bytes(evidence))
    client = fixtures.Client()
    with pytest.raises(ValueError, match="stale section review request"):
        controller.validate_sections(body, tmp_path, client)
    assert client.calls == []


def test_changed_historical_selection_guard_is_rejected(tmp_path):
    _, body = fixtures.inputs()
    target = historical_sections(body, tmp_path)
    guard = target / "selection.json"
    selection = json.loads(guard.read_bytes())
    selection["candidate_sha256"] = "f" * 64
    atomic_private(guard, json_bytes(selection))
    with pytest.raises(ValueError, match="changed historical section selection"):
        controller.validate_sections(body, tmp_path)


def test_other_execution_files_cannot_be_selected_under_a_legacy_digest(tmp_path, monkeypatch):
    _, body = fixtures.inputs()
    historical_sections(body, tmp_path)
    # A changed protocol digest must select a different directory, never the old response.
    monkeypatch.setattr(controller, "LEGACY_SECTION_SHA256", "f" * 64)
    with pytest.raises(ValueError, match="unavailable"):
        controller.validate_sections(body, tmp_path)


def test_final_capacity_is_preflighted_before_any_section_inference(tmp_path, monkeypatch):
    _, body = fixtures.inputs()
    measure = controller._request_characters
    measurements = []

    def full_final_only(request):
        payload = fixtures.before_payload(request)
        measurements.append(payload["independent_review_selection"])
        if "independent_fact_review_ledger" in payload:
            return 1048577
        return measure(request)

    monkeypatch.setattr(controller, "_request_characters", full_final_only)
    client = fixtures.Client()
    with pytest.raises(ValueError, match="native input capacity"):
        controller.validate_sections(body, tmp_path, client)
    assert client.calls == []
    assert len(measurements) == 1
    assert measurements[0]["preflight_only"]
    assert not list(tmp_path.rglob("section-*.json"))
    assert not list(tmp_path.rglob("validated.json"))
