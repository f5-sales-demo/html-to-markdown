"""Preservation entries keep original bytes and confer no privacy/redaction approval."""

import io

import pytest
from PIL import Image

from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.source_review import MediaChange, preview_media


def preservation(raw):
    return MediaChange(
        source_sha256=sha(raw),
        rectangles=[],
        labels=[],
        frames=[],
        strip_metadata=False,
        reason="Preserve the complete independently reviewed source image.",
    )


@pytest.mark.parametrize("format", ["PNG", "JPEG", "GIF"])
def test_noop_media_keeps_exact_format_metadata_animation_and_bytes(format):
    output = io.BytesIO()
    first = Image.new("RGB", (32, 20), "blue")
    if format == "GIF":
        first.save(
            output,
            format=format,
            save_all=True,
            append_images=[Image.new("RGB", first.size, "green")],
            duration=[80, 120],
            loop=3,
            comment=b"documentation example",
        )
    else:
        first.save(output, format=format)
    raw = output.getvalue()
    kept, receipt = preview_media(raw, preservation(raw), {})
    assert kept == raw
    assert receipt == {"output_sha256": sha(raw)}
    assert preview_media(raw, preservation(raw), {}) == (kept, receipt)


def test_noop_still_rejects_wrong_source_hash_missing_reason_and_invalid_frames():
    output = io.BytesIO()
    Image.new("RGB", (32, 20), "blue").save(output, format="PNG")
    raw = output.getvalue()
    change = preservation(raw)
    with pytest.raises(ValueError, match="changed media bytes"):
        preview_media(raw, change.model_copy(update={"source_sha256": "a" * 64}), {})
    with pytest.raises(ValueError, match="rationale"):
        preview_media(raw, change.model_copy(update={"reason": ""}), {})
    invalid = MediaChange.model_validate(
        {**change.model_dump(), "frames": [{"frame": 1, "rectangles": [], "labels": []}]}
    )
    with pytest.raises(ValueError, match="static media"):
        preview_media(raw, invalid, {})
