"""Native endpoints are explicit, validated and version-one requests remain exact."""

import json
from pathlib import Path

import pytest

from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.enrichment.contracts import ResponseEvidence
from html_to_markdown.enrichment.gates import request_hash
from html_to_markdown.media_observation_jobs import confirmed_media_values, observation_request
from html_to_markdown.media_observations import MediaObservations, observation_payload

LEGACY = json.loads(
    (Path(__file__).parent / "golden/native-observation-v1.requests.json").read_bytes()
)


def test_new_requests_declare_original_frame_endpoint_coordinates():
    """Model schema and instructions must agree on the exact coordinate convention."""
    body = observation_request(LEGACY["context"], LEGACY["visual"])
    assert body["instructions"].endswith("native-media-observation/2")
    rectangle = body["text"]["format"]["schema"]["$defs"]["VisibleValue"]["properties"]["rectangle"]
    assert rectangle["minItems"] == rectangle["maxItems"] == 4
    assert rectangle["items"]["minimum"] == 0
    for text in [body["instructions"], rectangle["description"]]:
        assert "left, top, right, bottom" in text
        assert "width, height" in text
        assert "original" in text and "exclusive" in text


@pytest.mark.parametrize("kind", ["editor", "validator"])
def test_version_one_request_schema_and_instructions_replay_exactly(kind):
    """Historical evidence cannot be rebound to the new request contract."""
    payload = LEGACY["payload"] if kind == "validator" else None
    assert (
        observation_request(
            LEGACY["context"], LEGACY["visual"], payload, version="native-media-observation/1"
        )
        == LEGACY[kind]
    )


@pytest.mark.parametrize("rectangle", [[1054, 320, 109, 32], [-1, 0, 100, 20], [0, 0, 3457, 20]])
def test_malformed_endpoints_report_index_dimensions_and_convention_without_values(rectangle):
    """Bad coordinates are rejected without inferred repair or identifying-value disclosure."""
    value = MediaObservations(
        identity="example",
        binding_sha256="b" * 64,
        complete_media_reviewed=True,
        unresolved=[],
        observations=[
            {
                "media_sha256": "a" * 64,
                "frame": 0,
                "rectangle": rectangle,
                "value": "DO_NOT_ECHO_THIS",
                "reason": "Candidate",
            }
        ],
    )
    with pytest.raises(ValueError) as error:
        observation_payload(
            "example", "b" * 64, value, {"a" * 64: {"frames": [{"width": 3456, "height": 1838}]}}
        )
    message = str(error.value)
    assert "observation 0" in message and "3456" in message and "1838" in message
    assert "left, top, right, bottom" in message and "width, height" in message
    assert "DO_NOT_ECHO_THIS" not in message
    assert value.observations[0].rectangle == rectangle


def test_valid_endpoints_keep_original_region_and_digest():
    """Clarifying the contract cannot alter a valid historical payload."""
    values = [
        {key: value for key, value in item.items() if key not in {"value_sha256", "guard_kind"}}
        for item in LEGACY["payload"]["values"]
    ]
    proposal = MediaObservations(
        identity=LEGACY["context"]["identity"],
        binding_sha256=LEGACY["context"]["binding_sha256"],
        observations=values,
        complete_media_reviewed=True,
        unresolved=[],
    )
    assert (
        observation_payload(
            proposal.identity, proposal.binding_sha256, proposal, LEGACY["context"]["media"]
        )
        == LEGACY["payload"]
    )


def test_version_one_complete_native_evidence_replays_without_new_inference():
    """Both historical requests and their complete evidence retain exact identity."""
    values = [
        {key: value for key, value in item.items() if key not in {"value_sha256", "guard_kind"}}
        for item in LEGACY["payload"]["values"]
    ]
    proposal = {
        "identity": LEGACY["context"]["identity"],
        "binding_sha256": LEGACY["context"]["binding_sha256"],
        "observations": values,
        "complete_media_reviewed": True,
        "unresolved": [],
    }
    validation = {
        "identity": proposal["identity"],
        "binding_sha256": proposal["binding_sha256"],
        "observations_sha256": LEGACY["payload"]["observations_sha256"],
        "approved_indices": [0],
        "complete_media_reviewed": True,
        "failures": [],
        "unresolved": [],
    }

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
            prompt_version="native-media-observation/1",
            request=body,
            response=response,
            error=None,
            transport="codex_litellm",
            runtime_version="test",
        ).model_dump()

    saved = {
        "phases": {
            "media_observation": evidence(LEGACY["editor"], proposal),
            "media_observation_validation": evidence(LEGACY["validator"], validation),
        }
    }
    assert (
        confirmed_media_values(saved, LEGACY["context"], LEGACY["visual"])
        == LEGACY["payload"]["values"]
    )
