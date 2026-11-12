"""Context views expose neighboring source pixels without authorizing wider redactions."""

import base64
import io
import json

import pytest
from PIL import Image

from html_to_markdown.media_observation_jobs import observation_request
from html_to_markdown.media_observations import observation_views


def native_frame():
    frame = Image.new("RGB", (200, 60))
    for y in range(frame.height):
        for x in range(frame.width):
            frame.putpixel((x, y), (x, y, (x + y) % 256))
    stream = io.BytesIO()
    frame.save(stream, format="PNG")
    visual = [
        {"type": "input_text", "text": "media/" + "a" * 64 + "/0"},
        {
            "type": "input_image",
            "image_url": "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode(),
        },
    ]
    return frame, visual


def test_context_crop_keeps_candidate_and_inspection_coordinates_and_exact_pixels():
    frame, visual = native_frame()
    region = {"media_sha256": "a" * 64, "frame": 0, "rectangle": [100, 0, 130, 8]}
    old = observation_views([region], visual)
    views = observation_views([region], visual, context_margin=64)
    assert observation_views([region], visual) == old
    description = views[0]["text"]
    details, _ = json.JSONDecoder().raw_decode(description[description.index("{") :])
    assert details["rectangle"] == region["rectangle"]
    assert details["inspection_rectangle"] == [36, 0, 194, 60]
    with Image.open(io.BytesIO(base64.b64decode(views[1]["image_url"].split(",", 1)[1]))) as pixels:
        restored = pixels.resize((158, 60), Image.Resampling.NEAREST)
        assert restored.tobytes() == frame.crop((36, 0, 194, 60)).tobytes()
    assert visual == native_frame()[1]


@pytest.mark.parametrize("rectangle", [[-1, 0, 12, 8], [190, 0, 210, 8], [100, 8, 100, 9]])
def test_context_does_not_make_an_invalid_candidate_region_valid(rectangle):
    _, visual = native_frame()
    with pytest.raises(ValueError, match="outside its native frame"):
        observation_views(
            [{"media_sha256": "a" * 64, "frame": 0, "rectangle": rectangle}],
            visual,
            context_margin=64,
        )


def test_context_contact_sheets_preserve_pixel_mapping_and_full_frame_image_limit():
    frame, pair = native_frame()
    visual = []
    regions = []
    for index in range(45):
        digest = f"{index:064x}"
        visual.extend([{**pair[0], "text": "media/" + digest + "/0"}, pair[1]])
        if index < 20:
            regions.append({"media_sha256": digest, "frame": 0, "rectangle": [100, 0, 130, 8]})
    views = observation_views(regions, visual, context_margin=64)
    assert sum(v["type"] == "input_image" for v in visual + views) == 50
    total = 0
    for description, image in zip(views[::2], views[1::2], strict=True):
        content = description["text"]
        panels, _ = json.JSONDecoder().raw_decode(content[content.index("[") :])
        with Image.open(io.BytesIO(base64.b64decode(image["image_url"].split(",", 1)[1]))) as sheet:
            for panel in panels:
                assert panel["rectangle"] == [100, 0, 130, 8]
                assert panel["inspection_rectangle"] == [36, 0, 194, 60]
                restored = sheet.crop(panel["sheet_rectangle"]).resize(
                    (158, 60), Image.Resampling.NEAREST
                )
                assert restored.tobytes() == frame.crop((36, 0, 194, 60)).tobytes()
                total += 1
    assert total == len(regions)


def test_source_privacy_v2_requests_select_context_without_changing_legacy_views():
    _, visual = native_frame()
    region = {"media_sha256": "a" * 64, "frame": 0, "rectangle": [100, 0, 130, 8]}
    context = {
        "identity": "doc",
        "binding_sha256": "b" * 64,
        "media": {"a" * 64: {"frames": []}},
        "observation_regions": [region],
    }
    legacy = observation_request(context, visual)
    assert legacy["input"][0]["content"][-2:] == observation_views([region], visual)
    current = observation_request(
        {**context, "binding": {"privacy_version": "source-privacy/2"}}, visual
    )
    assert current["input"][0]["content"][-2:] == observation_views(
        [region], visual, context_margin=128
    )
    assert legacy["input"][0]["content"][1:3] == visual
    assert current["input"][0]["content"][1:3] == visual
