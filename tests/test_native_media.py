"""Media review must see native frames and cannot resolve SVG-controlled resources."""

import base64
import io
from pathlib import Path

import pytest
from PIL import Image

from html_to_markdown.enrichment.media import all_frame_inputs, validate_svg
from html_to_markdown.source_review import addressed_spans


def test_review_contains_every_native_animation_frame_and_clock(tmp_path: Path) -> None:
    path = tmp_path / "animation.gif"
    frames = [Image.new("RGB", (40, 30), color) for color in ("red", "blue", "green")]
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=[100, 200, 300], loop=2)
    views = all_frame_inputs(path, perform_ocr=False)
    assert len(views) == 3
    assert [info["duration"] for _, info in views] == [100, 200, 300]
    assert all(info["frame_count"] == 3 and info["loop"] == 2 for _, info in views)
    with Image.open(io.BytesIO(base64.b64decode(views[1][0].split(",", 1)[1]))) as frame:
        assert frame.getpixel((0, 0)) == (0, 0, 255)


def test_native_screenshot_is_not_downsampled_before_ocr_or_validation(tmp_path: Path) -> None:
    path = tmp_path / "screenshot.png"
    Image.new("RGB", (3000, 80), "white").save(path)
    data, info = all_frame_inputs(path, perform_ocr=False)[0]
    assert info["width"] == 3000
    with Image.open(io.BytesIO(base64.b64decode(data.split(",", 1)[1]))) as image:
        assert image.size == (3000, 80)


@pytest.mark.parametrize(
    "markup",
    [
        '<image href="file:///private/auth.json"/>',
        '<image href="https://host.invalid/track"/>',
        "<script>read()</script>",
        "<foreignObject><iframe/></foreignObject>",
        '<rect onload="read()"/>',
        '<style>@import "https://host.invalid/track";</style>',
        '<rect fill="url(file:///private/auth.json)"/>',
    ],
)
def test_svg_resources_cannot_read_files_or_contacts(markup: str) -> None:
    with pytest.raises(ValueError):
        validate_svg(('<svg xmlns="http://www.w3.org/2000/svg">' + markup + "</svg>").encode())
    validate_svg(b'<svg xmlns="http://www.w3.org/2000/svg"><rect fill="url(#gradient)"/></svg>')


def test_unlisted_names_have_exact_controller_supplied_span_guards() -> None:
    text = "User: Person Example\nSecond line\n"
    spans = addressed_spans({"body": text})
    assert any(start == 0 and end == len("User: Person Example") for _, start, end, _ in spans)
    assert any(text[start:end] == "Person" for _, start, end, _ in spans)
