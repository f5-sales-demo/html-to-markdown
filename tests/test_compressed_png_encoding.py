"""Versioned encoding preserves image evidence while reducing private request bytes."""

import io
import struct

import pytest
from PIL import Image

from html_to_markdown.media_encoding import png_bytes


def chunks(raw):
    result = []
    offset = 8
    while offset < len(raw):
        length = struct.unpack(">I", raw[offset : offset + 4])[0]
        block = raw[offset : offset + 12 + length]
        if block[4:8] != b"IDAT":
            result.append(block)
        offset += 12 + length
    return result


@pytest.mark.parametrize("mode", ["RGB", "RGBA", "L", "P"])
def test_version_two_preserves_full_pixels_palette_and_non_image_chunks(mode):
    image = Image.new(mode, (256, 128))
    if mode == "P":
        image.putpalette([value for i in range(256) for value in (i, 255 - i, i // 2)])
        image.info["transparency"] = 7
    image.info["icc_profile"] = b"retained captured color-profile bytes"
    exif = Image.Exif()
    exif[270] = "Captured technical figure attribution"
    image.info["exif"] = exif.tobytes()
    for y in range(image.height):
        for x in range(image.width):
            color = (x // 8 % 256, y // 4 % 256, (x + y) // 16 % 256)
            image.putpixel(
                (x, y), color if mode == "RGB" else (*color, 128) if mode == "RGBA" else color[0]
            )
    original = png_bytes(image, encoding_version=1)
    compressed = png_bytes(image, encoding_version=2)
    assert chunks(original) == chunks(compressed)
    assert compressed == png_bytes(image, encoding_version=2)
    with Image.open(io.BytesIO(original)) as before, Image.open(io.BytesIO(compressed)) as after:
        before.load()
        after.load()
        assert before.mode == after.mode and before.size == after.size
        assert before.tobytes() == after.tobytes()
        assert before.info == after.info
        assert before.getexif().tobytes() == after.getexif().tobytes()
        if before.palette:
            assert before.palette.getdata() == after.palette.getdata()
    # Flat technical figures should not regress to stored raw scanlines.
    assert len(compressed) < len(original) // 10


@pytest.mark.parametrize("version", [0, 3, True, False, 2.0, "2", None])
def test_unknown_encoding_versions_are_rejected(version):
    with pytest.raises(ValueError, match="unsupported portable PNG encoding version"):
        png_bytes(Image.new("RGB", (2, 2)), encoding_version=version)


def test_default_retains_the_exact_historical_encoding():
    image = Image.new("RGBA", (16, 16), (32, 64, 128, 95))
    assert png_bytes(image) == png_bytes(image, encoding_version=1)


def test_historical_source_context_keeps_exact_uncompressed_request_and_cache(
    tmp_path, monkeypatch
):
    import html_to_markdown.source_review_jobs as jobs
    from html_to_markdown.content_inventory import atomic_private
    from html_to_markdown.curation import json_bytes
    from html_to_markdown.enrichment.analysis import sha
    from html_to_markdown.enrichment.media import all_frame_inputs

    capture = tmp_path / "capture"
    capture.mkdir()
    path = capture / "figure.png"
    image = Image.new("RGB", (100, 100), "blue")
    image.save(path)
    digest = sha(path.read_bytes())
    output = tmp_path / "review"
    old_frames = all_frame_inputs(path, perform_ocr=False, encoding_version=1)
    cache = output / "media" / (digest + "-native-2.json")
    atomic_private(cache, json_bytes(old_frames))
    item = {"media": [{"path": path.name, "sha256": digest}]}
    from html_to_markdown.media_encoding import review_media_encoding

    with review_media_encoding(1):
        visual, _, receipts = jobs._media_context(capture, output, item)
    assert visual[1]["image_url"] == old_frames[0][0]
    assert receipts[digest]["frames"] == [old_frames[0][1]]
    assert cache.read_bytes() == json_bytes(old_frames)
    assert not (output / "media" / (digest + "-native-3.json")).exists()


def test_source_encoding_scopes_are_independent_between_concurrent_reviews():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from html_to_markdown.media_encoding import current_review_encoding, review_media_encoding

    gate = Barrier(2)

    def observe(version):
        with review_media_encoding(version):
            gate.wait(timeout=5)
            return current_review_encoding()

    with ThreadPoolExecutor(max_workers=2) as pool:
        legacy = pool.submit(observe, 1)
        fresh = pool.submit(observe, 2)
        assert legacy.result() == 1 and fresh.result() == 2
    assert current_review_encoding() == 2
