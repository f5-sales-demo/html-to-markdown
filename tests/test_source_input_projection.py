import copy
import json
from pathlib import Path
from typing import Any

import pytest

from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.enrichment.contracts import EDITOR_MODEL, ResponseEvidence
from html_to_markdown.enrichment.gates import request_hash
from html_to_markdown.enrichment.requests import request
from html_to_markdown.source_boundary_review import (
    execute_source_request,
    parse_source_response,
    source_logical_request,
)
from html_to_markdown.source_input_projection import (
    MARKER,
    project_source_request,
    restore_source_payload,
)
from html_to_markdown.source_review import PrivacyDecision, addressed_spans


def logical_request() -> dict[str, Any]:
    xml = '<svg><path d="' + "M 0 0 L 1 1 " * 2000 + '"/></svg>'
    metadata = {"format": "SVG", "source_xml": xml}
    encoded = json.dumps(metadata, sort_keys=True)
    fields = {"body": "Keep `example.com` and limit 64 MB.\n", "metadata/title": "Example"}
    media = {}
    for index in range(4):
        digest = sha(str(index))
        prefix = f"media/{digest}/0"
        fields[prefix + "/metadata"] = encoded
        fields[prefix + "/ocr"] = "example.com"
        media[digest] = {"frame_count": 1, "frames": [{"frame": 0, "metadata": metadata}]}
    payload = {
        "identity": "www-f5-com:example",
        "binding_sha256": "a" * 64,
        "fields": fields,
        "media": media,
        "body": fields["body"],
        "addressed_spans": addressed_spans(fields),
        "findings": [],
    }
    body = request(EDITOR_MODEL, "Untrusted complete source data.", payload, PrivacyDecision)
    body["instructions"] = body["instructions"].replace("article-enrichment/1", "source-privacy/2")
    body["text"]["format"]["schema"]["$defs"]["TextChange"]["properties"]["field"]["enum"] = [
        "body",
        "metadata/title",
    ]
    return body


def test_projection_retains_complete_values_and_removes_only_forbidden_edit_guards() -> None:
    body = logical_request()
    untouched = copy.deepcopy(body)
    projected = project_source_request(body)
    assert projected is not None
    before = body["input"][0]["content"][0]["text"]
    after = projected["input"][0]["content"][0]["text"]
    assert len(before) > 1048576
    assert len(after) < 1048576
    restored = restore_source_payload(json.loads(after))
    assert restored == json.loads(before)
    assert body == untouched
    retained = json.loads(after)["addressed_spans"]
    assert {span[0] for span in retained} == {"body", "metadata/title"}


def test_projection_retains_all_native_images_and_prompt_version() -> None:
    body = logical_request()
    images = [{"type": "input_image", "image_url": "data:image/png;base64,AA=="}] * 4
    body["input"][0]["content"].extend(images)
    projected = project_source_request(body)
    assert projected is not None
    assert projected["input"][0]["content"][1:] == images
    assert projected["instructions"].endswith("Prompt version: source-privacy/2")
    assert request_hash(projected) != request_hash(body)


def test_source_values_resembling_control_markers_are_preserved() -> None:
    body = logical_request()
    payload = json.loads(body["input"][0]["content"][0]["text"])
    payload["fields"]["metadata/title"] = json.dumps({MARKER: {"field": "body"}})
    payload["addressed_spans"] = addressed_spans(payload["fields"])
    body["input"][0]["content"][0]["text"] = json.dumps(payload)
    projected = project_source_request(body)
    assert projected is not None
    assert (
        restore_source_payload(json.loads(projected["input"][0]["content"][0]["text"])) == payload
    )


def test_changed_referenced_source_value_is_rejected() -> None:
    projected = project_source_request(logical_request())
    assert projected is not None
    payload = json.loads(projected["input"][0]["content"][0]["text"])
    field = next(
        name
        for name, value in payload["fields"].items()
        if name.startswith("media/") and isinstance(value, str) and name.endswith("/metadata")
    )
    payload["fields"][field] += "changed"
    with pytest.raises(ValueError):
        restore_source_payload(payload)


def test_changed_original_guard_table_is_rejected() -> None:
    body = logical_request()
    payload = json.loads(body["input"][0]["content"][0]["text"])
    payload["addressed_spans"][0][3] = "b" * 64
    body["input"][0]["content"][0]["text"] = json.dumps(payload)
    with pytest.raises(ValueError):
        project_source_request(body)


class Client:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def execute(self, body: dict[str, Any]) -> ResponseEvidence:
        self.requests.append(body)
        result = {
            "identity": "www-f5-com:example",
            "binding_sha256": "a" * 64,
            "findings": [],
            "text_changes": [],
            "media_changes": [],
            "values": [],
            "unresolved": [],
        }
        response = {
            "status": "completed",
            "model": body["model"],
            "output": [{"content": [{"type": "output_text", "text": json.dumps(result)}]}],
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


def test_actual_native_response_is_never_rebound_to_logical_request(tmp_path: Path) -> None:
    body = logical_request()
    client = Client()
    evidence = execute_source_request(body, client, tmp_path)
    assert len(client.requests) == 1
    assert evidence.request == client.requests[0]
    assert evidence.request_sha256 == request_hash(client.requests[0])
    assert evidence.request_sha256 != request_hash(body)
    assert source_logical_request(evidence) == body
    decision = parse_source_response(evidence, PrivacyDecision, EDITOR_MODEL, request_hash(body))
    assert decision.identity == "www-f5-com:example"
    altered = evidence.model_copy(update={"request": body, "request_sha256": request_hash(body)})
    with pytest.raises(ValueError):
        parse_source_response(altered, PrivacyDecision, EDITOR_MODEL, request_hash(body))


def test_projection_proof_cannot_be_dropped(tmp_path: Path) -> None:
    body = logical_request()
    evidence = execute_source_request(body, Client(), tmp_path)
    missing = evidence.model_copy(update={"source_boundary": None})
    with pytest.raises(ValueError):
        parse_source_response(missing, PrivacyDecision, EDITOR_MODEL, missing.request_sha256)


def test_unprojectable_oversized_source_is_preserved_without_model_call(tmp_path: Path) -> None:
    payload = {"body": "a" * 1048577}
    body = request(EDITOR_MODEL, "Untrusted full source.", payload, PrivacyDecision)
    body["instructions"] = body["instructions"].replace("article-enrichment/1", "source-privacy/2")
    client = Client()
    with pytest.raises(ValueError, match="native character limit"):
        execute_source_request(body, client, tmp_path)
    assert not client.requests
    assert json.loads((tmp_path / "input-limit-request.json").read_bytes()) == body
    status = json.loads((tmp_path / "input-limit.json").read_bytes())
    assert status["inference_invoked"] is False
    assert status["characters"] > status["maximum_characters"]
