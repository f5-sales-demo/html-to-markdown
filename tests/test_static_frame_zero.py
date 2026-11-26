"""Static frame-zero edits retain every guard and independently replayed pixel."""

import io
from copy import deepcopy

import pytest
from PIL import Image

from html_to_markdown.community_media import reviewed_media
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.source_review import FrameChange, Label, MediaChange, preview_media


def source_image(format="PNG"):
    image = Image.new("RGB", (96, 64), "navy")
    image.putpixel((90, 60), (0, 255, 0))
    output = io.BytesIO()
    image.save(output, format=format)
    return output.getvalue()


def proposal(raw):
    value = sha("example.invalid")
    label = Label(
        rectangle=[8, 8, 72, 24],
        source_value_sha256=value,
        text="example.test",
        size=8,
    )
    return MediaChange(
        source_sha256=sha(raw),
        rectangles=[[8, 8, 72, 24]],
        labels=[label],
        frames=[],
        strip_metadata=False,
        reason="Replace the independently identified screenshot value consistently.",
    )


def as_frame_zero(change):
    return change.model_copy(
        update={
            "rectangles": [],
            "labels": [],
            "frames": [FrameChange(frame=0, rectangles=change.rectangles, labels=change.labels)],
        },
        deep=True,
    )


@pytest.mark.parametrize("format", ["PNG", "JPEG", "GIF"])
def test_static_frame_zero_matches_top_level_output_and_independent_replay(format):
    raw = source_image(format)
    top = proposal(raw)
    framed = as_frame_zero(top)
    preserved = deepcopy(framed.model_dump())
    replacements = {top.labels[0].source_value_sha256: top.labels[0].text}
    expected, receipt = preview_media(raw, top, replacements)
    output, frame_receipt = preview_media(raw, framed, replacements)
    assert (output, frame_receipt) == (expected, receipt)
    assert framed.model_dump() == preserved
    replayed, _ = reviewed_media(
        raw,
        "captured",
        {"assets": {"captured": sha(raw)}, "asset_redactions": {"captured": frame_receipt}},
    )
    assert replayed == output
    with Image.open(io.BytesIO(output)) as result, Image.open(io.BytesIO(raw)) as source:
        assert result.getpixel((90, 60)) == source.convert("RGB").getpixel((90, 60))
        assert result.size == (96, 64)


@pytest.mark.parametrize("format", ["PNG", "JPEG", "GIF"])
def test_empty_static_frame_zero_preserves_original_bytes(format):
    raw = source_image(format)
    change = MediaChange(
        source_sha256=sha(raw),
        rectangles=[],
        labels=[],
        frames=[FrameChange(frame=0, rectangles=[], labels=[])],
        strip_metadata=False,
        reason="Preserve all independently reviewed original pixels and metadata.",
    )
    assert preview_media(raw, change, {}) == (raw, {"output_sha256": sha(raw)})


@pytest.mark.parametrize("frames", [[1], [-1], [0, 0], [0, 1]])
def test_static_frame_zero_rejects_invalid_or_duplicate_frames(frames):
    raw = source_image()
    change = as_frame_zero(proposal(raw))
    change = change.model_copy(
        update={"frames": [change.frames[0].model_copy(update={"frame": n}) for n in frames]}
    )
    with pytest.raises(ValueError, match="static media"):
        preview_media(raw, change, {change.frames[0].labels[0].source_value_sha256: "example.test"})


@pytest.mark.parametrize("geometry", ["rectangles", "labels"])
def test_static_frame_zero_rejects_mixed_representations(geometry):
    raw = source_image()
    top = proposal(raw)
    change = as_frame_zero(top).model_copy(update={geometry: getattr(top, geometry)})
    with pytest.raises(ValueError, match="static media"):
        preview_media(raw, change, {top.labels[0].source_value_sha256: top.labels[0].text})


@pytest.mark.parametrize("replacements", [{}, {sha("example.invalid"): "different.test"}])
def test_static_frame_zero_requires_exact_text_image_replacements(replacements):
    raw = source_image()
    change = as_frame_zero(proposal(raw))
    with pytest.raises(ValueError, match="replacements disagree"):
        preview_media(raw, change, replacements)


def test_static_frame_zero_rejects_changed_source():
    raw = source_image()
    change = as_frame_zero(proposal(raw))
    changed = change.model_copy(update={"source_sha256": "a" * 64})
    with pytest.raises(ValueError, match="changed media bytes"):
        preview_media(
            raw, changed, {change.frames[0].labels[0].source_value_sha256: "example.test"}
        )
