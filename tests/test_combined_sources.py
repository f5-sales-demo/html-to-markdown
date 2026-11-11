"""Admitted sources compose with the newest verified baseline without source loss."""

import json
import shutil
from pathlib import Path

import pytest
from test_source_refresh import prepare
from test_source_review_jobs import LocalClient

from html_to_markdown.combined_sources import combine_sources, verify_combined
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.enrichment.engine import prepare as prepare_enrichment
from html_to_markdown.enrichment.engine import replay, verify_enrichment
from html_to_markdown.package import RELEASE_ASSET_NAMES, build_manifest, write_release
from html_to_markdown.refresh import verify_refresh
from html_to_markdown.source_inputs import seal_source, verify_source
from html_to_markdown.source_review_jobs import prepare_reviews, run_reviews
from html_to_markdown.state import StateStore


def admitted(capture: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    path = capture / "inventory.json"
    inventory = json.loads(path.read_bytes())
    inventory["binding"] = {"source": "my-f5-com", "full": True}
    inventory["completeness"]["capture"] = True
    inventory["ended_at"] = "2026-11-10T00:03:00Z"
    path.write_text(json.dumps(inventory))
    review = tmp_path / "review"
    monkeypatch.setattr("html_to_markdown.source_review_jobs.CodexClient", LocalClient)
    prepare_reviews(capture, review)
    result = run_reviews(capture, review, phase="all", journal=tmp_path / "journal")
    assert not result["exceptions"]
    bundle = tmp_path / "bundle"
    seal_source(capture, review, bundle)
    return bundle, sha((bundle / "source-review-receipt.json").read_bytes())


def test_source_admission_is_sealed_and_replayed_before_combining(
    capture: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, pin = admitted(capture, tmp_path, monkeypatch)
    verify_source(bundle, pin)
    baseline, _, receipt = prepare(tmp_path / "baseline-fixture")
    output = tmp_path / "combined"
    state = combine_sources(baseline, {"my-f5-com": (bundle, pin)}, output, receipt)
    assert set(state["sources"]) == {"my-f5-com"}
    assert len(state["documents"]) == 3
    assert state["documents"]["content/docs-cloud-f5-com/a/index.md"]["kind"] == "published"
    assert state["documents"]["content/my-f5-com/K000123456/index.md"]["kind"] == "admitted"
    verify_combined(output)
    manifest = json.loads((output / "manifest.json").read_bytes())
    assert manifest["schema_version"] == 2
    assert manifest["refresh"]["refreshed_sources"] == ["my-f5-com"]
    article = output / "content/my-f5-com/K000123456/index.md"
    article.write_text(article.read_text().replace("30 seconds", "40 seconds"))
    with pytest.raises(ValueError, match="body|metadata|inventory"):
        verify_combined(output)


def test_admission_pin_and_selected_source_cannot_be_swapped(
    capture: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, pin = admitted(capture, tmp_path, monkeypatch)
    baseline, _, receipt = prepare(tmp_path / "baseline-fixture")
    with pytest.raises(ValueError, match="digest"):
        combine_sources(baseline, {"my-f5-com": (bundle, "0" * 64)}, tmp_path / "bad-pin", receipt)
    with pytest.raises(ValueError, match="another source"):
        combine_sources(baseline, {"www-f5-com": (bundle, pin)}, tmp_path / "bad-source", receipt)


def test_refresh_and_enrichment_verify_preserved_source_chain_separately(
    capture: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, pin = admitted(capture, tmp_path, monkeypatch)
    baseline, _, receipt = prepare(tmp_path / "baseline-fixture")
    output = tmp_path / "combined"
    combine_sources(baseline, {"my-f5-com": (bundle, pin)}, output, receipt)
    artifact = tmp_path / "enrichment.json"
    clean = tmp_path / "release-baseline"
    clean.mkdir()
    for name in (*RELEASE_ASSET_NAMES, "publication.json"):
        shutil.copy2(baseline / name, clean / name)
    prepare_enrichment(
        output, clean, artifact, tag="content-20261002T000100Z", receipt_sha256=receipt
    )
    result = replay(output, artifact, sha(artifact.read_bytes()))
    assert result["counts"]["fallback"] == 3
    verify_enrichment(output)
    verify_refresh(output)
    store = StateStore(output / "state.sqlite")
    try:
        write_release(
            output, build_manifest(output, store, "2026-10-02T00:00:00Z", "2026-11-10T00:03:00Z")
        )
    finally:
        store.close()
    manifest = json.loads((output / "manifest.json").read_bytes())
    assert manifest["refresh"]["source_inputs"]["my-f5-com"]["admission_sha256"] == pin
    assert manifest["enrichment"]["artifact_sha256"] == sha(artifact.read_bytes())
    source = output / ".enrichment/curated/.refresh/sources/my-f5-com/source-review-receipt.json"
    source.write_text("changed")
    with pytest.raises(ValueError, match="digest"):
        verify_enrichment(output)
