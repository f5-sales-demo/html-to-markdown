"""Reviewed replacements preserve visual link roles without widening privacy regions."""

import io

import pytest
from PIL import Image, ImageDraw, ImageFont

from html_to_markdown.community_media import apply_labels_and_rectangles, transform_animation
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.source_review import (
    Label,
    LabelStyle,
    MediaChange,
    PrivacyDecision,
    preview_media,
)

STYLE = {"foreground": [0, 96, 192], "background": [245, 245, 245], "underline": True}


@pytest.mark.parametrize("model", [Label, PrivacyDecision])
def test_privacy_schemas_meet_the_responses_required_property_contract(model) -> None:
    def verify(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert set(node.get("required", [])) == set(node.get("properties", {}))
                assert node.get("additionalProperties") is False
            for child in node.values():
                verify(child)
        elif isinstance(node, list):
            for child in node:
                verify(child)

    schema = model.model_json_schema()
    verify(schema)
    label = schema if model is Label else schema["$defs"]["Label"]
    assert any(option.get("type") == "null" for option in label["properties"]["style"]["anyOf"])


def test_reviewed_link_label_preserves_region_color_underline_and_exact_replay() -> None:
    original = Image.new("RGB", (180, 50), (245, 245, 245))
    ImageDraw.Draw(original).text((6, 6), "private.example", fill=(0, 96, 192))
    ImageDraw.Draw(original).line((170, 0, 170, 49), fill="red")
    stream = io.BytesIO()
    original.save(stream, format="PNG")
    raw = stream.getvalue()
    label = Label(
        rectangle=[4, 4, 160, 32],
        source_value_sha256=sha("private.example"),
        text="example.invalid",
        size=14,
        style=LabelStyle.model_validate(STYLE),
    )
    change = MediaChange(
        source_sha256=sha(raw),
        rectangles=[],
        labels=[label],
        frames=[],
        strip_metadata=True,
        reason="Replace identifying hostname and retain its visible link role",
    )
    result, transform = preview_media(raw, change, {label.source_value_sha256: label.text})
    repeated, _ = preview_media(raw, change, {label.source_value_sha256: label.text})
    assert result == repeated and transform["labels"][0]["style"] == STYLE
    with Image.open(io.BytesIO(result)) as output:
        for y in range(original.height):
            for x in range(original.width):
                if not (4 <= x < 160 and 4 <= y < 32):
                    assert output.getpixel((x, y)) == original.getpixel((x, y))
        font = ImageFont.load_default(size=14)
        box = ImageDraw.Draw(output).textbbox((0, 0), label.text, font=font)
        width, height = box[2] - box[0], box[3] - box[1]
        assert all(
            output.getpixel((x, 4 + 2 + height + 1)) == (0, 96, 192) for x in range(6, 6 + width)
        )
        assert output.getpixel((4, 4)) == (245, 245, 245)


def test_unstyled_labels_replay_with_the_historical_serialization_and_pixels() -> None:
    old = {
        "rectangle": [1, 1, 99, 29],
        "source_value_sha256": "a" * 64,
        "text": "example",
        "size": 12,
    }
    label = Label.model_validate(old)
    assert label.model_dump() == old
    first = Image.new("RGB", (100, 30), "red")
    second = first.copy()
    render = {key: value for key, value in old.items() if key != "source_value_sha256"}
    apply_labels_and_rectangles(first, {"labels": [render]})
    apply_labels_and_rectangles(second, {"labels": [{**render, "style": None}]})
    assert first.tobytes() == second.tobytes()


@pytest.mark.parametrize("color", [[-1, 0, 0], [256, 0, 0], [True, 0, 0], [1, 2], "blue"])
def test_invalid_color_is_rejected_by_schema_and_renderer(color: object) -> None:
    style = {**STYLE, "foreground": color}
    with pytest.raises(ValueError):
        LabelStyle.model_validate(style)
    with pytest.raises(ValueError, match="color"):
        apply_labels_and_rectangles(
            Image.new("RGB", (100, 30)),
            {
                "labels": [
                    {"rectangle": [1, 1, 99, 29], "text": "example", "size": 12, "style": style}
                ]
            },
        )


@pytest.mark.parametrize(
    "style",
    [
        {**STYLE, "underline": 1},
        {**STYLE, "extra": True},
        {**STYLE, "foreground": STYLE["background"]},
    ],
)
def test_invalid_or_invisible_style_fails_closed(style: dict) -> None:
    with pytest.raises(ValueError):
        apply_labels_and_rectangles(
            Image.new("RGB", (100, 30)),
            {
                "labels": [
                    {"rectangle": [1, 1, 99, 29], "text": "example", "size": 12, "style": style}
                ]
            },
        )


def test_styled_animation_keeps_other_frame_pixels_duration_and_loop() -> None:
    frames = [Image.new("RGB", (100, 30), "white"), Image.new("RGB", (100, 30), "red")]
    stream = io.BytesIO()
    frames[0].save(
        stream,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=[40, 90],
        loop=2,
        optimize=False,
    )
    result = transform_animation(
        stream.getvalue(),
        {
            "animation": True,
            "animation_format": "gif",
            "rectangles": [],
            "labels": [],
            "frame_transforms": {
                "0": {
                    "labels": [
                        {
                            "rectangle": [1, 1, 99, 29],
                            "text": "example",
                            "size": 12,
                            "fit": True,
                            "style": {
                                "foreground": [0, 0, 255],
                                "background": [255, 255, 255],
                                "underline": True,
                            },
                        }
                    ]
                }
            },
        },
    )
    with Image.open(io.BytesIO(result)) as output:
        assert output.n_frames == 2 and output.info["loop"] == 2
        assert output.info["duration"] == 40
        assert (0, 0, 255) in set(output.convert("RGB").get_flattened_data())
        output.seek(1)
        assert output.info["duration"] == 90
        assert output.convert("RGB").tobytes() == frames[1].tobytes()
