import base64
import io
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, PngImagePlugin

from html_to_markdown.enrichment import media
from html_to_markdown.expected_frame_cache import ExpectedFrameCache


@pytest.fixture(autouse=True)
def isolate_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(media, "_EXPECTED_FRAMES", ExpectedFrameCache())
    monkeypatch.setattr(media.shutil, "which", lambda _name: None)


def raster(path: Path, color: str = "red", label: str = "technical example") -> Path:
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("Description", label)
    Image.new("RGB", (24, 16), color).save(path, pnginfo=metadata)
    return path


def count_encodings(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []
    original = media.png_bytes

    def encode(image: Image.Image, *, encoding_version: int = 1) -> bytes:
        calls.append(encoding_version)
        return original(image, encoding_version=encoding_version)

    monkeypatch.setattr(media, "png_bytes", encode)
    return calls


def test_identical_source_bytes_share_expected_frames_across_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = raster(tmp_path / "first.png")
    second = tmp_path / "second.png"
    second.write_bytes(first.read_bytes())
    calls = count_encodings(monkeypatch)
    original = media.all_frame_inputs(first, perform_ocr=False)
    repeated = media.all_frame_inputs(second, perform_ocr=False)
    assert original == repeated and calls == [2]
    assert repeated[0][1]["metadata"]["Description"] == "technical example"


def test_current_bytes_metadata_and_encoding_version_never_reuse_stale_frames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = raster(tmp_path / "source.png")
    calls = count_encodings(monkeypatch)
    first = media.all_frame_inputs(path, perform_ocr=False, encoding_version=1)
    compressed = media.all_frame_inputs(path, perform_ocr=False, encoding_version=2)
    assert first[0][1]["sha256"] == compressed[0][1]["sha256"]
    assert first[0][0] != compressed[0][0]
    raster(path, label="updated technical example")
    metadata_changed = media.all_frame_inputs(path, perform_ocr=False)
    assert metadata_changed[0][1]["metadata"]["Description"] == "updated technical example"
    assert metadata_changed[0][1]["sha256"] != compressed[0][1]["sha256"]
    raster(path, color="blue", label="updated technical example")
    pixels_changed = media.all_frame_inputs(path, perform_ocr=False)
    assert pixels_changed[0][0] != metadata_changed[0][0]
    assert calls == [1, 2, 2, 2]
    with pytest.raises(ValueError, match="encoding"):
        media.all_frame_inputs(path, perform_ocr=False, encoding_version=True)


def test_returned_mutable_metadata_cannot_poison_later_replay(tmp_path: Path) -> None:
    path = raster(tmp_path / "source.png")
    first = media.all_frame_inputs(path, perform_ocr=False)
    first[0][1]["metadata"]["Description"] = "altered"
    first[0][1]["frame_count"] = 99
    later = media.all_frame_inputs(path, perform_ocr=False)
    assert later[0][1]["metadata"]["Description"] == "technical example"
    assert later[0][1]["frame_count"] == 1
    assert first is not later and first[0][1] is not later[0][1]


def test_every_animated_frame_pixel_timing_and_loop_survives_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "animation.gif"
    colors = ["red", "green", "blue"]
    frames = [Image.new("RGB", (16, 12), color) for color in colors]
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=[80, 160, 240], loop=1)
    calls = count_encodings(monkeypatch)
    original = media.all_frame_inputs(path, perform_ocr=False)
    repeated = media.all_frame_inputs(path, perform_ocr=False)
    assert repeated == original and calls == [2, 2, 2]
    assert [info["frame"] for _, info in repeated] == [0, 1, 2]
    assert [info["duration"] for _, info in repeated] == [80, 160, 240]
    assert all(info["frame_count"] == 3 and info["loop"] == 1 for _, info in repeated)
    for (url, _), expected in zip(repeated, frames, strict=True):
        with Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))) as decoded:
            assert decoded.size == (16, 12)
            assert decoded.convert("RGB").tobytes() == expected.tobytes()


def test_ocr_and_svg_requests_keep_their_uncached_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = raster(tmp_path / "source.png")
    calls = count_encodings(monkeypatch)
    media.all_frame_inputs(path, perform_ocr=False)
    media.all_frame_inputs(path, perform_ocr=True)
    media.all_frame_inputs(path, perform_ocr=True)
    assert calls == [2, 2, 2]
    svg = tmp_path / "source.svg"
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
    attempts: list[int] = []

    def unavailable(_path: Path, **_options: Any) -> tuple[str, dict[str, Any]]:
        attempts.append(1)
        raise ValueError("SVG renderer unavailable")

    monkeypatch.setattr(media, "image_input", unavailable)
    for _ in range(2):
        with pytest.raises(ValueError, match="renderer unavailable"):
            media.all_frame_inputs(svg, perform_ocr=False)
    assert attempts == [1, 1]


def test_failed_encoding_is_preserved_as_a_failure_then_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = raster(tmp_path / "source.png")
    original = media.png_bytes
    attempts: list[int] = []

    def encode(image: Image.Image, *, encoding_version: int = 1) -> bytes:
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("interrupted encoding")
        return original(image, encoding_version=encoding_version)

    monkeypatch.setattr(media, "png_bytes", encode)
    with pytest.raises(OSError, match="interrupted encoding"):
        media.all_frame_inputs(path, perform_ocr=False)
    completed = media.all_frame_inputs(path, perform_ocr=False)
    assert media.all_frame_inputs(path, perform_ocr=False) == completed
    assert attempts == [1, 1]


def test_cache_memory_and_entry_limits_hold_during_concurrent_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = ExpectedFrameCache(max_bytes=1400, max_entries=2)
    monkeypatch.setattr(media, "_EXPECTED_FRAMES", cache)
    paths = [raster(tmp_path / f"frame-{index}.png", label=str(index)) for index in range(12)]
    barrier = threading.Barrier(4)

    def replay(index: int) -> list[tuple[str, dict[str, Any]]]:
        if index < 4:
            barrier.wait(timeout=10)
        return media.all_frame_inputs(paths[index], perform_ocr=False)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(replay, range(12)))
    assert [frames[0][1]["metadata"]["Description"] for frames in results] == [
        str(index) for index in range(12)
    ]
    assert cache.size_bytes <= 1400 and cache.entry_count <= 2


def test_oversized_frames_bypass_the_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = ExpectedFrameCache(max_bytes=1, max_entries=1)
    monkeypatch.setattr(media, "_EXPECTED_FRAMES", cache)
    path = raster(tmp_path / "source.png")
    calls = count_encodings(monkeypatch)
    first = media.all_frame_inputs(path, perform_ocr=False)
    assert media.all_frame_inputs(path, perform_ocr=False) == first
    assert calls == [2, 2] and cache.size_bytes == 0 and cache.entry_count == 0


@pytest.mark.parametrize("limit", [True, 0, -1, 1.5])
def test_cache_rejects_invalid_limits(limit: Any) -> None:
    with pytest.raises(ValueError, match="positive integers"):
        ExpectedFrameCache(max_bytes=limit)
    with pytest.raises(ValueError, match="positive integers"):
        ExpectedFrameCache(max_entries=limit)


@pytest.mark.parametrize("bound", ["bytes", "entries"])
def test_least_recently_used_frame_is_evicted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bound: str
) -> None:
    cache = ExpectedFrameCache()
    monkeypatch.setattr(media, "_EXPECTED_FRAMES", cache)
    paths = [raster(tmp_path / f"source-{index}.png", label=str(index)) for index in range(3)]
    media.all_frame_inputs(paths[0], perform_ocr=False)
    entry_bytes = cache.size_bytes
    cache = ExpectedFrameCache(
        max_bytes=entry_bytes * 2 if bound == "bytes" else 256 * 1024 * 1024,
        max_entries=2 if bound == "entries" else 32,
    )
    monkeypatch.setattr(media, "_EXPECTED_FRAMES", cache)
    calls = count_encodings(monkeypatch)
    for index in (0, 1, 0, 2, 0, 1):
        media.all_frame_inputs(paths[index], perform_ocr=False)
    assert calls == [2, 2, 2, 2]
    assert cache.entry_count == 2 and cache.size_bytes == entry_bytes * 2


def test_concurrent_identical_frames_encode_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = raster(tmp_path / "source.png")
    calls = count_encodings(monkeypatch)
    barrier = threading.Barrier(4)

    def replay(_index: int) -> list[tuple[str, dict[str, Any]]]:
        barrier.wait(timeout=10)
        return media.all_frame_inputs(path, perform_ocr=False)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(replay, range(4)))
    assert all(result == results[0] for result in results) and calls == [2]
    assert len({id(result[0][1]) for result in results}) == 4


def test_complete_source_working_set_reuses_frames_without_entry_churn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = [raster(tmp_path / f"source-{index}.png", label=str(index)) for index in range(80)]
    calls = count_encodings(monkeypatch)
    first = [media.all_frame_inputs(path, perform_ocr=False) for path in paths]
    repeated = [media.all_frame_inputs(path, perform_ocr=False) for path in paths]
    assert repeated == first and calls == [2] * 80
    assert all(frames[0][1]["frame_count"] == 1 for frames in repeated)
