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
