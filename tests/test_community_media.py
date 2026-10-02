"""Reviewed media edits remove pixels and bind both input and published output."""

import hashlib
import io

import pytest
from PIL import Image

from html_to_markdown.community_media import reviewed_media
from html_to_markdown.errors import PublicationBlockedError

URL = "https://community.f5.com/diagram.png"


def image_bytes():
    image = Image.new("RGB", (10, 10), "red")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_unchanged_approved_media():
    content = image_bytes()
    review = {"assets": {URL: hashlib.sha256(content).hexdigest()}}
    assert reviewed_media(content, URL, review) == (content, None)
    with pytest.raises(PublicationBlockedError, match="bytes differ"):
        reviewed_media(content + b"changed", URL, review)


def test_redaction_replaces_pixels_and_strips_metadata():
    content = image_bytes()
    expected = Image.new("RGB", (10, 10), "red")
    from PIL import ImageDraw

    ImageDraw.Draw(expected).rectangle((0, 0, 4, 4), fill="white")
    output = io.BytesIO()
    expected.save(output, format="PNG", optimize=False)
    review = {
        "assets": {URL: hashlib.sha256(content).hexdigest()},
        "asset_redactions": {
            URL: {
                "rectangles": [[0, 0, 5, 5]],
                "output_sha256": hashlib.sha256(output.getvalue()).hexdigest(),
            }
        },
    }
    result, media = reviewed_media(content, URL, review)
    assert result == output.getvalue()
    assert media == "image/png"
    image = Image.open(io.BytesIO(result))
    assert image.getpixel((0, 0)) == (255, 255, 255)
    assert image.getpixel((9, 9)) == (255, 0, 0)
    assert not image.info
    review["asset_redactions"][URL]["output_sha256"] = "0" * 64
    with pytest.raises(PublicationBlockedError, match="reviewed output"):
        reviewed_media(content, URL, review)


@pytest.mark.parametrize(
    "rectangles", [[], [[0, 0, 11, 11]], [[1, 1, 0, 0]], [[0, 0, 1]], [["0", 0, 1, 1]]]
)
def test_bad_redactions_fail_closed(rectangles):
    content = image_bytes()
    review = {
        "assets": {URL: hashlib.sha256(content).hexdigest()},
        "asset_redactions": {URL: {"rectangles": rectangles}},
    }
    with pytest.raises(PublicationBlockedError):
        reviewed_media(content, URL, review)


def test_metadata_only_policy_preserves_pixels_and_binds_output():
    from PIL.PngImagePlugin import PngInfo

    image = Image.new("RGB", (10, 10), "blue")
    metadata = PngInfo()
    metadata.add_text("comment", "Synthetic private annotation")
    source = io.BytesIO()
    image.save(source, format="PNG", pnginfo=metadata)
    expected = io.BytesIO()
    image.save(expected, format="PNG", optimize=False)
    content = source.getvalue()
    review = {
        "assets": {URL: hashlib.sha256(content).hexdigest()},
        "asset_redactions": {
            URL: {
                "strip_metadata": True,
                "rectangles": [],
                "output_sha256": hashlib.sha256(expected.getvalue()).hexdigest(),
            }
        },
    }
    result, media = reviewed_media(content, URL, review)
    assert result == expected.getvalue()
    assert media == "image/png"
    published = Image.open(io.BytesIO(result))
    assert not published.info
    assert published.mode == image.mode
    assert published.tobytes() == image.tobytes()


def test_metadata_policy_preserves_transparent_diagram():
    image = Image.new("RGBA", (10, 10), (0, 0, 255, 0))
    content = io.BytesIO()
    image.save(content, format="PNG", optimize=False)
    review = {
        "assets": {URL: hashlib.sha256(content.getvalue()).hexdigest()},
        "asset_redactions": {
            URL: {
                "strip_metadata": True,
                "rectangles": [],
                "output_sha256": hashlib.sha256(content.getvalue()).hexdigest(),
            }
        },
    }
    result, _ = reviewed_media(content.getvalue(), URL, review)
    assert Image.open(io.BytesIO(result)).getpixel((0, 0)) == (0, 0, 255, 0)


def test_animated_media_transform_cannot_discard_later_frames():
    source = io.BytesIO()
    Image.new("RGB", (10, 10), "blue").save(
        source,
        format="GIF",
        save_all=True,
        append_images=[Image.new("RGB", (10, 10), "red")],
        duration=[100, 200],
        loop=0,
    )
    content = source.getvalue()
    still = io.BytesIO()
    Image.new("RGB", (10, 10), "blue").save(still, format="PNG", optimize=False)
    review = {
        "assets": {URL: hashlib.sha256(content).hexdigest()},
        "asset_redactions": {
            URL: {
                "strip_metadata": True,
                "rectangles": [],
                "output_sha256": hashlib.sha256(still.getvalue()).hexdigest(),
            }
        },
    }
    with pytest.raises(PublicationBlockedError, match="animated"):
        reviewed_media(content, URL, review)
    del review["asset_redactions"]
    assert reviewed_media(content, URL, review) == (content, None)


def test_reviewed_label_preserves_diagram_relationships():
    from PIL import ImageDraw, ImageFont

    image = Image.new("RGB", (160, 40), "blue")
    source = io.BytesIO()
    image.save(source, format="PNG")
    expected = Image.new("RGB", image.size, "blue")
    draw = ImageDraw.Draw(expected)
    draw.rectangle((2, 2, 157, 27), fill="white")
    draw.text((4, 4), "blue.example.com", fill="black", font=ImageFont.load_default(size=12))
    output = io.BytesIO()
    expected.save(output, format="PNG", optimize=False)
    review = {
        "assets": {URL: hashlib.sha256(source.getvalue()).hexdigest()},
        "asset_redactions": {
            URL: {
                "rectangles": [],
                "labels": [{"rectangle": [2, 2, 158, 28], "text": "blue.example.com", "size": 12}],
                "output_sha256": hashlib.sha256(output.getvalue()).hexdigest(),
            }
        },
    }
    result, _ = reviewed_media(source.getvalue(), URL, review)
    assert result == output.getvalue()
    assert Image.open(io.BytesIO(result)).getpixel((159, 39)) == (0, 0, 255)
    review["asset_redactions"][URL]["labels"][0]["text"] = "example.com " * 30
    with pytest.raises(PublicationBlockedError, match="invalid"):
        reviewed_media(source.getvalue(), URL, review)


@pytest.mark.parametrize(
    "labels",
    [
        "invalid",
        [None],
        [{}],
        [{"rectangle": [0, 0, 10, 10], "text": 3, "size": 8}],
        [{"rectangle": [0, 0, 10, 10], "text": "", "size": 8}],
        [{"rectangle": [0, 0, 10, 10], "text": "é", "size": 8}],
        [{"rectangle": [0, 0, 10, 10], "text": "\n", "size": 8}],
        [{"rectangle": [0, 0, 10, 10], "text": "x", "size": True}],
        [{"rectangle": [0, 0, 10, 10], "text": "x", "size": "8"}],
        [{"rectangle": [0, 0, 10, 10], "text": "x", "size": 73}],
    ],
)
def test_malformed_labels_fail_closed(labels):
    content = image_bytes()
    review = {
        "assets": {URL: hashlib.sha256(content).hexdigest()},
        "asset_redactions": {URL: {"strip_metadata": True, "labels": labels}},
    }
    with pytest.raises(PublicationBlockedError, match="invalid"):
        reviewed_media(content, URL, review)
