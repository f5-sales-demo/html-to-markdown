"""Portable lossless PNG derivatives without platform-dependent compression."""

import io

from PIL import Image


def png_bytes(image: Image.Image) -> bytes:
    """Keep every pixel while making Mac and Ubuntu replay byte-identical."""
    output = io.BytesIO()
    image.save(output, format="PNG", compress_level=0, optimize=False)
    return output.getvalue()
