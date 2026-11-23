"""Intermediate native-observation history stays in its owning baseline batch."""

# pylint: disable=protected-access

import io
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from PIL import Image
from test_baseline_global_ledgers import cloned, exhaust
from test_baseline_source_review import Client

from html_to_markdown import baseline_global_ledgers as ledger
from html_to_markdown import baseline_source_review as source
from html_to_markdown.baseline_history_lookup import HistoryLookup
from html_to_markdown.baseline_late_validation import _execution_digest
from html_to_markdown.curation import json_bytes
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.history_preservation import history_transaction, preserve_history
from html_to_markdown.models import PageMetadata
from html_to_markdown.render import serialize_document

pytest_plugins = ["test_baseline_source_review"]


class CorrectedMediaClient(Client):
    def __init__(self):
        super().__init__()
        self.observations = 0

    def close(self):
        pass

    def execute(self, body):
        evidence = super().execute(body)
        if body["text"]["format"]["name"] == "mediaobservations":
            self.observations += 1
            if self.observations <= 2:
                response = dict(evidence.response)
                value = json.loads(response["output"][0]["content"][0]["text"])
                value["unresolved"] = ["Reinspect the complete technical controls"]
                response["output"] = [
                    {"content": [{"type": "output_text", "text": json.dumps(value)}]}
                ]
                evidence = evidence.model_copy(
                    update={
                        "response": response,
                        "response_sha256": sha(
                            json.dumps(response, sort_keys=True, separators=(",", ":"))
                        ),
                    }
                )
        return evidence


@pytest.fixture
def media_batch(tmp_path, monkeypatch):
    image = io.BytesIO()
    Image.new("RGB", (20, 20), "white").save(image, format="PNG")
    raw = image.getvalue()
    digest = sha(raw)
    article = "content/docs-cloud-f5-com/how-to/origin-health/index.md"
    asset = article.removesuffix("index.md") + "assets/" + digest + ".png"
    metadata = PageMetadata(
        sourceId="docs-cloud-f5-com",
        title="Cloud origin health",
        slug="origin-health",
        category="Guides",
        url="https://docs.cloud.f5.com/docs-v2/how-to/origin-health",
    )
    body = (
        "# Cloud origin health\n\nWait 30 seconds.\n\n"
        + "![Current controls](assets/"
        + digest
        + ".png)\n"
    )
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    (baseline / "publication.json").write_text(json.dumps({"published_at": "2026-11-10T19:49:38Z"}))
    monkeypatch.setattr(
        source,
        "_verified_baseline",
        lambda *args: {article: serialize_document(metadata, body).encode(), asset: raw},
    )
    root = tmp_path / "worker"
    output = root / "capture/docs-cloud-f5-com/curation-renewal/dependency-privacy"
    inventory = source.prepare_baseline_reviews(baseline, "a" * 64, [article], output)
    monkeypatch.setenv("HTML_TO_MARKDOWN_BASELINE_HISTORY_ROOT", str(root))
    return root, output, inventory


def test_real_media_corrections_register_every_intermediate_generation(media_batch, monkeypatch):
    root, output, inventory = media_batch
    client = CorrectedMediaClient()
    monkeypatch.setattr(source, "client_for_host", lambda *args, **kwargs: client)
    report = source.review_baseline_batch(output, inventory, root / "journal")
    histories = list((output / "history").glob("*.json"))
    assert len(histories) >= 3
    assert all(sha(path.read_bytes()) == path.stem for path in histories)
    saved = json.loads((output / inventory["documents"][0]["path"]).read_bytes())
    assert len(saved["media_observation_feedback"]) == 4
    assert saved["privacy_feedback"] == []
    assert report["history_integrity_verified"] is True
    assert report["exceptions"] == 0
    assert report["publication_approved"] is False


def test_canonical_original_generation_is_owned_without_changing_allowance(prepared, tmp_path):

    root = tmp_path / "worker"
    first, donor = cloned(prepared, root, "first")
    output, record = cloned(prepared, root, "second")
    original = exhaust(first, donor)
    raw = json.dumps(original, sort_keys=True, indent=4).encode() + b"\n"
    history = first / "history" / (sha(raw) + ".json")
    history.parent.mkdir(exist_ok=True)
    history.write_bytes(raw)
    wrapped = {
        **original,
        "existing_proposal_pending": {
            "source_record_sha256": sha(raw),
            "prepared_binding": original["binding"],
        },
    }
    donor_bytes = json_bytes(wrapped)
    donor.write_bytes(donor_bytes)
    lookup = HistoryLookup(output, root)
    with history_transaction(lookup):
        ledger.reconcile_record(output, record, root, _execution_digest(), lookup=lookup)
    canonical = json_bytes(original)
    assert canonical != raw
    assert (output / "history" / (sha(raw) + ".json")).read_bytes() == raw
    assert (output / "history" / (sha(canonical) + ".json")).read_bytes() == canonical
    saved = json.loads(record.read_bytes())
    assert saved["privacy_feedback"] == original["privacy_feedback"]
    assert len(saved["privacy_feedback"]) == 3
    assert saved["existing_proposal_pending"]["new_editor_proposals"] == 0
    assert donor.read_bytes() == donor_bytes
    assert history.read_bytes() == raw
    lookup.verify()


def test_four_workers_register_only_their_own_exact_generations(tmp_path):

    barrier = Barrier(4)

    def worker(index):
        output = tmp_path / str(index) / "review"
        lookup = HistoryLookup(output, tmp_path / str(index))
        lookup.paths({})
        with history_transaction(lookup):
            barrier.wait(timeout=5)
            for generation in range(3):
                preserve_history(
                    output, json_bytes({"binding": {}, "worker": index, "generation": generation})
                )
        lookup.verify()
        assert lookup.metrics["files_parsed"] == 3
        return output, lookup

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(worker, range(4)))
    for output, lookup in results:
        # A helper call after scope exit must not retain the worker's authority.
        extra = preserve_history(output, json_bytes({"binding": {}, "unregistered": True}))
        assert lookup.custody_differences()["unregistered_histories"] == [str(extra)]
        with pytest.raises(ValueError, match="custody changed"):
            lookup.verify()


def test_nested_destination_cannot_register_foreign_history(tmp_path):

    outer = tmp_path / "outer"
    nested = tmp_path / "nested"
    lookup = HistoryLookup(outer, tmp_path / "unrelated")
    nested_lookup = HistoryLookup(nested, tmp_path / "unrelated")
    lookup.paths({})
    nested_lookup.paths({})
    with history_transaction(lookup):
        preserve_history(outer, json_bytes({"binding": {}, "outer": True}))
        foreign = preserve_history(nested, json_bytes({"binding": {}, "candidate": True}))
    lookup.verify()
    assert nested_lookup.custody_differences()["unregistered_histories"] == [str(foreign)]
    with pytest.raises(ValueError, match="custody changed"):
        nested_lookup.verify()


def test_owned_history_survives_failure_without_granting_success(media_batch, monkeypatch):

    root, output, inventory = media_batch
    client = CorrectedMediaClient()
    monkeypatch.setattr(source, "client_for_host", lambda *args, **kwargs: client)

    def fail_after_preservation(destination, record, _client):
        saved = json.loads(record.read_bytes())
        saved["failed_review_marker"] = True
        preserve_history(destination, json_bytes(saved))
        raise ValueError("independent preservation failed")

    monkeypatch.setattr(source, "review_baseline_privacy", fail_after_preservation)
    report = source.review_baseline_batch(output, inventory, root / "journal")
    assert report["history_integrity_verified"] is True
    assert report["exceptions"] == 1
    assert report["documents"][0]["disposition"] == "queued"
    assert report["publication_approved"] is False
