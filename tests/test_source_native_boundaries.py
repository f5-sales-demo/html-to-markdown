"""Full native source coverage is required before complete article admission."""

from __future__ import annotations

import base64
import io
import json
import shutil
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from PIL import Image
from test_source_review_jobs import LocalClient

import html_to_markdown.enrichment.native_visuals as native
import html_to_markdown.source_boundary_requests as requests
import html_to_markdown.source_review_jobs as jobs
import html_to_markdown.source_review_reconciliation as reconciliation
from html_to_markdown.cross_queue_reviews import seed_review_batch
from html_to_markdown.curation import json_bytes
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.enrichment.contracts import EDITOR_MODEL, ResponseEvidence, StrictModel
from html_to_markdown.enrichment.gates import parse_response, request_hash
from html_to_markdown.enrichment.media import all_frame_inputs
from html_to_markdown.enrichment.requests import request
from html_to_markdown.errors import TransportFailure
from html_to_markdown.render import split_document
from html_to_markdown.source_boundary_review import (
    execute_cached_source,
    execute_source_request,
    has_retryable_boundary,
    parse_source_response,
    source_logical_request,
)
from html_to_markdown.source_phase_bindings import independently_validated_privacy


class Probe(StrictModel):
    identity: str
    retained_facts: list[str]


def image_content(index: int, size=(8, 8)) -> dict:
    stream = io.BytesIO()
    Image.new("RGB", size, (index, index * 3 % 255, index * 7 % 255)).save(stream, format="PNG")
    return {
        "type": "input_image",
        "detail": "high",
        "image_url": "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode(),
    }


def body(count=4, *, paired=False) -> dict:
    source: dict[str, Any] = {
        "identity": "article",
        "body": "Prerequisite: retain TLS. Run `curl --retry 3` within 30 seconds.",
        "source_article_html": "<article>All source instructions before and after images.</article>",
    }
    if paired:
        source["media"] = {str(i): {"frames": [{"frame": 0}]} for i in range(count // 2)}
        source["candidate"] = {
            "media": {str(i): {"frames": [{"frame": 0}]} for i in range(count // 2)}
        }
    result = request(
        EDITOR_MODEL, "Review the complete source and all protected facts.", source, Probe
    )
    for index in range(count):
        result["input"][0]["content"].extend(
            [{"type": "input_text", "text": "media/source/" + str(index)}, image_content(index)]
        )
    return result


@pytest.fixture
def small_limit(monkeypatch):
    monkeypatch.setattr(native, "MAX_DECODED_BYTES", 600)
    monkeypatch.setattr(requests, "MAX_DECODED_BYTES", 600)


def evidence(body, value, *, error=None, events=None):
    response = (
        {}
        if error
        else {
            "status": "completed",
            "model": body["model"],
            "output": [{"content": [{"type": "output_text", "text": json.dumps(value)}]}],
        }
    )
    if events is not None:
        response["events"] = events
    return ResponseEvidence(
        request_sha256=request_hash(body),
        response_sha256=sha(json.dumps(response, sort_keys=True, separators=(",", ":"))),
        model=body["model"],
        reasoning="high",
        prompt_version=body["instructions"].rsplit("Prompt version: ", 1)[-1].strip(),
        request=body,
        response=response,
        error=error,
        transport="codex_litellm",
        runtime_version="test",
    )


class Client(LocalClient):
    def __init__(self):
        self.calls = []
        self.invalidated = []
        self.reject_partition = False
        self.fail_once = None
        self.fail_phase_once = None
        self.fail_semantic_once = False

    def invalidate(self, item):
        self.invalidated.append(item)

    def execute(self, body):
        current = body
        self.calls.append(current)
        payload = json.loads(current["input"][0]["content"][0]["text"])
        name = current["text"]["format"]["name"]
        if (
            name == "boundaryvalidation"
            and self.fail_phase_once == payload["original_phase_schema"]
        ):
            self.fail_phase_once = None
            return evidence(current, {}, error="HTTP 503 temporarily unavailable")
        if self.fail_once == name:
            self.fail_once = None
            return evidence(current, {}, error="HTTP 503 temporarily unavailable")
        if name in {"boundaryanalysis", "boundaryvalidation"}:
            scope = payload["assigned_partition"]
            output = {
                "logical_request_sha256": payload["logical_request_sha256"],
                "partition_sha256": scope["partition_sha256"],
            }
            exact_visuals, _ = native.native_visual_view(current)
            assert len(exact_visuals) == len(scope["positions"])
            assert payload["complete_original_phase_text"]
            if name == "boundaryanalysis":
                output.update(
                    complete_text_reviewed=True,
                    technical_relationships=[
                        "Complete source and candidate retain the supplied command order and numerical bounds."
                    ],
                    unresolved=[],
                    visuals=[
                        {
                            "position": position["position"],
                            "content_sha256": position["content_sha256"],
                            "description": "Complete synthetic solid-color image; no visible text or identifying values.",
                            "visible_text": [],
                            "technical_facts": [],
                            "privacy_context": [],
                            "complete_visual_reviewed": True,
                            "uncertain": False,
                        }
                        for position in scope["positions"]
                    ],
                )
                if self.fail_semantic_once:
                    self.fail_semantic_once = False
                    output["complete_text_reviewed"] = False
            else:
                output.update(
                    analysis_sha256=payload["analysis_sha256"],
                    covered_positions=[p["position"] for p in scope["positions"]],
                    approved=not self.reject_partition,
                    complete_text_reviewed=True,
                    complete_assigned_visuals_reviewed=True,
                    all_necessary_facts_captured=True,
                    assigned_phase_checks=[
                        {
                            "name": key,
                            "passed": not self.reject_partition,
                            "reason": "Exact assigned original phase checks pass",
                        }
                        for key in payload["required_assigned_phase_checks"]
                    ],
                    relationships_and_privacy_context_checked=True,
                    failures=["A necessary command fact is missing"]
                    if self.reject_partition
                    else [],
                    unresolved=[],
                )
        elif name == "probe":
            output = {
                "identity": payload["identity"],
                "retained_facts": ["TLS", "curl --retry 3", "30 seconds"],
            }
        elif name == "finalclassification":
            output = {
                "identity": payload["source"]["identity"],
                "binding_sha256": payload["source"]["binding_sha256"],
                "candidate_sha256": payload["candidate_sha256"],
                "findings": [],
            }
        else:
            return super().execute(current)
        Draft202012Validator(current["text"]["format"]["schema"]).validate(output)
        return evidence(current, output)


def test_complete_distinct_images_require_bounded_independent_review(small_limit, tmp_path):
    original = body()
    before = json_bytes(original)
    with pytest.raises(ValueError, match="decoded image capacity"):
        native.native_visual_view(original)
    client = Client()
    result = execute_source_request(original, client, tmp_path / "journal")
    assert json_bytes(original) == before
    assert len(result.source_boundary["parts"]) == 2
    assert len(client.calls) == 5  # two independently validated parts, one actual final decision
    for leaf in client.calls[:-1]:
        native.native_visual_view(leaf)
        payload = json.loads(leaf["input"][0]["content"][0]["text"])
        assert payload["complete_original_phase_text"] == requests.text_request(original)["input"]
    assert result.request_sha256 != request_hash(original)
    assert source_logical_request(result) == original
    assert parse_source_response(
        result, Probe, EDITOR_MODEL, request_hash(original)
    ).retained_facts == ["TLS", "curl --retry 3", "30 seconds"]
    with pytest.raises(ValueError, match="scoped replay"):
        parse_response(result, Probe, EDITOR_MODEL, result.request_sha256)
    assert (tmp_path / "journal/logical-request.json").read_bytes() == before


def test_normal_request_keeps_historical_response_and_native_input(tmp_path):
    original = body(1)
    client = Client()
    result = execute_source_request(original, client, tmp_path / "journal")
    assert result.source_boundary is None and len(client.calls) == 1
    assert result.request == original
    assert "source_boundary" not in result.model_dump()
    assert not (tmp_path / "journal").exists()
    assert (
        parse_source_response(result, Probe, EDITOR_MODEL, request_hash(original)).identity
        == "article"
    )


def test_original_candidate_frame_pairs_are_never_split(small_limit):
    original = body(8, paired=True)
    plan = requests.boundary_plan(original)
    assert len(plan["partitions"]) == 4
    all_positions = []
    for index, part in enumerate(plan["partitions"]):
        positions = [p["position"] for p in part["positions"]]
        assert positions == [f"0:{2 + index * 2}", f"0:{10 + index * 2}"]
        all_positions.extend(positions)
    assert set(all_positions) == {p["position"] for p in plan["positions"]}


@pytest.mark.parametrize("paired", [False, True])
def test_oversized_single_image_or_pair_stays_visible(small_limit, paired):
    original = body(4, paired=paired)
    original["input"][0]["content"][2] = image_content(1, (20, 20) if not paired else (12, 12))
    with pytest.raises(ValueError, match="image or compared pair"):
        requests.boundary_plan(original)


def test_mismatched_candidate_frame_count_is_rejected(small_limit):
    original = body(8, paired=True)
    payload = json.loads(original["input"][0]["content"][0]["text"])
    payload["candidate"]["media"]["0"]["frames"].append({"frame": 1})
    original["input"][0]["content"][0]["text"] = json.dumps(payload)
    with pytest.raises(ValueError, match="frame pairing"):
        requests.boundary_plan(original)


def _mutate_leaf(proof, mutation):
    kind = (
        "validation"
        if mutation
        in {
            "independent_failure",
            "missing_independent_position",
            "unchecked_phase",
            "missing_phase",
            "unknown_phase",
        }
        else "analysis"
    )
    leaf = proof["parts"][0][kind]
    parsed = json.loads(leaf["response"]["output"][0]["content"][0]["text"])
    if mutation == "wrong_position":
        parsed["visuals"][0]["position"] = "0:999"
    elif mutation == "wrong_hash":
        parsed["visuals"][0]["content_sha256"] = "0" * 64
    elif mutation == "missing_visual":
        parsed["visuals"].pop()
    elif mutation == "unchecked_text":
        parsed["complete_text_reviewed"] = False
    elif mutation == "unchecked_visual":
        parsed["visuals"][0]["complete_visual_reviewed"] = False
    elif mutation == "uncertain_visual":
        parsed["visuals"][0]["uncertain"] = True
    elif mutation == "independent_failure":
        parsed["approved"] = False
    elif mutation == "missing_independent_position":
        parsed["covered_positions"].pop()
    elif mutation == "unchecked_phase":
        parsed["assigned_phase_checks"][0]["passed"] = False
    elif mutation == "missing_phase":
        parsed["assigned_phase_checks"].pop()
    elif mutation == "unknown_phase":
        parsed["assigned_phase_checks"][0]["name"] = "unreviewed"
    leaf["response"]["output"][0]["content"][0]["text"] = json.dumps(parsed)
    leaf["response_sha256"] = sha(
        json.dumps(leaf["response"], sort_keys=True, separators=(",", ":"))
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_part",
        "duplicate_part",
        "reordered_part",
        "wrong_position",
        "wrong_hash",
        "missing_visual",
        "unchecked_text",
        "unchecked_visual",
        "uncertain_visual",
        "independent_failure",
        "unchecked_phase",
        "missing_phase",
        "unknown_phase",
        "missing_independent_position",
        "rebound_final",
        "changed_caption",
        "unknown_version",
        "nested_aggregate",
    ],
)
def test_offline_replay_rejects_incomplete_stale_or_failed_evidence(
    small_limit, tmp_path, mutation
):
    original = body()
    result = execute_source_request(original, Client(), tmp_path / "journal")
    changed = deepcopy(result.model_dump())
    proof = changed["source_boundary"]
    if mutation == "missing_part":
        proof["parts"].pop()
    elif mutation == "duplicate_part":
        proof["parts"][1] = deepcopy(proof["parts"][0])
    elif mutation == "reordered_part":
        proof["parts"].reverse()
    elif mutation == "unknown_version":
        proof["version"] = "unreviewed/2"
    elif mutation == "changed_caption":
        proof["logical_request"]["input"][0]["content"][1]["text"] = "Changed task caption"
    elif mutation == "rebound_final":
        changed["request"] = deepcopy(original)
        changed["request_sha256"] = request_hash(original)
    elif mutation == "nested_aggregate":
        proof["parts"][0]["analysis"]["source_boundary"] = deepcopy(proof)
    else:
        _mutate_leaf(proof, mutation)
    with pytest.raises(ValueError):
        parse_source_response(
            ResponseEvidence.model_validate(changed), Probe, EDITOR_MODEL, request_hash(original)
        )


def test_final_decision_cannot_drop_its_boundary_proof(small_limit, tmp_path):
    original = body()
    result = execute_source_request(original, Client(), tmp_path / "journal")
    stripped = result.model_copy(update={"source_boundary": None})
    with pytest.raises(ValueError, match="missing its complete boundary"):
        parse_source_response(stripped, Probe, EDITOR_MODEL, stripped.request_sha256)
    with pytest.raises(ValueError, match="stale complete logical"):
        parse_source_response(result, Probe, EDITOR_MODEL, result.request_sha256)


def test_interruption_resumes_validated_parts_without_new_observation_calls(small_limit, tmp_path):
    original = body()
    client = Client()
    client.fail_once = "boundaryvalidation"
    with pytest.raises(TransportFailure):
        execute_source_request(original, client, tmp_path / "journal")
    assert len(client.calls) == 2
    assert (tmp_path / "journal/0/analysis.json").exists()
    result = execute_source_request(original, client, tmp_path / "journal")
    assert (
        len(client.calls) == 6
    )  # reuse completed analysis, retry validation; no extra proposal round
    assert not client.invalidated  # transport failure is not semantic proposal rejection
    assert (
        parse_source_response(result, Probe, EDITOR_MODEL, request_hash(original)).identity
        == "article"
    )


def test_failed_part_cannot_reach_final_decision_and_preserves_attempt(small_limit, tmp_path):
    client = Client()
    client.reject_partition = True
    with pytest.raises(ValueError, match="preservation validation"):
        execute_source_request(body(), client, tmp_path / "journal")
    assert len(client.calls) == 2
    assert (tmp_path / "journal/0/validation.json").exists()
    assert list((tmp_path / "journal/0/history").glob("*.json"))
    assert not (tmp_path / "journal/final-response.json").exists()


def test_semantically_failed_checkpoint_is_not_permanently_cached(small_limit, tmp_path):
    client = Client()
    client.fail_semantic_once = True
    with pytest.raises(ValueError, match="complete bound coverage"):
        execute_source_request(body(), client, tmp_path / "journal")
    result = execute_source_request(body(), client, tmp_path / "journal")
    assert (
        parse_source_response(result, Probe, EDITOR_MODEL, request_hash(body())).identity
        == "article"
    )
    assert len(client.calls) == 6
    assert list((tmp_path / "journal/0/history").glob("*.json"))


def test_cached_complete_boundary_source_replays_with_no_client(small_limit, tmp_path):
    original = body()
    path = tmp_path / "evidence.json"
    client = Client()
    result = execute_cached_source(path, original, client)
    replayed = execute_cached_source(path, original, None)
    assert result == replayed and len(client.calls) == 5
    changed = deepcopy(original)
    changed["input"][0]["content"][1]["text"] = "Different caption or policy context"
    with pytest.raises(ValueError, match="stale complete"):
        execute_cached_source(path, changed, client)


def test_full_source_admission_roundtrip_preserves_complete_text_media_and_budget(
    capture, small_limit, tmp_path, monkeypatch
):
    item = _capture_with_images(capture)
    output = tmp_path / "review"
    jobs.prepare_reviews(capture, output)
    record = next((output / "documents").glob("*.json"))
    saved = json.loads(record.read_bytes())
    saved["privacy_feedback"] = [
        "First independently checked correction",
        "Second independently checked correction",
    ]
    record.write_bytes(json_bytes(saved))
    client = Client()
    monkeypatch.setattr(jobs, "CodexClient", lambda *args, **kwargs: client)
    report = jobs.run_reviews(capture, output, phase="all", journal=tmp_path / "codex-journal")
    assert report["documents"][0]["disposition"] == "accepted"
    preserved = json.loads(record.read_bytes())
    assert preserved["privacy_feedback"] == saved["privacy_feedback"]
    assert all(
        preserved["phases"][phase].get("source_boundary")
        for phase in [
            "relevance",
            "privacy",
            "validation",
            "media_observation",
            "media_observation_validation",
        ]
    )
    replayed = jobs.verify_reviews(capture, output)
    files = {path: (output / "reviewed" / path).read_bytes() for path in replayed["accepted_files"]}
    markdown = next(value.decode() for path, value in files.items() if path.endswith("index.md"))
    assert "curl --retry 3" in markdown and "30 seconds" in markdown
    assets = [data for path, data in files.items() if "/assets/" in path]
    assert {sha(data) for data in assets} == {entry["sha256"] for entry in item["media"]}
    assert split_document(markdown)[0]["url"] == item["url"]


def _capture_with_images(capture):
    inventory = json.loads((capture / "inventory.json").read_bytes())
    item = inventory["documents"][0]
    article = (capture / item["article_path"]).read_text()
    for index in range(4):
        raw = base64.b64decode(image_content(index)["image_url"].split(",", 1)[1])
        digest = sha(raw)
        relative = str(Path(item["article_path"]).parent / "assets" / digest)
        (capture / relative).parent.mkdir(parents=True, exist_ok=True)
        (capture / relative).write_bytes(raw)
        item["media"].append(
            {
                "url": f"https://my.f5.com/images/{index}.png",
                "sha256": digest,
                "path": relative,
                "media_type": "image/png",
            }
        )
        article += f"\n![Useful procedure image {index}](assets/{digest})\n"
    (capture / item["article_path"]).write_text(article)
    item["article_sha256"] = sha(article)
    (capture / "inventory.json").write_bytes(json_bytes(inventory))
    return item


@pytest.mark.parametrize(
    "phase",
    [
        "relevancedecision",
        "mediaobservations",
        "observationvalidation",
        "privacydecision",
        "admissionvalidation",
    ],
)
def test_partial_transport_failure_stays_retryable_without_resetting_privacy_round(
    capture, small_limit, tmp_path, monkeypatch, phase
):
    _capture_with_images(capture)
    output = tmp_path / "review"
    jobs.prepare_reviews(capture, output)
    record = next((output / "documents").glob("*.json"))
    saved = json.loads(record.read_bytes())
    saved["privacy_feedback"] = ["First semantic correction", "Second semantic correction"]
    record.write_bytes(json_bytes(saved))
    client = Client()
    client.fail_phase_once = phase
    monkeypatch.setattr(jobs, "CodexClient", lambda *args, **kwargs: client)
    first = jobs.run_reviews(
        capture, output, phase="all", journal=tmp_path / "codex-journal", limit=1
    )
    assert first["pending"] == 1 and first["documents"][0]["disposition"] == "queued"
    pending = json.loads(record.read_bytes())
    assert pending["privacy_feedback"] == saved["privacy_feedback"]
    assert pending["retryable"] and has_retryable_boundary(pending)
    assert not (output / "reviewed").exists()
    second = jobs.run_reviews(
        capture, output, phase="all", journal=tmp_path / "codex-journal", limit=1
    )
    assert second["pending"] == 0 and second["documents"][0]["disposition"] == "accepted"
    completed = json.loads(record.read_bytes())
    assert completed["privacy_feedback"] == saved["privacy_feedback"]
    assert not has_retryable_boundary(completed)
    assert sum(call["text"]["format"]["name"] == "privacydecision" for call in client.calls) == 1
    assert independently_validated_privacy(completed)
    stripped = deepcopy(completed)
    stripped["phases"]["privacy"].pop("source_boundary")
    assert not independently_validated_privacy(stripped)


def test_partial_first_relevance_crosses_queues_with_complete_native_checkpoints(
    capture, small_limit, tmp_path, monkeypatch
):
    _capture_with_images(capture)
    worker = tmp_path / "worker"
    prior_capture = worker / "previous/my-f5-com/capture"
    shutil.copytree(capture, prior_capture)
    prior = worker / "previous/my-f5-com/review"
    jobs.prepare_reviews(prior_capture, prior)
    first_client = Client()
    first_client.fail_phase_once = "relevancedecision"
    monkeypatch.setattr(jobs, "CodexClient", lambda *args, **kwargs: first_client)
    report = jobs.run_reviews(
        prior_capture, prior, phase="all", journal=tmp_path / "journal", limit=1
    )
    assert report["pending"] == 1
    prior_record = next((prior / "documents").glob("*.json"))
    before = prior_record.read_bytes()
    assert not json.loads(before)["phases"]
    current_capture = worker / "current/my-f5-com/capture"
    shutil.copytree(capture, current_capture)
    current = worker / "current/my-f5-com/review"
    jobs.prepare_reviews(current_capture, current)
    seeded = seed_review_batch(current_capture, current, worker, limit=1)
    assert len(seeded["seeded"]) == 1 and not seeded["exceptions"]
    assert prior_record.read_bytes() == before
    assert list((current / "native-boundaries").glob("*/0/analysis.json"))
    second_client = Client()
    monkeypatch.setattr(jobs, "CodexClient", lambda *args, **kwargs: second_client)
    result = jobs.run_reviews(
        current_capture, current, phase="all", journal=tmp_path / "journal", limit=1
    )
    assert result["documents"][0]["disposition"] == "accepted"
    relevance_analyses = [
        call
        for call in first_client.calls + second_client.calls
        if call["text"]["format"]["name"] == "boundaryanalysis"
        and json.loads(call["input"][0]["content"][0]["text"])["original_phase_schema"]
        == "relevancedecision"
    ]
    assert (
        len(relevance_analyses) == 2
    )  # the copied complete first part never gets another model call
    assert prior_record.read_bytes() == before


def test_count_limit_partitions_all_distinct_images_and_preserves_shared_appearances(tmp_path):
    original = body(51)
    result = execute_source_request(original, Client(), tmp_path / "distinct")
    proof = result.source_boundary
    assert [len(part["positions"]) for part in proof["coverage_plan"]["partitions"]] == [50, 1]
    assert len(proof["coverage_plan"]["positions"]) == 51
    repeated = body(51)
    repeated["input"][0]["content"] = [
        image_content(1) if content["type"] == "input_image" else content
        for content in repeated["input"][0]["content"]
    ]
    preserved = execute_source_request(repeated, Client(), tmp_path / "repeated")
    assert preserved.source_boundary is None and preserved.request == repeated


def test_complete_animation_frames_timing_and_native_pixels_are_preserved(small_limit, tmp_path):
    path = tmp_path / "animation.gif"
    images = [Image.new("RGB", (8, 8), (i * 35, i * 45, i * 55)) for i in range(3)]
    images[0].save(
        path,
        save_all=True,
        append_images=images[1:],
        duration=[100, 200, 300],
        loop=0,
        optimize=False,
    )
    raw = path.read_bytes()
    frames = all_frame_inputs(path, perform_ocr=False)
    original = body(0)
    payload = json.loads(original["input"][0]["content"][0]["text"])
    payload["media"] = {sha(raw): {"frames": [info for _, info in frames]}}
    original["input"][0]["content"][0]["text"] = json.dumps(payload)
    for url, info in frames:
        original["input"][0]["content"].extend(
            [
                {"type": "input_text", "text": f"media/{sha(raw)}/{info['frame']}"},
                {"type": "input_image", "image_url": url, "detail": "high"},
            ]
        )
    result = execute_source_request(original, Client(), tmp_path / "parts")
    assert path.read_bytes() == raw
    preserved = source_logical_request(result)
    metadata = json.loads(preserved["input"][0]["content"][0]["text"])["media"][sha(raw)]["frames"]
    assert [info["duration"] for info in metadata] == [100, 200, 300]
    assert len(result.source_boundary["coverage_plan"]["positions"]) == 3
    assert [
        content for content in original["input"][0]["content"] if content["type"] == "input_image"
    ] == [
        content for content in preserved["input"][0]["content"] if content["type"] == "input_image"
    ]


@pytest.mark.parametrize(
    "event",
    [
        {"type": "context.compacted"},
        {"type": "thread.compaction.started"},
        {"type": "item.completed", "item": {"type": "mcp_tool_call"}},
        {"type": "turn.failed"},
    ],
)
def test_compaction_tools_and_failed_turns_never_contribute_boundary_approval(
    small_limit, tmp_path, event
):
    original = body()
    result = execute_source_request(original, Client(), tmp_path / "parts")
    changed = deepcopy(result.model_dump())
    leaf = changed["source_boundary"]["parts"][0]["validation"]
    leaf["response"]["events"] = [event]
    leaf["response_sha256"] = sha(
        json.dumps(leaf["response"], sort_keys=True, separators=(",", ":"))
    )
    with pytest.raises(ValueError):
        parse_source_response(
            ResponseEvidence.model_validate(changed), Probe, EDITOR_MODEL, request_hash(original)
        )


@pytest.mark.parametrize(
    "path_kind",
    [
        "root_link",
        "part_link",
        "stale_request",
        "missing_offline",
        "reserved_marker",
        "noncaptured_image",
    ],
)
def test_boundary_input_and_checkpoint_guards_are_enforced(small_limit, tmp_path, path_kind):
    original = body()
    root = tmp_path / "parts"
    if path_kind == "root_link":
        target = tmp_path / "outside"
        target.mkdir()
        root.symlink_to(target, target_is_directory=True)
    elif path_kind == "part_link":
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (root / "0").symlink_to(outside, target_is_directory=True)
    elif path_kind == "stale_request":
        root.mkdir()
        (root / "logical-request.json").write_text("changed")
    elif path_kind == "reserved_marker":
        payload = json.loads(original["input"][0]["content"][0]["text"])
        payload[requests.MARKER] = {"approved": True}
        original["input"][0]["content"][0]["text"] = json.dumps(payload)
    elif path_kind == "noncaptured_image":
        original["input"][0]["content"][2]["image_url"] = "https://my.f5.com/image.png"
    with pytest.raises(ValueError):
        if path_kind == "missing_offline":
            execute_cached_source(tmp_path / "missing.json", original, None)
        else:
            execute_source_request(original, Client(), root)


def test_final_finding_classification_composes_with_boundary_replay(small_limit, tmp_path):
    original = body(8, paired=True)
    payload = json.loads(original["input"][0]["content"][0]["text"])
    context = {key: value for key, value in payload.items() if key != "candidate"}
    context["binding_sha256"] = "b" * 64
    candidate = {**payload["candidate"], "findings": [], "body": context["body"]}
    visual = original["input"][0]["content"][1:]
    output = tmp_path / "review"
    record = output / "documents/source.json"
    record.parent.mkdir(parents=True)
    saved = {"phases": {}, "privacy_feedback": ["first correction", "second correction"]}
    record.write_bytes(json_bytes(saved))
    client = Client()
    client.fail_phase_once = "finalclassification"
    with pytest.raises(TransportFailure):
        reconciliation.review_final_findings(
            saved, record, output, context, candidate, visual, client
        )
    pending = json.loads(record.read_bytes())
    assert pending["privacy_feedback"] == saved["privacy_feedback"] and has_retryable_boundary(
        pending
    )
    reconciliation.review_final_findings(
        pending, record, output, context, candidate, visual, client
    )
    completed = json.loads(record.read_bytes())
    assert not has_retryable_boundary(completed)
    checked = reconciliation.replay_classification(completed, context, candidate, visual)
    assert checked["finding_decisions"] == [] and checked["body"] == candidate["body"]
