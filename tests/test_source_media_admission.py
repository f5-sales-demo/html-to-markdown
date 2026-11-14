"""Complete admission keeps repeated media and rejects unresolved assets."""

import io
import json
from pathlib import Path

import pytest
from PIL import Image
from test_source_review_jobs import LocalClient

import html_to_markdown.source_review_jobs as jobs
from html_to_markdown.content_inventory import atomic_private
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.enrichment.media import all_frame_inputs
from html_to_markdown.source_review_jobs import prepare_reviews, run_reviews, verify_reviews


@pytest.mark.parametrize("suffix", ["", ".png"])
def test_repeated_media_references_have_one_mime_extension(
    capture: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str
) -> None:
    output = tmp_path / "review"
    inventory = json.loads((capture / "inventory.json").read_bytes())
    item = inventory["documents"][0]
    buffer = io.BytesIO()
    Image.new("RGB", (16, 16), "blue").save(buffer, format="PNG")
    raw = buffer.getvalue()
    digest = sha(raw)
    media_path = "articles/content/my-f5-com/K000123456/assets/" + digest + suffix
    atomic_private(capture / media_path, raw)
    article = (capture / item["article_path"]).read_text() + (
        f"\n![First placement](assets/{digest}{suffix})\n"
        f"\n![Second placement](assets/{digest}{suffix})\n"
    )
    item["published_path"] = item["article_path"]
    item["article_path"] = "extractions/" + sha(article) + "/index.md"
    atomic_private(capture / item["article_path"], article.encode())
    item["article_sha256"] = sha(article)
    placement = {
        "path": media_path,
        "sha256": digest,
        "url": "https://cdn.f5.com/image.png",
        "media_type": "image/png",
    }
    item["media"] = [dict(placement), dict(placement)]
    (capture / "inventory.json").write_text(json.dumps(inventory))
    monkeypatch.setattr(jobs, "CodexClient", LocalClient)
    monkeypatch.setattr(
        jobs, "all_frame_inputs", lambda path, **kwargs: all_frame_inputs(path, perform_ocr=False)
    )
    prepare_reviews(capture, output)
    result = run_reviews(capture, output, phase="all", journal=tmp_path / "journal")
    assert not result["exceptions"]
    published = output / "reviewed/content/my-f5-com/K000123456/index.md"
    body = published.read_text()
    assert ".png.png" not in body
    assert body.count("assets/" + digest + ".png") == 2
    assert body.index("First placement") < body.index("Second placement")
    assert (published.parent / "assets" / (digest + ".png")).read_bytes() == raw
    assert len(verify_reviews(capture, output)["accepted_files"]) == 2


def test_missing_source_image_rejects_before_curation(
    capture: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "review"
    inventory = json.loads((capture / "inventory.json").read_bytes())
    item = inventory["documents"][0]
    article = (capture / item["article_path"]).read_text() + "\n![Missing](assets/missing.png)\n"
    atomic_private(capture / item["article_path"], article.encode())
    item["article_sha256"] = sha(article)
    (capture / "inventory.json").write_text(json.dumps(inventory))
    monkeypatch.setattr(jobs, "CodexClient", LocalClient)
    calls = []
    monkeypatch.setattr(jobs, "preview_curation", lambda *args: calls.append(args))
    prepare_reviews(capture, output)
    result = run_reviews(capture, output, phase="all", journal=tmp_path / "journal")
    assert result["exceptions"] and not calls
    assert not verify_reviews(capture, output)["accepted_files"]
