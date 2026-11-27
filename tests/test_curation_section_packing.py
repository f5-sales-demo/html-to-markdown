"""Packed fact responses retain complete source context and immutable legacy evidence."""

import json
from copy import deepcopy

import pytest
import test_curation_section_validation as fixtures

import html_to_markdown.curation_section_validation as controller
from html_to_markdown.content_inventory import atomic_private
from html_to_markdown.curation import json_bytes
from html_to_markdown.curation_section_validation import (
    _execution_digest,
    _final_controller_digest,
    _scope_request,
    _section_assignments,
)
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.enrichment.gates import request_hash
from html_to_markdown.errors import TransportFailure

PREDECESSOR = "b1745d5b4876db5802d8bd2268e0d73c8dc38ac5b19531ce16a941610ca7678e"


def source_groups(sizes):
    return {
        "blocks": [
            {
                "address": f"section-{i}:paragraph:{n}",
                "kind": "paragraph",
                "sha256": sha(f"{i}-{n}"),
                "text": f"Required fact {i}-{n}.",
            }
            for i, size in enumerate(sizes)
            for n in range(size)
        ]
    }


def historical_selection(body, root):
    context, candidate = controller._source(body)
    groups = _section_assignments(context)
    selection = {
        "version": controller.VERSION,
        "original_request_sha256": request_hash(body),
        "execution_sha256": _execution_digest(section_sha256=PREDECESSOR),
        "candidate_sha256": candidate["output_sha256"],
        "assignments_sha256": sha(json_bytes(groups)),
        "new_editor_proposals": 0,
    }
    directory = root / sha(json_bytes(selection))
    atomic_private(directory / "selection.json", json_bytes(selection))
    return groups, selection, directory


def save_sections(body, groups, selection, directory, count=None):
    client = fixtures.Client()
    for i, addresses in enumerate(groups[:count]):
        request = _scope_request(body, addresses, {**selection, "section": i})
        evidence = client.execute(request)
        atomic_private(
            directory / f"section-{i:04d}.json", json_bytes(evidence.model_dump(mode="json"))
        )
    return client


def save_legacy_final(body, groups, selection, directory):
    client = save_sections(body, groups, selection, directory)
    facts = []
    custody = []
    for i, _request in enumerate(client.calls):
        evidence = json.loads((directory / f"section-{i:04d}.json").read_bytes())
        value = json.loads(evidence["response"]["output"][0]["content"][0]["text"])
        facts.extend(value["facts"])
        custody.append(
            {
                "section": i,
                "request_sha256": evidence["request_sha256"],
                "response_sha256": evidence["response_sha256"],
            }
        )
    request = _scope_request(
        body,
        [],
        {
            **selection,
            "final_document_review": True,
            "final_controller_sha256": _final_controller_digest(PREDECESSOR),
        },
        facts,
    )
    evidence = client.execute(request)
    final_value = json.loads(evidence.response["output"][0]["content"][0]["text"])
    result = {
        "selection": selection,
        "sections": custody,
        "final_request_sha256": request_hash(request),
        "final_response_sha256": evidence.response_sha256,
        "validation": {**final_value, "facts": facts},
        "planner_validation_required": True,
        "publication_approved": False,
    }
    atomic_private(directory / "final.json", json_bytes(evidence.model_dump(mode="json")))
    atomic_private(directory / "validated.json", json_bytes(result))
    return result


def test_measured_adjacent_sections_use_three_full_context_requests():
    source = source_groups([8, 2, 10, 10, 14, 5, 9, 2, 6, 5, 10, 6, 6, 14, 6])
    groups = controller.assignments(source)
    assert [len(group) for group in groups] == [44, 43, 26]
    assert [a for group in groups for a in group] == [b["address"] for b in source["blocks"]]
    assert groups == controller.assignments(source)


def test_oversized_sections_keep_bounded_chunks_and_all_occurrences():
    source = source_groups([2, 100, 3])
    groups = controller.assignments(source)
    assert [len(group) for group in groups] == [2, 48, 48, 7]
    assert [a for group in groups for a in group] == [b["address"] for b in source["blocks"]]
    with pytest.raises(ValueError, match="positive"):
        controller.assignments(source, 0)
    assert not controller.assignments({"blocks": []})


def test_new_packed_request_contains_every_source_and_candidate_block(tmp_path):
    source, body = fixtures.inputs()
    original = deepcopy(body)
    client = fixtures.Client()
    result = controller.validate_sections(body, tmp_path, client)
    assert len(result["sections"]) == 1
    assert len(client.calls) == 2
    assert result["selection"]["assignment_strategy"] == "adjacent-sections/1"
    for call in client.calls:
        payload = fixtures.before_payload(call)
        assert payload["blocks"] == source["blocks"]
        assert payload["candidate"] == fixtures.before_payload(original)["candidate"]
        assert call["input"][0]["content"][1:] == original["input"][0]["content"][1:]
    assert fixtures.before_payload(client.calls[-1])["independent_review_selection"][
        "final_document_review"
    ]
    assert body == original


def test_completed_version_one_replays_without_a_new_request_or_changed_byte(tmp_path):
    _, body = fixtures.inputs()
    groups, selection, directory = historical_selection(body, tmp_path)
    result = save_legacy_final(body, groups, selection, directory)
    before = {p: p.read_bytes() for p in directory.iterdir()}
    assert controller.validate_sections(body, tmp_path) == result
    assert all(p.read_bytes() == value for p, value in before.items())


def test_partial_version_one_keeps_original_assignments_and_resume_history(tmp_path):
    _, body = fixtures.inputs()
    groups, selection, directory = historical_selection(body, tmp_path)
    save_sections(body, groups, selection, directory, 1)
    preserved = {p: p.read_bytes() for p in directory.iterdir()}
    first = fixtures.Client("interrupt")
    with pytest.raises(TransportFailure):
        controller.validate_sections(body, tmp_path, first)
    client = fixtures.Client()
    result = controller.validate_sections(body, tmp_path, client)
    assert result["selection"] == selection
    assert len(result["sections"]) == len(groups)
    assert all(p.read_bytes() == value for p, value in preserved.items())
    assert list(directory.glob("history/*.json"))
    assert controller.validate_sections(body, tmp_path) == result


@pytest.mark.parametrize("change", ["final_request", "final_controller", "dependency"])
def test_stale_final_or_unknown_dependency_cannot_reuse_evidence(tmp_path, monkeypatch, change):
    _, body = fixtures.inputs()
    groups, selection, directory = historical_selection(body, tmp_path)
    save_legacy_final(body, groups, selection, directory)
    if change == "dependency":
        actual = _execution_digest

        def changed(**kwargs):
            return sha(actual(**kwargs) + "changed dependent policy/controller")

        monkeypatch.setattr(controller, "_execution_digest", changed)
    else:
        path = directory / "final.json"
        evidence = json.loads(path.read_bytes())
        if change == "final_request":
            evidence["request"]["instructions"] += " changed"
        else:
            payload = fixtures.before_payload(evidence["request"])
            payload["independent_review_selection"]["final_controller_sha256"] = "f" * 64
            evidence["request"]["input"][0]["content"][0]["text"] = json.dumps(payload)
        atomic_private(path, json_bytes(evidence))
    client = fixtures.Client()
    with pytest.raises(ValueError):
        controller.validate_sections(body, tmp_path, client if change != "dependency" else None)
    assert not client.calls


def test_version_one_final_rejection_remains_rejected_after_packing(tmp_path):
    _, body = fixtures.inputs()
    groups, selection, directory = historical_selection(body, tmp_path)
    save_sections(body, groups, selection, directory)
    with pytest.raises(ValueError, match="rejected"):
        controller.validate_sections(body, tmp_path, fixtures.Client("final"))
    assert not (directory / "validated.json").exists()
