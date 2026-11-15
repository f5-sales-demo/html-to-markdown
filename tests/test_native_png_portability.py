"""Encoder backend changes must not change complete native review inputs."""

import base64
import io

import pytest
from PIL import Image

from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.enrichment.media import all_frame_inputs, image_input


def frame(offset=0):
    image = Image.new("RGB", (96, 64))
    image.putdata(
        [
            ((x // 3 + y + offset) % 256, (x * 17 + offset) % 256, (y * 7) % 256)
            for y in range(64)
            for x in range(96)
        ]
    )
    return image


def codec_variation(monkeypatch):
    original = Image.Image.save
    backend = [1]

    def varied_save(image, destination, format=None, **parameters):
        if format == "PNG" and "compress_level" not in parameters:
            parameters["compress_level"] = backend[0]
        return original(image, destination, format=format, **parameters)

    monkeypatch.setattr(Image.Image, "save", varied_save)
    return backend


def pixels(value):
    encoded = base64.b64decode(value.split(",", 1)[1])
    with Image.open(io.BytesIO(encoded)) as image:
        return image.mode, image.size, image.tobytes(), sha(encoded)


def test_single_image_ocr_and_visual_digest_survive_encoder_default_changes(tmp_path, monkeypatch):
    path = tmp_path / "technical-frame.png"
    frame().save(path)
    original = path.read_bytes()
    backend = codec_variation(monkeypatch)
    first = image_input(path, perform_ocr=False)
    backend[0] = 9
    second = image_input(path, perform_ocr=False)
    assert first == second
    mode, size, contents, digest = pixels(first[0])
    assert mode == "RGB" and size == (96, 64) and contents == frame().tobytes()
    assert first[1]["visual_input_sha256"] == digest
    assert first[1]["sha256"] == sha(original)
    assert path.read_bytes() == original


@pytest.mark.parametrize("animated", [False, True])
def test_full_frames_and_animation_metadata_survive_encoder_default_changes(
    tmp_path, monkeypatch, animated
):
    path = tmp_path / ("technical-frames.gif" if animated else "technical-frame.png")
    if animated:
        frame().save(path, save_all=True, append_images=[frame(41)], duration=[90, 130], loop=2)
    else:
        frame().save(path)
    original = path.read_bytes()
    backend = codec_variation(monkeypatch)
    first = all_frame_inputs(path, perform_ocr=False)
    backend[0] = 9
    second = all_frame_inputs(path, perform_ocr=False)
    assert first == second
    assert len(first) == (2 if animated else 1)
    with Image.open(path) as captured:
        for index, (visual, observation) in enumerate(first):
            captured.seek(index)
            mode, size, contents, digest = pixels(visual)
            assert mode == "RGB" and size == captured.size == (96, 64)
            assert contents == captured.convert("RGBA").convert("RGB").tobytes()
            assert observation["visual_input_sha256"] == digest
            assert observation["sha256"] == sha(original)
            assert observation["frame"] == index
            assert observation["frame_count"] == len(first)
            if animated:
                assert observation["duration"] == [90, 130][index]
                assert observation["loop"] == 2
    assert path.read_bytes() == original
