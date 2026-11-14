"""Retirement masks protect the rest of a necessary source image and its references."""

import io
from copy import deepcopy

import pytest
from PIL import Image

from html_to_markdown.curation_media import (
    prepare_mask,
    reference_updates,
    relocate_references,
    replay_mask,
    verify_mask,
)
from html_to_markdown.enrichment.analysis import sha


def picture():
    image = Image.new("RGB", (30, 20), (90, 120, 160))
    image.putpixel((20, 15), (1, 2, 3))
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def masks():
    return [
        {
            "frame": 0,
            "rectangle": [2, 2, 10, 5],
            "color": [246, 247, 249],
            "reason": "Remove the reviewed retired capability banner; retain dashboard controls.",
        }
    ]


def test_mask_preserves_dimensions_mode_and_every_unaffected_pixel():
    raw = picture()
    output, specification = prepare_mask(raw, masks())
    verify_mask(raw, output, specification)
    assert replay_mask(raw, specification) == output
    with Image.open(io.BytesIO(raw)) as original, Image.open(io.BytesIO(output)) as candidate:
        assert original.size == candidate.size and original.mode == candidate.mode
        assert original.getpixel((20, 15)) == candidate.getpixel((20, 15))
        assert candidate.getpixel((2, 2)) == (246, 247, 249)


@pytest.mark.parametrize("fault", ["source", "output", "pixels", "outside", "fill"])
def test_changed_source_output_or_pixel_guards_are_rejected(fault):
    raw = picture()
    output, spec = prepare_mask(raw, masks())
    spec = deepcopy(spec)
    if fault == "source":
        spec["source_sha256"] = "0" * 64
    elif fault == "output":
        spec["output_sha256"] = "0" * 64
    elif fault == "pixels":
        spec["masks"][0]["source_pixels_sha256"] = "0" * 64
    else:
        with Image.open(io.BytesIO(output)) as image:
            image.putpixel((20, 15) if fault == "outside" else (2, 2), (5, 6, 7))
            out = io.BytesIO()
            image.save(out, format="PNG")
            output = out.getvalue()
            spec["output_sha256"] = sha(output)
    with pytest.raises(ValueError):
        verify_mask(raw, output, spec)


@pytest.mark.parametrize(
    "boxes", [[[2, 2, 10, 5], [9, 2, 11, 6]], [[-1, 2, 10, 5]], [[2, 2, 31, 5]], [[True, 2, 10, 5]]]
)
def test_invalid_or_overlapping_regions_do_not_change_the_source(boxes):
    raw = picture()
    before = sha(raw)
    choices = [{**masks()[0], "rectangle": box} for box in boxes]
    with pytest.raises(ValueError):
        prepare_mask(raw, choices)
    assert sha(raw) == before


def test_animated_media_is_never_silently_flattened():
    out = io.BytesIO()
    a = Image.new("RGB", (2, 2), "red")
    b = Image.new("RGB", (2, 2), "blue")
    a.save(out, format="GIF", save_all=True, append_images=[b], duration=[20, 40], loop=3)
    raw = out.getvalue()
    with pytest.raises(ValueError, match="frame-preserving"):
        prepare_mask(raw, masks())
    with Image.open(io.BytesIO(raw)) as source:
        assert source.n_frames == 2 and source.info["loop"] == 3


def test_asset_reference_updates_preserve_caption_and_literal_code_payloads():
    old = "a" * 64
    new = "b" * 64
    body = f"# Dashboard\n\n![Provisioned dashboard](assets/{old}.png)\n\n```md\n![literal](assets/{old}.png)\n```\n"
    rules = reference_updates(body, old, new)
    assert rules == [
        {"source": f"assets/{old}.png", "destination": f"assets/{new}.png", "count": 1}
    ]
    changed = relocate_references(body, rules)
    assert f"![Provisioned dashboard](assets/{new}.png)" in changed
    assert f"![literal](assets/{old}.png)" in changed
    with pytest.raises(ValueError):
        relocate_references(changed, rules)
