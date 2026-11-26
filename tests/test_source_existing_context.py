"""Original source proposals remain immutable data pending current independent validation."""

import base64
import copy
import io
import json

import pytest
from PIL import Image

from html_to_markdown import source_existing_context as inputs
from html_to_markdown.curation import json_bytes
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.enrichment.contracts import ResponseEvidence
from html_to_markdown.enrichment.gates import request_hash
from html_to_markdown.source_existing_context import qualify_source_proposal
from html_to_markdown.source_review import PrivacyDecision, admission_request
from html_to_markdown.source_review_jobs import _compile, _context, prepare_reviews


def evidence(context):
    body = admission_request("gpt-6.1-sol", "privacy", context, PrivacyDecision)
    value = {
        "identity": context["identity"],
        "binding_sha256": context["binding_sha256"],
        "findings": [],
        "values": [],
        "text_changes": [],
        "media_changes": [],
        "unresolved": [],
    }
    response = {
        "status": "completed",
        "model": "gpt-6.1-sol",
        "output": [{"content": [{"type": "output_text", "text": json.dumps(value)}]}],
    }
    return ResponseEvidence(
        request_sha256=request_hash(body),
        response_sha256=sha(json.dumps(response, sort_keys=True, separators=(",", ":"))),
        model="gpt-6.1-sol",
        reasoning="high",
        prompt_version="source-admission/1",
        request=body,
        response=response,
        error=None,
        transport="codex_litellm",
        runtime_version="test",
    ).model_dump()


def prepared(capture, tmp_path, media=False):
    if media:
        inventory = json.loads((capture / "inventory.json").read_bytes())
        item = inventory["documents"][0]
        path = capture / "articles/content/my-f5-com/K000123456/assets/sample.gif"
        path.parent.mkdir(parents=True, exist_ok=True)
        frames = [Image.new("RGB", (16, 16), color) for color in ("blue", "green", "red")]
        frames[0].save(
            path, save_all=True, append_images=frames[1:], duration=[80, 120, 160], loop=2
        )
        item["media"] = [
            {
                "path": path.relative_to(capture).as_posix(),
                "sha256": sha(path.read_bytes()),
                "media_type": "image/gif",
                "url": "https://my.f5.com/sample.gif",
            }
        ]
        (capture / "inventory.json").write_bytes(json_bytes(inventory))
    output = tmp_path / "review"
    prepare_reviews(capture, output)
    current = next((output / "documents").glob("*.json"))
    saved = json.loads(current.read_bytes())
    context, visual = _context(capture, output, saved)
    old = copy.deepcopy(saved)
    old["binding"]["validator_model"] = "gpt-6-astra"
    old["binding"].pop("model_policy_version", None)
    old["binding_sha256"] = sha(json_bytes(old["binding"]))
    context.update(binding=old["binding"], binding_sha256=old["binding_sha256"])
    context["validation_feedback"] = [
        "original failed frame compiler",
        "original failed OCR compiler",
    ]
    body_evidence = evidence(context)
    body_evidence["request"]["input"][0]["content"].extend(visual)
    body_evidence["request_sha256"] = request_hash(body_evidence["request"])
    old["phases"] = {"privacy": body_evidence}
    old["privacy_feedback"] = list(context["validation_feedback"])
    old["privacy_exhausted"] = True
    original = tmp_path / "original.json"
    original.write_bytes(json_bytes(old))
    return current, original, old, context


def call(capture, current, original):
    return qualify_source_proposal(
        capture, current, sha(current.read_bytes()), original, sha(original.read_bytes())
    )


def alter_original(original, change):
    value = json.loads(original.read_bytes())
    change(value)
    original.write_bytes(json_bytes(value))


@pytest.mark.parametrize("media", [False, True])
def test_original_legacy_binding_is_kept_and_no_source_or_ledger_is_rebound(
    capture, tmp_path, media
):
    current, original, old, context = prepared(capture, tmp_path, media)
    source_bytes = current.read_bytes(), original.read_bytes()
    proposal = PrivacyDecision.model_validate_json(
        old["phases"]["privacy"]["response"]["output"][0]["content"][0]["text"]
    )
    with pytest.raises(ValueError, match="stale"):
        _compile(capture, tmp_path / "compiler", old, context, proposal)
    qualified = call(capture, current, original)
    assert qualified["original_context"]["binding_sha256"] == old["binding_sha256"]
    assert qualified["original_context"]["binding"] == old["binding"]
    assert qualified["source_binding_sha256"] != old["binding_sha256"]
    assert qualified["original_feedback"] == old["privacy_feedback"]
    assert qualified["editor_corrections_consumed"] == 2
    assert qualified["new_editor_allowances"] == 0
    assert qualified["admission_approved"] is False
    assert (current.read_bytes(), original.read_bytes()) == source_bytes
    assert call(capture, current, original) == qualified
    if media:
        assert len(qualified["original_context"]["media"].popitem()[1]["frames"]) == 3


@pytest.mark.parametrize("which", ["current", "original"])
def test_digest_pins_are_required_before_source_or_response_consumption(capture, tmp_path, which):
    current, original, _, _ = prepared(capture, tmp_path)
    pins = [sha(current.read_bytes()), sha(original.read_bytes())]
    pins[0 if which == "current" else 1] = "0" * 64
    with pytest.raises(ValueError, match="record digest"):
        qualify_source_proposal(capture, current, pins[0], original, pins[1])


def test_changed_capture_is_rejected(capture, tmp_path):
    current, original, saved, _ = prepared(capture, tmp_path)
    (capture / saved["item"]["article_path"]).write_text("changed source")
    with pytest.raises(ValueError, match="digest"):
        call(capture, current, original)


@pytest.mark.parametrize("field", ["body", "metadata", "source_article_html", "binding_sha256"])
def test_self_consistent_original_request_cannot_assert_different_source(capture, tmp_path, field):
    current, original, _, _ = prepared(capture, tmp_path)

    def change(value):
        ev = value["phases"]["privacy"]
        ctx = json.loads(ev["request"]["input"][0]["content"][0]["text"])
        if field == "metadata":
            ctx[field]["title"] = "Invented title"
        else:
            ctx[field] = "different source"
        ev["request"]["input"][0]["content"][0]["text"] = json.dumps(ctx)
        ev["request_sha256"] = request_hash(ev["request"])

    alter_original(original, change)
    with pytest.raises(ValueError, match="original source"):
        call(capture, current, original)


@pytest.mark.parametrize("kind", ["refusal", "unresolved", "request_hash"])
def test_failed_or_incomplete_original_is_unavailable_for_selection(capture, tmp_path, kind):
    current, original, _, _ = prepared(capture, tmp_path)

    def change(value):
        ev = value["phases"]["privacy"]
        if kind == "request_hash":
            ev["request_sha256"] = "0" * 64
        elif kind == "refusal":
            ev["response"]["status"] = "incomplete"
            ev["response_sha256"] = sha(
                json.dumps(ev["response"], sort_keys=True, separators=(",", ":"))
            )
        else:
            result = json.loads(ev["response"]["output"][0]["content"][0]["text"])
            result["unresolved"] = ["unverified identifying pixels"]
            ev["response"]["output"][0]["content"][0]["text"] = json.dumps(result)
            ev["response_sha256"] = sha(
                json.dumps(ev["response"], sort_keys=True, separators=(",", ":"))
            )

    alter_original(original, change)
    with pytest.raises(ValueError):
        call(capture, current, original)


def test_missing_animation_frame_is_rejected_without_regenerating_ocr(capture, tmp_path):
    current, original, _, _ = prepared(capture, tmp_path, True)

    def change(value):
        ev = value["phases"]["privacy"]
        ctx = json.loads(ev["request"]["input"][0]["content"][0]["text"])
        next(iter(ctx["media"].values()))["frames"].pop()
        ev["request"]["input"][0]["content"][0]["text"] = json.dumps(ctx)
        ev["request_sha256"] = request_hash(ev["request"])

    alter_original(original, change)
    with pytest.raises(ValueError, match="native frame coverage"):
        call(capture, current, original)


def test_original_record_symlink_is_rejected(capture, tmp_path):
    current, original, _, _ = prepared(capture, tmp_path)
    link = tmp_path / "original-link.json"
    link.symlink_to(original)
    with pytest.raises(ValueError, match="unsafe"):
        call(capture, current, link)


@pytest.mark.parametrize("kind", ["pixels", "timing"])
def test_changed_original_native_representation_cannot_match_the_source(capture, tmp_path, kind):
    current, original, _, _ = prepared(capture, tmp_path, True)

    def change(value):
        ev = value["phases"]["privacy"]
        ctx = json.loads(ev["request"]["input"][0]["content"][0]["text"])
        frame = next(iter(ctx["media"].values()))["frames"][0]
        if kind == "timing":
            frame["duration"] = 999
        else:
            image = next(
                c for c in ev["request"]["input"][0]["content"] if c["type"] == "input_image"
            )
            raw = io.BytesIO()
            Image.new("RGB", (16, 16), "yellow").save(raw, format="PNG")
            image["image_url"] = (
                "data:image/png;base64," + base64.b64encode(raw.getvalue()).decode()
            )
            frame["visual_input_sha256"] = sha(raw.getvalue())
        ev["request"]["input"][0]["content"][0]["text"] = json.dumps(ctx)
        ev["request_sha256"] = request_hash(ev["request"])

    alter_original(original, change)
    with pytest.raises(ValueError):
        call(capture, current, original)


def test_retained_ocr_is_checked_without_reexecution(capture, tmp_path, monkeypatch):
    current, original, _, _ = prepared(capture, tmp_path, True)
    native = inputs.all_frame_inputs
    calls = []

    def inspect(path, *, perform_ocr):
        calls.append(perform_ocr)
        assert perform_ocr is False
        return native(path, perform_ocr=perform_ocr)

    monkeypatch.setattr(inputs, "all_frame_inputs", inspect)
    call(capture, current, original)
    assert calls == [False]
