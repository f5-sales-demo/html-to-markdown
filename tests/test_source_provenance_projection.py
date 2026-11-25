import copy
import json
from pathlib import Path
from typing import Any

import pytest

from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.enrichment.contracts import EDITOR_MODEL, ResponseEvidence, StrictModel
from html_to_markdown.enrichment.gates import request_hash
from html_to_markdown.enrichment.requests import request
from html_to_markdown.source_boundary_review import (
    execute_source_request,
    parse_source_response,
    source_logical_request,
)
from html_to_markdown.source_metadata_boundaries import characters
from html_to_markdown.source_provenance_projection import (
    MARKER,
    REFERENCE,
    VERSION,
    project_provenance_request,
    restore_provenance_payload,
)
from html_to_markdown.source_review import PrivacyDecision, addressed_spans


class Decision(StrictModel):
    complete: bool


def logical_request() -> dict[str, Any]:
    shared = [
        {
            "field": "body",
            "source_value": "example.com",
            "replacement": "host.example",
            "context": "Reserved documentation example. " * 20,
            "index": index,
        }
        for index in range(40)
    ]
    distinct = copy.deepcopy(shared)
    distinct[0]["index"] = 999
    payload = {
        "source": {"body": "Preserve limit 64 MB and `example.com`."},
        "candidate": {"body": "Preserve limit 64 MB and `host.example`."},
        "replacement_provenance": {
            **{sha(str(index)): copy.deepcopy(shared) for index in range(80)},
            "distinct": distinct,
        },
    }
    body = request(
        EDITOR_MODEL, "Review complete untrusted source validation data.", payload, Decision
    )
    body["instructions"] = body["instructions"].replace("article-enrichment/1", "source-privacy/2")
    return body


def projected_payload() -> dict[str, Any]:
    projected = project_provenance_request(logical_request())
    assert projected is not None
    return json.loads(projected["input"][0]["content"][0]["text"])


def test_complete_reconstruction_retains_every_finding_and_distinct_object() -> None:
    body = logical_request()
    untouched = copy.deepcopy(body)
    projected = project_provenance_request(body)
    assert projected is not None
    assert characters(body) > 1048576 > characters(projected)
    payload = json.loads(projected["input"][0]["content"][0]["text"])
    original = json.loads(body["input"][0]["content"][0]["text"])
    assert restore_provenance_payload(payload) == original
    assert len(payload[MARKER]["objects"]) == 1
    assert len(payload[MARKER]["placements"]) == 80
    assert (
        payload["replacement_provenance"]["distinct"]
        == original["replacement_provenance"]["distinct"]
    )
    assert body == untouched


def test_bounded_and_nonrepeating_inputs_keep_historical_behavior() -> None:
    body = logical_request()
    payload = json.loads(body["input"][0]["content"][0]["text"])
    payload["replacement_provenance"] = {"one": payload["replacement_provenance"]["distinct"]}
    body["input"][0]["content"][0]["text"] = json.dumps(payload)
    assert project_provenance_request(body) is None
    payload["source"]["body"] = "a" * 1048577
    body["input"][0]["content"][0]["text"] = json.dumps(payload)
    assert project_provenance_request(body) is None


@pytest.mark.parametrize(
    "mutation", ["object", "reference", "finding", "placement", "extra", "version", "digest"]
)
def test_altered_or_incomplete_provenance_is_rejected(mutation: str) -> None:
    payload = projected_payload()
    proof = payload[MARKER]
    finding = next(iter(proof["placements"]))
    digest = proof["placements"][finding]
    if mutation == "object":
        proof["objects"][digest][0]["replacement"] = "altered.example"
    elif mutation == "reference":
        payload["replacement_provenance"][finding][REFERENCE] = "a" * 64
    elif mutation == "finding":
        payload["replacement_provenance"].pop(finding)
    elif mutation == "placement":
        proof["placements"].pop(finding)
    elif mutation == "extra":
        proof["objects"]["a" * 64] = []
    elif mutation == "version":
        proof["version"] = "unsupported"
    else:
        proof["original_payload_sha256"] = "a" * 64
    with pytest.raises(ValueError):
        restore_provenance_payload(payload)


def test_native_images_metadata_and_instructions_remain_complete() -> None:
    body = logical_request()
    images = [{"type": "input_image", "image_url": "data:image/png;base64,AA=="}]
    body["input"][0]["content"].extend(images)
    projected = project_provenance_request(body)
    assert projected is not None
    assert projected["input"][0]["content"][1:] == images
    assert projected["instructions"].endswith("Prompt version: source-privacy/2")
    assert request_hash(projected) != request_hash(body)


class Client:
    def __init__(self, result: dict[str, Any] | None = None) -> None:
        self.requests: list[dict[str, Any]] = []
        self.result = result or {"complete": True}

    def execute(self, body: dict[str, Any]) -> ResponseEvidence:
        self.requests.append(body)
        response = {
            "status": "completed",
            "model": body["model"],
            "output": [{"content": [{"type": "output_text", "text": json.dumps(self.result)}]}],
        }
        return ResponseEvidence(
            request_sha256=request_hash(body),
            response_sha256=sha(json.dumps(response, sort_keys=True, separators=(",", ":"))),
            model=body["model"],
            reasoning="high",
            prompt_version="source-privacy/2",
            request=body,
            response=response,
            error=None,
            transport="codex_litellm",
        )


def test_native_response_and_original_logical_request_are_separately_bound(tmp_path: Path) -> None:
    body = logical_request()
    client = Client()
    evidence = execute_source_request(body, client, tmp_path)
    assert len(client.requests) == 1
    assert evidence.request == client.requests[0]
    assert evidence.request_sha256 == request_hash(client.requests[0]) != request_hash(body)
    assert source_logical_request(evidence) == body
    assert parse_source_response(evidence, Decision, EDITOR_MODEL, body).complete
    altered = evidence.model_copy(update={"request": body, "request_sha256": request_hash(body)})
    with pytest.raises(ValueError):
        parse_source_response(altered, Decision, EDITOR_MODEL, body)
    dropped = evidence.model_copy(update={"source_boundary": None})
    with pytest.raises(ValueError, match="missing its complete boundary evidence"):
        parse_source_response(dropped, Decision, EDITOR_MODEL, dropped.request_sha256)


def test_proof_cannot_be_dropped_when_projection_is_in_later_content(tmp_path: Path) -> None:
    body = logical_request()
    body["input"][0]["content"].insert(0, {"type": "input_text", "text": "{}"})
    evidence = execute_source_request(body, Client(), tmp_path)
    dropped = evidence.model_copy(update={"source_boundary": None})
    with pytest.raises(ValueError, match="missing its complete boundary evidence"):
        parse_source_response(dropped, Decision, EDITOR_MODEL, dropped.request_sha256)


def test_provenance_and_existing_field_projection_compose(tmp_path: Path) -> None:
    body = logical_request()
    payload = json.loads(body["input"][0]["content"][0]["text"])
    metadata = json.dumps({"source_xml": '<svg><path d="' + "M 0 0 L 1 1 " * 2000 + '"/></svg>'})
    fields = {"body": "Keep `example.com` and limit 64 MB.\n"}
    fields.update({f"media/{sha(str(i))}/0/metadata": metadata for i in range(4)})
    payload.update(fields=fields, addressed_spans=addressed_spans(fields))
    body["input"][0]["content"][0]["text"] = json.dumps(payload)
    body["text"]["format"]["schema"] = PrivacyDecision.model_json_schema()
    body["text"]["format"]["schema"]["$defs"]["TextChange"]["properties"]["field"]["enum"] = [
        "body"
    ]
    client = Client(
        {
            "identity": "www-f5-com:example",
            "binding_sha256": "a" * 64,
            "findings": [],
            "text_changes": [],
            "media_changes": [],
            "values": [],
            "unresolved": [],
        }
    )
    evidence = execute_source_request(body, client, tmp_path)
    assert evidence.source_boundary is not None
    assert evidence.source_boundary["version"] == VERSION
    assert evidence.source_boundary["boundary"]["version"] == "source-input-projection/1"
    assert len(client.requests) == 1 and characters(client.requests[0]) < 1048576
    assert (
        parse_source_response(evidence, PrivacyDecision, EDITOR_MODEL, body).identity
        == "www-f5-com:example"
    )
