"""Published privacy derivatives use a byte-stable PNG encoding across hosts."""

import hashlib
import io

import pytest
from PIL import Image

from html_to_markdown.media_encoding import png_bytes


@pytest.mark.parametrize("mode", ["RGB", "RGBA", "L"])
def test_portable_png_round_trips_every_source_pixel_and_has_stable_encoding(mode):
    image = Image.new(mode, (23, 17))
    for y in range(image.height):
        for x in range(image.width):
            pixel = (x * 7 % 256, y * 11 % 256, (x + y) * 13 % 256)
            image.putpixel(
                (x, y), pixel if mode == "RGB" else (*pixel, 127) if mode == "RGBA" else pixel[0]
            )
    first = png_bytes(image)
    assert first == png_bytes(image)
    with Image.open(io.BytesIO(first)) as decoded:
        assert decoded.mode == image.mode and decoded.size == image.size
        assert decoded.tobytes() == image.tobytes()
    # The chosen stored-deflate encoding avoids platform-dependent compression.
    expected = io.BytesIO()
    image.save(expected, format="PNG", compress_level=0, optimize=False)
    assert first == expected.getvalue()


def test_portable_png_known_cross_host_receipt():
    image = Image.new("RGB", (2, 2), "blue")
    assert (
        hashlib.sha256(png_bytes(image)).hexdigest()
        == "def9356b24f0030e6c62bc7d9dc6a1e4c68cb53e56d632ee8f626bb9b64b583f"
    )
