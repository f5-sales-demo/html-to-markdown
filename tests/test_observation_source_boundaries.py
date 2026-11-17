"""Complete observation intake must reach independent native boundary review."""

import base64
import io
import json
from pathlib import Path

import pytest
from PIL import Image
from test_source_native_boundaries import Client, image_content

import html_to_markdown.enrichment.native_visuals as native
import html_to_markdown.media_observation_jobs as observations
import html_to_markdown.source_boundary_requests as boundaries
import html_to_markdown.source_review_jobs as jobs
from html_to_markdown.curation import json_bytes
from html_to_markdown.media_observation_jobs import confirmed_media_values, observation_request


def inputs(count=3, size=(8, 8)):
    media, visual = {}, []
    for index in range(count):
        digest = f"{index:064x}"
        media[digest] = {"frames": [{"width": size[0], "height": size[1]}]}
        visual.extend(
            [
                {"type": "input_text", "text": f"media/{digest}/0"},
                image_content(index, size),
            ]
        )
    return {
        "identity": "source:article",
        "binding_sha256": "b" * 64,
        "source_article_html": "<article>Retain prerequisite TLS and curl --retry 3.</article>",
        "body": "Retain all 30 second limits before and after every image.",
        "media": media,
    }, visual


@pytest.fixture
def limit(monkeypatch):
    for module in [native, observations, boundaries]:
        monkeypatch.setattr(module, "MAX_DECODED_BYTES", 600)


def test_actual_privacy_intake_observes_and_replays_all_over_limit_frames(
    limit, tmp_path, monkeypatch
):
    context, visual = inputs()
    original_context = json.loads(json_bytes(context))
    record = tmp_path / "document.json"
    feedback = ["Existing privacy correction remains authoritative"]
    record.write_bytes(
        json_bytes(
            {
                "phases": {},
                "item": {"media": [{}]},
                "privacy_feedback": feedback,
            }
        )
    )
    monkeypatch.setattr(jobs, "_privacy_observation_context", lambda *args: (context, visual))
    client = Client()
    saved = jobs._privacy_media_intake(
        tmp_path, tmp_path, record, "privacy", client, context, visual
    )
    assert saved["privacy_feedback"] == feedback
    for phase in ["media_observation", "media_observation_validation"]:
        proof = saved["phases"][phase]["source_boundary"]
        assert len(proof["parts"]) == 2
        assert len(proof["coverage_plan"]["positions"]) == 3
    assert context["confirmed_media_values"] == []
    assert confirmed_media_values(saved, original_context, visual) == []
    assert "privacy" not in saved["phases"]  # Observation alone authorizes no edit.
    for call in client.calls:
        native.native_visual_view(call)


@pytest.mark.parametrize("count", [50, 54])
@pytest.mark.parametrize("with_region", [True, False])
def test_complete_regions_and_boundaries_survive_more_than_fifty_original_frames(
    count, with_region
):
    context, visual = inputs(count)
    context.update(
        binding={"privacy_version": "source-privacy/2"},
        observation_regions=[{"media_sha256": "0" * 64, "frame": 0, "rectangle": [1, 1, 3, 3]}],
        observation_feedback=["Inspect native boundary " + "0" * 64],
    )
    if not with_region:
        context.pop("observation_regions")
    before = json_bytes(visual)
    body = observation_request(context, visual)
    content = body["input"][0]["content"]
    assert content[1 : 1 + len(visual)] == visual and json_bytes(visual) == before
    extra = content[1 + len(visual) :]
    assert len(extra) == (
        4 if with_region else 2
    )  # Exact region plus complete geometry-only boundary tile.
    with Image.open(io.BytesIO(base64.b64decode(extra[1]["image_url"].split(",", 1)[1]))) as crop:
        assert crop.resize((8, 8), Image.Resampling.NEAREST).tobytes() == bytes(8 * 8 * 3)
    plan = boundaries.boundary_plan(body)
    assert plan and len(plan["positions"]) == count + len(extra) // 2
    for part in plan["partitions"]:
        native.native_visual_view(boundaries.partition_request(body, plan, part))
    assert sum(len(part["positions"]) for part in plan["partitions"]) == count + len(extra) // 2


def test_unapproved_native_part_never_supplies_confirmed_media_values(limit, tmp_path, monkeypatch):
    context, visual = inputs()
    record = tmp_path / "document.json"
    record.write_bytes(json_bytes({"phases": {}, "item": {"media": [{}]}}))
    monkeypatch.setattr(jobs, "_privacy_observation_context", lambda *args: (context, visual))
    client = Client()
    client.reject_partition = True
    with pytest.raises(ValueError, match="independent native source boundary"):
        jobs._privacy_media_intake(tmp_path, tmp_path, record, "privacy", client, context, visual)
    assert "media_observation_validation" not in json.loads(record.read_bytes())["phases"]
    assert "confirmed_media_values" not in context


def test_oversized_individual_frame_remains_explicitly_unreviewable(limit):
    context, visual = inputs(1, size=(16, 16))
    body = observation_request(context, visual)
    with pytest.raises(ValueError, match="image or compared pair exceeds"):
        boundaries.boundary_plan(body)


@pytest.mark.parametrize("role", ["editor", "validator"])
def test_version_two_observation_requests_replay_exactly(role):
    fixture = json.loads(
        (Path(__file__).parent / "golden/native-observation-v2.requests.json").read_bytes()
    )
    payload = fixture["payload"] if role == "validator" else None
    assert (
        observation_request(
            fixture["context"], fixture["visual"], payload, version="native-media-observation/2"
        )
        == fixture[role]
    )


def test_individually_oversized_region_is_not_dropped(limit):
    context, visual = inputs()
    context["observation_regions"] = [
        {"media_sha256": "0" * 64, "frame": 0, "rectangle": [0, 0, 8, 8]},
    ]
    with pytest.raises(ValueError, match="individual native source-region view"):
        observation_request(context, visual)


def test_stale_complete_observation_parts_cannot_be_rebound(limit, tmp_path, monkeypatch):
    context, visual = inputs()
    record = tmp_path / "document.json"
    record.write_bytes(json_bytes({"phases": {}, "item": {"media": [{}]}}))
    original_context = json.loads(json_bytes(context))
    monkeypatch.setattr(jobs, "_privacy_observation_context", lambda *args: (context, visual))
    saved = jobs._privacy_media_intake(
        tmp_path, tmp_path, record, "privacy", Client(), context, visual
    )
    original_context["body"] += " Changed action order."
    with pytest.raises(ValueError, match="stale complete logical source"):
        confirmed_media_values(saved, original_context, visual)


def test_independent_value_validation_rejection_cannot_confirm_an_observation(
    limit, tmp_path, monkeypatch
):
    context, visual = inputs()
    record = tmp_path / "document.json"
    original_feedback = ["Existing privacy correction"]
    record.write_bytes(
        json_bytes({"phases": {}, "item": {"media": [{}]}, "privacy_feedback": original_feedback})
    )
    monkeypatch.setattr(jobs, "_privacy_observation_context", lambda *args: (context, visual))

    class RejectValueClient(Client):
        def execute(self, body):
            payload = json.loads(body["input"][0]["content"][0]["text"])
            self.reject_partition = (
                body["text"]["format"]["name"] == "boundaryvalidation"
                and payload["original_phase_schema"] == "observationvalidation"
            )
            return super().execute(body)

    client = RejectValueClient()
    with pytest.raises(ValueError, match="independent native source boundary"):
        jobs._privacy_media_intake(tmp_path, tmp_path, record, "privacy", client, context, visual)
    saved = json.loads(record.read_bytes())
    assert "media_observation" in saved["phases"]
    assert "media_observation_validation" not in saved["phases"]
    assert saved["privacy_feedback"] == original_feedback
    assert "confirmed_media_values" not in context
    assert all(call["text"]["format"]["name"] != "privacydecision" for call in client.calls)
