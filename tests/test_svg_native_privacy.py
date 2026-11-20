"""Complete SVG geometry, private cache custody, and independently replayed transforms."""

import io

import pytest
from PIL import Image

from html_to_markdown.community_media import reviewed_media
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.enrichment.media import all_frame_inputs
from html_to_markdown.errors import PublicationBlockedError
from html_to_markdown.media_observations import MediaObservations, VisibleValue, observation_payload
from html_to_markdown.source_review import MediaChange, preview_media
from html_to_markdown.source_review_jobs import _media_context

SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="120" height="3000"><text x="1" y="20">Documentation example</text></svg>'


@pytest.fixture
def raster(monkeypatch):
    image = Image.new("RGB", (120, 3000), "white")
    image.putpixel((45, 2000), (0, 17, 34))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    monkeypatch.setattr(
        "html_to_markdown.enrichment.media.render_svg", lambda raw: buffer.getvalue()
    )
    monkeypatch.setattr("html_to_markdown.enrichment.media.shutil.which", lambda command: None)
    return image


def test_svg_frame_metadata_covers_complete_raster_and_source_xml(tmp_path, raster):
    source = tmp_path / "figure.svg"
    source.write_bytes(SVG)
    frames = all_frame_inputs(source, perform_ocr=False)
    assert len(frames) == 1
    info = frames[0][1]
    assert (info["frame"], info["frame_count"], info["width"], info["height"]) == (0, 1, 120, 3000)
    assert info["duration"] == 0 and info["loop"] is None
    assert info["metadata"]["source_xml"] == SVG.decode()
    assert info["sha256"] == sha(SVG)
    assert source.read_bytes() == SVG


def test_source_privacy_geometry_no_longer_fails_on_svg_or_overwrites_old_cache(tmp_path, raster):
    capture = tmp_path / "capture"
    capture.mkdir()
    source = capture / "figure.svg"
    source.write_bytes(SVG)
    output = tmp_path / "review"
    old_cache = output / "media" / (sha(SVG) + "-native-2.json")
    old_cache.parent.mkdir(parents=True)
    old_cache.write_bytes(b"original incomplete historical frame observations")
    item = {"media": [{"path": "figure.svg", "sha256": sha(SVG)}]}
    _, _, media = _media_context(capture, output, item)
    observations = MediaObservations(
        identity="source:figure",
        binding_sha256="a" * 64,
        observations=[
            VisibleValue(
                media_sha256=sha(SVG),
                frame=0,
                rectangle=[1, 1, 20, 21],
                value="example",
                reason="visible example",
            )
        ],
        complete_media_reviewed=True,
        unresolved=[],
    )
    payload = observation_payload("source:figure", "a" * 64, observations, media)
    assert payload["values"][0]["rectangle"] == [1, 1, 20, 21]
    assert old_cache.read_bytes() == b"original incomplete historical frame observations"
    assert (output / "media" / (sha(SVG) + "-native-3.json")).exists()


def change(**values):
    return MediaChange(
        source_sha256=sha(SVG),
        rectangles=[],
        labels=[],
        frames=[],
        strip_metadata=False,
        reason="independently reviewed source",
        **values,
    )


def test_unchanged_safe_svg_is_retained_exactly():
    output, transform = preview_media(SVG, change(), {})
    assert output == SVG and transform == {"output_sha256": sha(SVG)}


def test_svg_privacy_transform_replays_exact_source_raster_and_preserves_other_pixels(raster):
    proposed = MediaChange(
        source_sha256=sha(SVG),
        rectangles=[[1, 1, 20, 21]],
        labels=[],
        frames=[],
        strip_metadata=False,
        reason="identifying region",
    )
    output, transform = preview_media(SVG, proposed, {})
    assert transform["png_encoding"] == "portable-2"
    replayed, mime = reviewed_media(
        SVG, "source", {"assets": {"source": sha(SVG)}, "asset_redactions": {"source": transform}}
    )
    assert output == replayed and mime == "image/png"
    with Image.open(io.BytesIO(output)) as decoded:
        assert decoded.size == raster.size and decoded.getpixel((45, 2000)) == raster.getpixel(
            (45, 2000)
        )
    with pytest.raises(PublicationBlockedError, match="redaction is invalid"):
        reviewed_media(
            SVG,
            "source",
            {
                "assets": {"source": sha(SVG)},
                "asset_redactions": {"source": {**transform, "svg_rendering_sha256": "0" * 64}},
            },
        )


def test_active_svg_is_rejected_before_privacy_approval():
    unsafe = b'<svg xmlns="http://www.w3.org/2000/svg"><script>untrusted()</script></svg>'
    proposed = MediaChange(
        source_sha256=sha(unsafe),
        rectangles=[],
        labels=[],
        frames=[],
        strip_metadata=False,
        reason="source review",
    )
    with pytest.raises(ValueError, match="active document element"):
        preview_media(unsafe, proposed, {})
