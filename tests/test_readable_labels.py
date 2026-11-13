"""Long identifying replacements retain readable native glyphs and exact guards."""

from copy import deepcopy
from hashlib import sha256

import pytest
from PIL import Image, ImageDraw

from html_to_markdown.community_media import draw_label


class RecordingDraw:
    def __init__(self, image):
        self.draw = ImageDraw.Draw(image)
        self.lines = []
        self.underlines = []

    def textbbox(self, *args, **kwargs):
        return self.draw.textbbox(*args, **kwargs)

    def text(self, xy, text, **kwargs):
        self.lines.append((text, kwargs["font"].size, xy))
        self.draw.text(xy, text, **kwargs)

    def line(self, xy, **kwargs):
        self.underlines.append(xy)
        self.draw.line(xy, **kwargs)


@pytest.mark.parametrize(
    "text,rectangle",
    [
        ("demo-kvm-app-forward-proxy-policy1", [372, 607, 501, 662]),
        ("edge-k8s-eu.demo-stage.example.com", [1257, 733, 1360, 791]),
        ('"demo-namespace.example"', [30, 20, 110, 70]),
    ],
)
def test_actual_rejected_shapes_wrap_at_requested_size_with_exact_characters(text, rectangle):
    source = Image.new("RGB", (1500, 900), "white")
    output = source.copy()
    recording = RecordingDraw(output)
    label = {
        "text": text,
        "rectangle": rectangle,
        "size": 12,
        "fit": True,
        "style": {"foreground": [21, 36, 82], "background": [255, 255, 255], "underline": False},
    }
    original = deepcopy(label)
    draw_label(recording, {**label, "layout": "wrapped/2"})
    assert label == original
    assert len(recording.lines) > 1
    assert "".join(line[0] for line in recording.lines) == text
    assert all(line[1] == 12 for line in recording.lines)
    left, top, right, bottom = rectangle
    for outside in (
        (0, 0, 1500, top),
        (0, bottom, 1500, 900),
        (0, top, left, bottom),
        (right, top, 1500, bottom),
    ):
        assert output.crop(outside).tobytes() == source.crop(outside).tobytes()
    assert output.crop(rectangle).tobytes() != source.crop(rectangle).tobytes()


def test_wrapped_links_keep_source_color_and_each_line_underline():
    output = Image.new("RGB", (150, 70), "white")
    draw = RecordingDraw(output)
    label = {
        "text": "example-long-policy.example.com",
        "size": 14,
        "rectangle": [5, 5, 130, 60],
        "fit": True,
        "style": {"foreground": [0, 96, 192], "background": [255, 255, 255], "underline": True},
    }
    draw_label(draw, {**label, "layout": "wrapped/2"})
    assert len(draw.lines) > 1 and len(draw.underlines) == len(draw.lines)
    assert "".join(line[0] for line in draw.lines) == label["text"]
    for left, top, right, bottom in draw.underlines:
        assert 5 <= left <= right < 130 and 5 <= top <= bottom < 60
        assert output.getpixel((left, top)) == (0, 96, 192)


def test_impossible_region_fails_instead_of_microscopic_or_outside_glyphs():
    """A new layout cannot expand the guarded region."""
    image = Image.new("RGB", (20, 20), "white")
    before = image.tobytes()
    with pytest.raises(ValueError, match="does not fit"):
        draw_label(
            ImageDraw.Draw(image),
            {"text": "example", "size": 12, "rectangle": [1, 1, 3, 3], "fit": True},
        )
    assert image.tobytes() == before


def test_unversioned_long_label_keeps_the_historical_pixel_receipt():
    image = Image.new("RGB", (150, 70), "white")
    label = {
        "text": "demo-kvm-app-forward-proxy-policy1",
        "size": 12,
        "rectangle": [5, 5, 134, 60],
        "fit": True,
        "style": {"foreground": [21, 36, 82], "background": [255, 255, 255], "underline": False},
    }
    draw_label(ImageDraw.Draw(image), label)
    # Captured from the immutable b66b3b6 renderer, not computed by this implementation.
    assert (
        sha256(image.tobytes()).hexdigest()
        == "b3296dee01789a905ede1bfcbf20cec13c7ebe5246d839e9908c53dfae3fb829"
    )
