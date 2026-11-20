"""Captured OCR remains exact while offline replay rejects incomplete or altered frames."""

import io
import json
from copy import deepcopy

import pytest
from PIL import Image

from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.enrichment.media import all_frame_inputs
from html_to_markdown.reference_repair_media import seed_candidate_media


def candidate_media(tmp_path, animated=False):
    picture = Image.new("RGB", (8, 8), "white")
    stream = io.BytesIO()
    if animated:
        picture.save(
            stream,
            format="GIF",
            save_all=True,
            append_images=[Image.new("RGB", (8, 8), "blue")],
            duration=[100, 200],
            loop=0,
        )
    else:
        picture.save(stream, format="PNG")
    raw = stream.getvalue()
    digest = sha(raw)
    path = tmp_path / "asset"
    path.write_bytes(raw)
    frames = [info for _, info in all_frame_inputs(path, perform_ocr=False)]
    for frame in frames:
        frame.update(text="Captured technical text.", engine="captured-ocr", uncertain=False)
    return {"media": {digest: {"frame_count": len(frames), "frames": frames}}}, {digest: path}


def test_replay_preserves_captured_ocr_without_rerunning_host_ocr(tmp_path, monkeypatch):
    candidate, blobs = candidate_media(tmp_path)
    before = deepcopy(candidate)
    original = all_frame_inputs

    def native(path, **kwargs):
        assert kwargs == {"perform_ocr": False}
        return original(path, **kwargs)

    monkeypatch.setattr("html_to_markdown.reference_repair_media.all_frame_inputs", native)
    seed_candidate_media(candidate, blobs, tmp_path / "output")
    digest = next(iter(blobs))
    frames = json.loads((tmp_path / "output/media" / (digest + "-native-3.json")).read_bytes())
    assert frames[0][1] == before["media"][digest]["frames"][0]
    assert candidate == before


@pytest.mark.parametrize(
    "fault",
    [
        "inventory",
        "bytes",
        "frames",
        "width",
        "duration",
        "metadata",
        "digest",
        "ocr_type",
        "unknown",
    ],
)
def test_replay_rejects_changed_capture_or_frame_guards(tmp_path, fault):
    candidate, blobs = candidate_media(tmp_path)
    digest = next(iter(blobs))
    receipt = candidate["media"][digest]
    if fault == "inventory":
        blobs = {}
    elif fault == "bytes":
        blobs[digest].write_bytes(b"changed image")
    elif fault == "frames":
        receipt["frames"].clear()
    else:
        key, value = {
            "width": ("width", 1),
            "duration": ("duration", 100),
            "metadata": ("metadata", {"new": "unreviewed"}),
            "digest": ("visual_input_sha256", "0" * 64),
            "ocr_type": ("text", None),
            "unknown": ("unreviewed_field", "new"),
        }[fault]
        receipt["frames"][0][key] = value
    with pytest.raises(ValueError, match="source-reference replay"):
        seed_candidate_media(candidate, blobs, tmp_path / "output")


def test_animation_replay_preserves_every_frame_and_timing(tmp_path):
    candidate, blobs = candidate_media(tmp_path, animated=True)
    seed_candidate_media(candidate, blobs, tmp_path / "output")
    digest = next(iter(blobs))
    frames = json.loads((tmp_path / "output/media" / (digest + "-native-3.json")).read_bytes())
    assert len(frames) == 2 and [v[1]["duration"] for v in frames] == [100, 200]
    assert [v[1] for v in frames] == candidate["media"][digest]["frames"]
