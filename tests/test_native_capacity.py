"""Decoded capacity cannot drop native source pixels or retry permanent failures."""

import base64
import io
import json
import subprocess
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from test_codex_worker import body, client
from test_native_context_views import native_frame
from test_native_visuals import image

from html_to_markdown.enrichment.codex_client import is_transient_error
from html_to_markdown.enrichment.visual_capacity import decoded_image_bytes
from html_to_markdown.media_observation_jobs import observation_request


@pytest.mark.parametrize(
    "error",
    [
        'Codex incomplete or failed response: {"error":{"code":"400","type":"invalid_request_error","message":"Total resolved image size is 516.85 MB, which exceeds the allowed limit of 512 MB."}}',
        "HTTP 400: the example contains 500 ms and 512 MB",
        "SchemaValidationError: the network timeout example contains HTTP 503",
        "invalid_json_schema: network connection field is malformed",
        "complete physical native attachments exceed decoded image capacity",
        "Unsupported enum 512",
    ],
)
def test_permanent_failures_are_not_transient(error):
    assert not is_transient_error(error)


@pytest.mark.parametrize(
    "error",
    [
        "429 Too Many Requests",
        "HTTP 503 Service Unavailable",
        '{"error":{"code":"504"}}',
        "ConnectionError: reset",
        "timed out after 1800 seconds",
    ],
)
def test_actual_transport_faults_remain_transient(error):
    assert is_transient_error(error)


def test_budget_reduces_supplemental_context_with_exact_source_and_region_pixels(monkeypatch):
    frame, visual = native_frame(300, 250)
    region = {"media_sha256": "a" * 64, "frame": 0, "rectangle": [140, 120, 160, 130]}
    context = {
        "identity": "doc",
        "binding_sha256": "b" * 64,
        "binding": {"privacy_version": "source-privacy/2"},
        "media": {"a" * 64: {"frames": [{"width": 300, "height": 250}]}},
        "observation_regions": [region],
        "source_article_html": "complete original source",
    }
    before = deepcopy(context)
    monkeypatch.setattr("html_to_markdown.media_observation_jobs.MAX_DECODED_BYTES", 1_500_000)
    request = observation_request(context, visual)
    assert request == observation_request(context, visual)
    content = request["input"][0]["content"]
    assert content[1:3] == visual and context == before
    assert json.loads(content[0]["text"])["source_article_html"] == "complete original source"
    assert decoded_image_bytes(content) <= 1_500_000
    description = content[-2]["text"]
    details, _ = json.JSONDecoder().raw_decode(description[description.index("{") :])
    assert details["rectangle"] == region["rectangle"]
    left, top, right, bottom = details["inspection_rectangle"]
    assert left < 140 < 160 < right and top < 120 < 130 < bottom
    assert (right - left, bottom - top) != frame.size
    with Image.open(
        io.BytesIO(base64.b64decode(content[-1]["image_url"].split(",", 1)[1]))
    ) as crop:
        assert crop.size == ((right - left) * 4, (bottom - top) * 4)
        assert (
            crop.resize((right - left, bottom - top), Image.Resampling.NEAREST).tobytes()
            == frame.crop((left, top, right, bottom)).tobytes()
        )


def test_original_frames_over_budget_fail_before_transport_and_preserve_request(
    tmp_path, monkeypatch
):
    request = body()
    request["input"][0]["content"].append(image(1))
    original = deepcopy(request)
    monkeypatch.setattr("html_to_markdown.enrichment.codex_client.MAX_DECODED_BYTES", 1)
    monkeypatch.setattr(
        "html_to_markdown.enrichment.codex_client.subprocess.run",
        lambda *args, **kwargs: pytest.fail("oversized input must not invoke inference"),
    )
    evidence = client(tmp_path).execute(request)
    assert evidence.error and "decoded image capacity" in evidence.error
    assert evidence.request == request == original
    attempts = list(tmp_path.glob("*.attempt-*.json"))
    assert len(attempts) == 1
    assert json.loads(attempts[0].read_bytes())["raw"]["invoked"] is False
    assert not list(tmp_path.glob("*.codex.json"))


def test_capacity_provider_failure_is_recorded_once_without_resubmission(tmp_path, monkeypatch):
    calls = []
    message = '{"type":"error","message":"{\\"error\\":{\\"type\\":\\"invalid_request_error\\",\\"code\\":\\"400\\",\\"message\\":\\"Total resolved image size is 516.85 MB, above the 512 MB limit.\\"}}"}'

    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=1, stdout=message, stderr="preserved diagnostic")

    monkeypatch.setattr("html_to_markdown.enrichment.codex_client.subprocess.run", run)
    evidence = client(tmp_path).execute(body())
    assert evidence.error and len(calls) == 1
    attempts = list(tmp_path.glob("*.attempt-*.json"))
    assert len(attempts) == 1
    assert json.loads(attempts[0].read_bytes())["raw"]["stdout"] == message
    assert not list(tmp_path.glob("*.codex.json"))


def test_bounded_sheets_keep_every_requested_region_and_original_frame(monkeypatch):
    _, visual = native_frame(200, 80)
    regions = [
        {"media_sha256": "a" * 64, "frame": 0, "rectangle": [i % 160, 20, i % 160 + 10, 25]}
        for i in range(159)
    ]
    context = {
        "identity": "doc",
        "binding_sha256": "b" * 64,
        "binding": {"privacy_version": "source-privacy/2"},
        "media": {"a" * 64: {"frames": [{"width": 200, "height": 80}]}},
        "observation_regions": regions,
    }
    monkeypatch.setattr("html_to_markdown.media_observation_jobs.MAX_DECODED_BYTES", 1_500_000)
    content = observation_request(context, visual)["input"][0]["content"]
    assert content[1:3] == visual
    assert decoded_image_bytes(content) <= 1_500_000
    observed = []
    for description in content[3::2]:
        text = description["text"]
        panels, _ = json.JSONDecoder().raw_decode(text[text.index("[") :])
        observed.extend(panel["rectangle"] for panel in panels)
    assert observed == [region["rectangle"] for region in regions]
    assert sum(v["type"] == "input_image" for v in content) <= 50


def test_malformed_transport_events_preserve_original_output(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "html_to_markdown.enrichment.codex_client.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout="{malformed event", stderr="original diagnostic"
        ),
    )
    evidence = client(tmp_path).execute(body())
    assert evidence.error
    raw = json.loads(next(tmp_path.glob("*.attempt-*.json")).read_bytes())["raw"]
    assert raw["stdout"] == "{malformed event" and raw["stderr"] == "original diagnostic"


def test_timeout_preserves_partial_transport_and_output_bytes(tmp_path, monkeypatch):
    def run(args, **kwargs):
        Path(args[args.index("--output-last-message") + 1]).write_bytes(b'{"partial":')
        raise subprocess.TimeoutExpired(
            args, 1800, output=b"partial stdout", stderr=b"partial stderr"
        )

    monkeypatch.setattr("html_to_markdown.enrichment.codex_client.subprocess.run", run)
    monkeypatch.setattr("html_to_markdown.enrichment.codex_client.time.sleep", lambda delay: None)
    evidence = client(tmp_path).execute(body())
    assert evidence.error and len(list(tmp_path.glob("*.attempt-*.json"))) == 3
    for attempt in tmp_path.glob("*.attempt-*.json"):
        raw = json.loads(attempt.read_bytes())["raw"]
        assert base64.b64decode(raw["stdout_base64"]) == b"partial stdout"
        assert base64.b64decode(raw["stderr_base64"]) == b"partial stderr"
        assert base64.b64decode(raw["partial_output_base64"]) == b'{"partial":'
