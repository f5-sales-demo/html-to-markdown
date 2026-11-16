"""Encoding equivalence never substitutes for complete source or placement review."""

import base64
import io
from copy import deepcopy

import pytest
from PIL import Image, PngImagePlugin
from test_native_visuals import body

from html_to_markdown.enrichment import native_visuals
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.enrichment.native_pixel_views import image_identity, metadata_value
from html_to_markdown.enrichment.native_visuals import (
    NativeVisualCapacityRequired,
    native_visual_view,
    validate_visual_manifest,
)
from html_to_markdown.enrichment.visual_capacity import decoded_image_bytes


def encoded(picture, compression=6, **options):
    stream = io.BytesIO()
    picture.save(stream, format="PNG", compress_level=compression, **options)
    return {
        "type": "input_image",
        "image_url": "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode(),
    }


def equivalent_pair():
    picture = Image.new("RGBA", (4, 4), (10, 20, 30, 255))
    return encoded(picture, 0), encoded(picture, 9)


def test_encoding_only_changes_share_native_pixels_and_reconstruct_every_placement(monkeypatch):
    original, candidate = equivalent_pair()
    assert original != candidate
    original["detail"] = "high"
    candidate["detail"] = "auto"
    request = body([original])
    request["input"].append(
        {"role": "user", "content": [{"type": "input_text", "text": "candidate"}, candidate]}
    )
    before = deepcopy(request)
    monkeypatch.setattr(native_visuals, "MAX_DECODED_BYTES", 64)
    attachments, manifest = native_visual_view(request)
    assert manifest["schema_version"] == 2 and len(attachments) == 1
    assert decoded_image_bytes(attachments) == 64
    assert [(r["item_index"], r["content_index"]) for r in manifest["references"]] == [
        (0, 1),
        (1, 1),
    ]
    assert [r["original_image_url_sha256"] for r in manifest["references"]] == [
        sha(original["image_url"]),
        sha(candidate["image_url"]),
    ]
    assert (
        manifest["references"][0]["image_bytes_sha256"]
        != manifest["references"][1]["image_bytes_sha256"]
    )
    assert (
        manifest["references"][0]["content_sha256"] != manifest["references"][1]["content_sha256"]
    )
    assert validate_visual_manifest(request, manifest) == [original, candidate]
    assert request == before


def test_more_than_50_encodings_share_without_losing_placement_context():
    first, second = equivalent_pair()
    request = body([first if i % 2 else second for i in range(51)])
    # Byte dedup suffices here; historical v1 behavior must remain unchanged.
    attachments, manifest = native_visual_view(request)
    assert len(attachments) == 2 and manifest["schema_version"] == 1
    assert len(validate_visual_manifest(request, manifest)) == 51


@pytest.mark.parametrize(
    "difference", ["pixel", "alpha", "metadata", "palette", "mode", "native_depth", "size"]
)
def test_changed_source_pixels_or_rendering_metadata_never_share(difference, monkeypatch):
    picture = Image.new("RGBA", (4, 4), (10, 20, 30, 255))
    changed = picture.copy()
    options = {}
    if difference == "pixel":
        changed.putpixel((0, 0), (11, 20, 30, 255))
    elif difference == "alpha":
        changed.putpixel((0, 0), (10, 20, 30, 254))
    elif difference == "metadata":
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text("context", "identifying screenshot context")
        options["pnginfo"] = metadata
    elif difference == "palette":
        picture = Image.new("P", (4, 4), 0)
        picture.putpalette([10, 20, 30] + [0] * 765)
        changed = picture.copy()
        palette = picture.getpalette()
        palette[3] = 1  # An unused entry still belongs to the complete original palette.
        changed.putpalette(palette)
    elif difference == "mode":
        changed = picture.convert("RGB")
    elif difference == "native_depth":
        picture = Image.new("I;16", (4, 4), 256)
        changed = Image.new("I;16", (4, 4), 257)
        assert picture.convert("RGBA").tobytes() == changed.convert("RGBA").tobytes()
    else:
        changed = Image.new("RGBA", (8, 2), (10, 20, 30, 255))
    request = body([encoded(picture, 0), encoded(changed, 9, **options)])
    before = deepcopy(request)
    monkeypatch.setattr(native_visuals, "MAX_DECODED_BYTES", 64)
    with pytest.raises(NativeVisualCapacityRequired, match="decoded image capacity"):
        native_visual_view(request)
    assert request == before


@pytest.mark.parametrize(
    "fault",
    ["missing", "reference", "encoded", "pixels", "native", "rendering", "placement", "source"],
)
def test_v2_manifest_rejects_tampered_evidence(fault, monkeypatch):
    request = body(list(equivalent_pair()))
    monkeypatch.setattr(native_visuals, "MAX_DECODED_BYTES", 64)
    _, manifest = native_visual_view(request)
    if fault == "missing":
        manifest["references"].pop()
    elif fault == "reference":
        manifest["references"][1]["attachment_index"] = 1
    elif fault == "placement":
        manifest["references"][1]["content_index"] = 1
    elif fault == "source":
        request["input"][0]["content"][0]["text"] = "changed source"
    else:
        key = {
            "encoded": "image_bytes_sha256",
            "pixels": "pixels_sha256",
            "native": "native_pixels_sha256",
            "rendering": "rendering_sha256",
        }[fault]
        manifest["references"][1][key] = "0" * 64
    with pytest.raises(ValueError, match="guard changed"):
        validate_visual_manifest(request, manifest)


@pytest.mark.parametrize(
    "left,right", [(b"a", {"type": "bytes", "value": "61"}), ((1, 2), [1, 2]), (True, 1), (1, 1.0)]
)
def test_distinct_metadata_types_cannot_alias(left, right):
    assert metadata_value(left) != metadata_value(right)


def test_unsupported_metadata_is_not_silently_omitted():
    with pytest.raises(ValueError, match="metadata"):
        metadata_value(object())
    with pytest.raises(ValueError, match="metadata"):
        metadata_value(float("nan"))


def test_animated_input_requires_all_complete_frames():
    first = Image.new("RGBA", (4, 4), "red")
    second = Image.new("RGBA", (4, 4), "blue")
    visual = encoded(first, save_all=True, append_images=[second], duration=100, loop=0)
    with pytest.raises(ValueError, match="separate complete animation frames"):
        image_identity(visual)
