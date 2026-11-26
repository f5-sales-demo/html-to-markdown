"""OCR addresses compile into native guards without changing observation text."""

import io
from copy import deepcopy

import pytest
from PIL import Image, ImageDraw

from html_to_markdown.curation import json_bytes
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.source_ocr_edits import compile_text
from html_to_markdown.source_review import PrivacyDecision, apply_text, preview_media

MEDIA = "a" * 64
ORIGINAL = "private-demo-id"
REPLACEMENT = "documentation-example"


def case(frame=0, framed=False):
    field = f"media/{MEDIA}/{frame}/ocr"
    label = {
        "rectangle": [1, 1, 120, 20],
        "source_value_sha256": sha(ORIGINAL),
        "text": REPLACEMENT,
        "size": 12,
    }
    context = {
        "metadata": {"title": "Demo", "source_url": "https://docs.cloud.f5.com/demo"},
        "body": "Run `curl --retry 3` within 30 seconds.",
        "fields": {field: ORIGINAL},
        "media": {
            MEDIA: {
                "frame_count": 2 if frame else 1,
                "frames": [
                    {"text": ORIGINAL, "frame": 0},
                    *([{"text": ORIGINAL, "frame": 1}] if frame else []),
                ],
            }
        },
    }
    proposal = PrivacyDecision.model_validate(
        {
            "identity": "demo",
            "binding_sha256": "b" * 64,
            "findings": [],
            "unresolved": [],
            "values": [{"source_sha256": sha(ORIGINAL), "replacement": REPLACEMENT}],
            "text_changes": [
                {
                    "field": field,
                    "start": 0,
                    "end": len(ORIGINAL),
                    "source_sha256": sha(ORIGINAL),
                    "replacement": REPLACEMENT,
                    "reason": "Replace the same identifying value in source pixels.",
                }
            ],
            "media_changes": [
                {
                    "source_sha256": MEDIA,
                    "rectangles": [],
                    "strip_metadata": False,
                    "reason": "Preserve the technical relationship.",
                    "labels": [] if framed else [label],
                    "frames": [{"frame": frame, "rectangles": [], "labels": [label]}]
                    if framed
                    else [],
                }
            ],
        }
    )
    return context, proposal


@pytest.mark.parametrize(("frame", "framed"), [(0, False), (0, True), (1, True)])
def test_exact_ocr_edit_is_an_assertion_bound_to_the_native_label(frame, framed):
    context, proposal = case(frame, framed)
    before_context, before_proposal = deepcopy(context), proposal.model_dump()
    metadata, body, ledger = compile_text(context, proposal)
    assert metadata == context["metadata"] and body == context["body"]
    assert context == before_context and proposal.model_dump() == before_proposal
    assert ledger["proposal_sha256"] == sha(json_bytes(before_proposal))
    guard = ledger["changes"][0]
    assert guard["field"] == proposal.text_changes[0].field
    assert guard["source_sha256"] == sha(ORIGINAL)
    assert guard["ocr_sha256"] == sha(ORIGINAL)
    assert guard["replacement_sha256"] == sha(REPLACEMENT)
    assert guard["frame"] == frame and guard["media_sha256"] == MEDIA
    assert guard["labels"][0]["rectangle"] == [1, 1, 120, 20]
    assert ORIGINAL not in json_bytes(ledger).decode()
    assert "approved" not in ledger


@pytest.mark.parametrize(
    "field",
    [
        f"media/{MEDIA}/00/ocr",
        f"media/{MEDIA}/-1/ocr",
        f"media/{MEDIA}/0/metadata",
        f"media/{MEDIA}/0/ocr/extra",
        f"media/{MEDIA.upper()}/0/ocr",
        "media/../0/ocr",
        f"media/{'c' * 64}/0/ocr",
        f"media/{MEDIA}/2/ocr",
    ],
)
def test_noncanonical_or_unknown_native_address_is_rejected(field):
    context, proposal = case()
    proposal.text_changes[0].field = field
    context["fields"][field] = ORIGINAL
    with pytest.raises(ValueError, match="OCR"):
        compile_text(context, proposal)


@pytest.mark.parametrize("change", ["digest", "start", "end", "mapping", "reason"])
def test_exact_ocr_span_and_relationship_guards_cannot_be_bypassed(change):
    context, proposal = case()
    edit = proposal.text_changes[0]
    if change == "digest":
        edit.source_sha256 = "c" * 64
    elif change == "start":
        edit.start = -1
    elif change == "end":
        edit.end += 1
    elif change == "mapping":
        edit.replacement = "disagrees"
    else:
        edit.reason = " "
    with pytest.raises(ValueError, match="OCR"):
        compile_text(context, proposal)


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "value",
        "text",
        "media",
        "frame",
        "duplicate_media",
        "duplicate_frame",
        "mixed",
        "receipt_text",
        "receipt_frame",
        "receipt_count",
    ],
)
def test_ocr_instruction_requires_the_same_native_source_frame_and_mapped_label(change):
    context, proposal = case(0, True)
    media = proposal.media_changes[0]
    if change == "missing":
        media.frames[0].labels = []
    elif change == "value":
        media.frames[0].labels[0].source_value_sha256 = "c" * 64
    elif change == "text":
        media.frames[0].labels[0].text = "disagrees"
    elif change == "media":
        media.source_sha256 = "c" * 64
    elif change == "frame":
        media.frames[0].frame = 1
    elif change == "duplicate_media":
        proposal.media_changes.append(media.model_copy(deep=True))
    elif change == "duplicate_frame":
        media.frames.append(media.frames[0].model_copy(deep=True))
    elif change == "mixed":
        media.labels = media.frames[0].labels
    elif change == "receipt_text":
        context["media"][MEDIA]["frames"][0]["text"] = "changed"
    elif change == "receipt_frame":
        context["media"][MEDIA]["frames"][0]["frame"] = 1
    else:
        context["media"][MEDIA]["frame_count"] = 3
    with pytest.raises(ValueError, match="OCR"):
        compile_text(context, proposal)


def test_overlapping_ocr_spans_and_duplicate_mappings_are_rejected():
    context, proposal = case()
    proposal.text_changes.append(proposal.text_changes[0].model_copy(deep=True))
    with pytest.raises(ValueError, match="overlapping OCR"):
        compile_text(context, proposal)
    proposal.text_changes.pop()
    proposal.values.append(proposal.values[0].model_copy())
    with pytest.raises(ValueError, match="duplicate synthetic"):
        compile_text(context, proposal)


def test_ordinary_text_contract_and_protected_metadata_are_unchanged():
    context, proposal = case()
    proposal.text_changes = []
    assert compile_text(context, proposal) == (
        *apply_text(context["metadata"], context["body"], proposal),
        None,
    )
    with pytest.raises(ValueError, match="identity or dates"):
        apply_text(context["metadata"], context["body"], case()[1])
    proposal.text_changes = [
        case()[1].text_changes[0].model_copy(update={"field": "metadata/source_url"})
    ]
    with pytest.raises(ValueError, match="identity or dates"):
        compile_text(context, proposal)


def test_mixed_ocr_and_body_edits_preserve_command_limits_and_original_instruction():
    context, proposal = case()
    context["body"] = ORIGINAL + ": " + context["body"]
    proposal.text_changes.append(proposal.text_changes[0].model_copy(update={"field": "body"}))
    original = proposal.model_dump()
    metadata, body, ledger = compile_text(context, proposal)
    assert body == REPLACEMENT + ": Run `curl --retry 3` within 30 seconds."
    assert metadata == context["metadata"]
    assert len(ledger["changes"]) == 1 and proposal.model_dump() == original
    ledger["changes"][0]["labels"][0]["rectangle"][0] = 999
    assert proposal.media_changes[0].labels[0].rectangle == [1, 1, 120, 20]


@pytest.mark.parametrize("mixed", [False, True])
def test_animation_global_labels_apply_to_every_frame_with_optional_specific_edits(mixed):
    context, proposal = case(1, True)
    media = proposal.media_changes[0]
    media.labels = [media.frames[0].labels[0].model_copy(deep=True)]
    if not mixed:
        media.frames = []
    before = proposal.model_dump()
    _, _, ledger = compile_text(context, proposal)
    guard = ledger["changes"][0]
    assert guard["frame"] == 1
    assert len(guard["labels"]) == (2 if mixed else 1)
    assert proposal.model_dump() == before


@pytest.mark.parametrize("mixed", [False, True])
def test_ocr_animation_guards_match_actual_pixel_and_playback_replay(mixed):
    frames = [Image.new("RGB", (240, 40), color) for color in ("white", "lightgray")]
    for image in frames:
        ImageDraw.Draw(image).text((2, 2), ORIGINAL, fill="black")
    stream = io.BytesIO()
    frames[0].save(
        stream,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=[100, 200],
        loop=2,
        optimize=False,
    )
    raw = stream.getvalue()
    digest = sha(raw)
    context, proposal = case(1, True)
    old_field = proposal.text_changes[0].field
    field = f"media/{digest}/1/ocr"
    context["fields"] = {field: context["fields"][old_field]}
    context["media"] = {digest: context["media"][MEDIA]}
    proposal.text_changes[0].field = field
    media = proposal.media_changes[0]
    media.source_sha256 = digest
    media.frames[0].labels[0].rectangle = [1, 1, 200, 30]
    media.labels = [media.frames[0].labels[0].model_copy(deep=True)]
    if not mixed:
        media.frames = []
    original = proposal.model_dump()
    _, _, ledger = compile_text(context, proposal)
    assert ledger["changes"][0]["media_sha256"] == digest
    output, receipt = preview_media(raw, media, {sha(ORIGINAL): REPLACEMENT})
    assert preview_media(raw, media, {sha(ORIGINAL): REPLACEMENT}) == (output, receipt)
    with Image.open(io.BytesIO(output)) as rendered:
        assert rendered.n_frames == 2 and rendered.info["loop"] == 2
        durations = []
        for index in range(rendered.n_frames):
            rendered.seek(index)
            durations.append(rendered.info["duration"])
        assert durations == [100, 200]
    assert proposal.model_dump() == original
