"""Preservation entries keep original bytes and confer no privacy/redaction approval."""

import io
import json
from pathlib import Path

import pytest
from PIL import Image

from html_to_markdown.content_inventory import atomic_private
from html_to_markdown.curation import json_bytes
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.models import PageMetadata
from html_to_markdown.render import serialize_document
from html_to_markdown.source_review import MediaChange, PrivacyDecision, preview_media
from html_to_markdown.source_review_jobs import _compile, _context, _request, prepare_reviews


def preservation(raw):
    return MediaChange(
        source_sha256=sha(raw),
        rectangles=[],
        labels=[],
        frames=[],
        strip_metadata=False,
        reason="Preserve the complete independently reviewed source image.",
    )


@pytest.mark.parametrize("format", ["PNG", "JPEG", "GIF"])
def test_noop_media_keeps_exact_format_metadata_animation_and_bytes(format):
    output = io.BytesIO()
    first = Image.new("RGB", (32, 20), "blue")
    if format == "GIF":
        first.save(
            output,
            format=format,
            save_all=True,
            append_images=[Image.new("RGB", first.size, "green")],
            duration=[80, 120],
            loop=3,
            comment=b"documentation example",
        )
    else:
        first.save(output, format=format)
    raw = output.getvalue()
    kept, receipt = preview_media(raw, preservation(raw), {})
    assert kept == raw
    assert receipt == {"output_sha256": sha(raw)}
    assert preview_media(raw, preservation(raw), {}) == (kept, receipt)


def test_noop_still_rejects_wrong_source_hash_missing_reason_and_invalid_frames():
    output = io.BytesIO()
    Image.new("RGB", (32, 20), "blue").save(output, format="PNG")
    raw = output.getvalue()
    change = preservation(raw)
    with pytest.raises(ValueError, match="changed media bytes"):
        preview_media(raw, change.model_copy(update={"source_sha256": "a" * 64}), {})
    with pytest.raises(ValueError, match="rationale"):
        preview_media(raw, change.model_copy(update={"reason": ""}), {})
    invalid = MediaChange.model_validate(
        {**change.model_dump(), "frames": [{"frame": 1, "rectangles": [], "labels": []}]}
    )
    with pytest.raises(ValueError, match="static media"):
        preview_media(raw, invalid, {})


def mixed_media_capture(capture: Path, format: str, extension: str) -> tuple[bytes, bytes]:
    inventory = json.loads((capture / "inventory.json").read_bytes())
    item = inventory["documents"][0]
    preserved = io.BytesIO()
    image = Image.new("RGB", (32, 20), "blue")
    options = (
        {
            "save_all": True,
            "append_images": [Image.new("RGB", image.size, "green")],
            "duration": [80, 120],
            "loop": 3,
        }
        if format == "GIF"
        else {}
    )
    image.save(preserved, format=format, **options)
    changed = io.BytesIO()
    Image.new("RGB", (32, 20), "yellow").save(changed, format="JPEG")
    for raw, suffix, mime in [
        (
            preserved.getvalue(),
            extension,
            "image/" + ("jpeg" if format == "JPEG" else format.lower()),
        ),
        (changed.getvalue(), ".jpg", "image/jpeg"),
    ]:
        digest = sha(raw)
        path = "captures/" + digest + suffix
        atomic_private(capture / path, raw)
        item["media"].append(
            {
                "path": path,
                "sha256": digest,
                "url": "https://example.com/" + digest + suffix,
                "media_type": mime,
            }
        )
    body = (
        "# Configure service\n\n"
        + "\n\n".join(
            "![Technical example](assets/" + Path(media["path"]).name + ")"
            for media in item["media"]
        )
        + "\n"
    )
    article = serialize_document(PageMetadata.model_validate(item["metadata"]), body).encode()
    atomic_private(capture / item["article_path"], article)
    item["article_sha256"] = sha(article)
    atomic_private(capture / "inventory.json", json_bytes(inventory))
    return preserved.getvalue(), changed.getvalue()


@pytest.mark.parametrize("format,extension", [("PNG", ".png"), ("JPEG", ".jpg"), ("GIF", ".gif")])
def test_compiled_article_keeps_noop_media_routes_and_mixed_transform_replay(
    capture: Path, tmp_path: Path, format: str, extension: str
) -> None:
    preserved, changed = mixed_media_capture(capture, format, extension)
    output = tmp_path / "review"
    prepare_reviews(capture, output)
    saved = json.loads(next((output / "documents").glob("*.json")).read_bytes())
    context, visual = _context(capture, output, saved)
    request = _request("gpt-6.1-sol", "privacy", context, visual, PrivacyDecision)
    payload = json.loads(request["input"][0]["content"][0]["text"])
    proposal = PrivacyDecision(
        identity=context["identity"],
        binding_sha256=payload["binding_sha256"],
        findings=[],
        values=[],
        text_changes=[],
        unresolved=[],
        media_changes=[
            preservation(preserved),
            MediaChange(
                source_sha256=sha(changed),
                rectangles=[[1, 1, 5, 5]],
                labels=[],
                frames=[],
                strip_metadata=False,
                reason="Clear the independently reviewed identifying region.",
            ),
        ],
    )
    compiled = _compile(capture, output, saved, context, proposal)
    candidate, _, _, files = compiled
    assets = {Path(path).name: raw for path, raw in files.items() if "/assets/" in path}
    kept_name = sha(preserved) + extension
    assert assets[kept_name] == preserved
    assert "assets/" + kept_name in candidate["body"]
    assert candidate["media_transforms"][sha(preserved)] == {"output_sha256": sha(preserved)}
    transformed = next(raw for name, raw in assets.items() if name != kept_name)
    assert transformed.startswith(b"\x89PNG") and transformed != changed
    assert compiled == _compile(capture, output, saved, context, proposal)
