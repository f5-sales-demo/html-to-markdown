"""One cohort shares complete replay work without granting source admission."""

# Fault injection targets the internal preparation boundary.
# pylint: disable=protected-access

import io
import json
from collections import Counter
from copy import deepcopy
from pathlib import Path

import pytest
from PIL import Image
from test_source_review_jobs import LocalClient

from html_to_markdown import admitted_baseline_sources as fresh
from html_to_markdown import baseline_source_review as retained
from html_to_markdown import source_review_jobs
from html_to_markdown.content_inventory import atomic_private
from html_to_markdown.curation import json_bytes
from html_to_markdown.curation_renewal_dependencies import prepare_dependencies
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.models import PageMetadata
from html_to_markdown.render import serialize_document
from html_to_markdown.source_inputs import seal_source


@pytest.fixture
# pylint: disable-next=too-many-locals
def cohort(capture, tmp_path, monkeypatch):
    template = json.loads((capture / "inventory.json").read_bytes())["documents"][0]
    sources = {}
    baseline_files = {}
    articles = []
    for source_id, count in [("my-f5-com", 2), ("docs-cloud-f5-com", 3)]:
        root = tmp_path / source_id
        root.mkdir()
        documents = []
        for index in range(count):
            identifier = f"K00012345{index}" if source_id == "my-f5-com" else f"procedure-{index}"
            url = (
                f"https://my.f5.com/manage/s/article/{identifier}"
                if source_id == "my-f5-com"
                else f"https://docs.cloud.f5.com/docs-v2/how-to/{identifier}"
            )
            metadata = PageMetadata(
                sourceId=source_id,
                title=f"Configure service {index}",
                slug=identifier.lower(),
                url=url,
                category="knowledge",
            )
            pixels = io.BytesIO()
            Image.new("RGB", (16, 16), "blue").save(pixels, format="PNG")
            media = pixels.getvalue()
            media_pin = sha(media)
            body = (
                f"# Configure service {index}\n\n"
                f"Run `curl --retry {index + 3}` within 30 seconds.\n\n"
                f"![Diagram](assets/{media_pin}.png)\n"
            )
            article = f"content/{source_id}/{identifier}/index.md"
            raw = serialize_document(metadata, body).encode()
            media_path = f"articles/content/{source_id}/{identifier}/assets/{media_pin}.png"
            authored = f"<main><h1>Configure service {index}</h1><p>Run curl.</p></main>".encode()
            html_path = f"captures/{sha(authored)}.html"
            item = {
                **deepcopy(template),
                "identity": f"{source_id}:{identifier}",
                "sourceId": source_id,
                "url": url,
                "captured_at": "2026-11-10T00:00:00Z",
                "article_path": "articles/" + article,
                "article_sha256": sha(raw),
                "metadata": metadata.model_dump(by_alias=True),
                "source_html_path": html_path,
                "source_html_sha256": sha(authored),
                "media": [
                    {
                        "path": media_path,
                        "sha256": media_pin,
                        "url": url + "/diagram.png",
                        "media_type": "image/png",
                    }
                ],
            }
            atomic_private(root / item["article_path"], raw)
            atomic_private(root / html_path, authored)
            atomic_private(root / media_path, media)
            documents.append(item)
            articles.append(article)
            baseline_files[article] = serialize_document(
                metadata, body + "\n```yaml\napiVersion: v1 spec: collapsed: true\n```\n"
            ).encode()
            baseline_files[str(Path(article).parent / "assets" / (media_pin + ".png"))] = media
        atomic_private(
            root / "inventory.json",
            json_bytes(
                {
                    "binding": {"source": source_id, "full": True},
                    "completeness": {"enumeration": True, "capture": True},
                    "documents": documents,
                    "ended_at": "2026-11-10T00:03:00Z",
                }
            ),
        )
        review = tmp_path / (source_id + "-review")
        monkeypatch.setattr(source_review_jobs, "CodexClient", LocalClient)
        source_review_jobs.prepare_reviews(root, review)
        report = source_review_jobs.run_reviews(
            root, review, phase="all", journal=tmp_path / "journal"
        )
        assert not report["exceptions"]
        bundle = tmp_path / (source_id + "-bundle")
        seal_source(root, review, bundle)
        sources[source_id] = (bundle, sha((bundle / fresh.MARKER).read_bytes()))
    baseline_files["manifest.json"] = json_bytes(
        {
            "documents": [
                {"path": article, "provenance": {"last_success_at": "2026-11-01T00:00:00Z"}}
                for article in articles
            ]
        }
    )
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    (baseline / "publication.json").write_bytes(
        json_bytes({"published_at": "2026-11-01T00:00:00Z"})
    )
    monkeypatch.setattr(fresh, "_verified_baseline", lambda *args: deepcopy(baseline_files))
    monkeypatch.setattr(retained, "_verified_baseline", lambda *args: deepcopy(baseline_files))
    output = tmp_path / "renewals"
    inventory = prepare_dependencies(baseline, "a" * 64, articles, output)
    records = {}
    for row in inventory["documents"]:
        path = output / row["source_privacy_path"]
        saved = json.loads(path.read_bytes())
        saved["privacy_feedback"] = ["first rejection", "second rejection"]
        path.write_bytes(json_bytes(saved))
        records[str(path)] = path.read_bytes()
    return baseline, output, articles, sources, records, baseline_files


def _prepare(cohort):
    baseline, output, articles, sources, *_ = cohort
    return prepare_dependencies(baseline, "a" * 64, articles, output, sources)


def _watch_replay(monkeypatch):
    calls = []
    actual = fresh.verify_source

    def verify(directory, digest):
        calls.append((directory, digest))
        return actual(directory, digest)

    monkeypatch.setattr(fresh, "verify_source", verify)
    return calls


def test_cohort_replays_original_and_copy_twice_per_source_then_verifies_independently(
    cohort, monkeypatch
):
    calls = _watch_replay(monkeypatch)
    inventory = _prepare(cohort)
    _, output, articles, sources, records, _ = cohort
    assert len(inventory["documents"]) == len(articles) == 5
    assert len(calls) == 4 * len(sources) == 8
    assert set(Counter(calls).values()) == {2}
    for row in inventory["documents"]:
        context = json.loads((output / row["context_path"]).read_bytes())
        assert "collapsed: true" not in context["body"]
        assert "30 seconds" in context["body"]
        retained.verify_baseline_prerequisite(output, context)
    assert len(calls) == 13, "each later independent verifier must run complete source replay"
    before = len(calls)
    assert _prepare(cohort) == inventory
    assert len(calls) - before == 8, "a later cohort cannot reuse a persistent approval cache"
    assert all(Path(path).read_bytes() == raw for path, raw in records.items())


@pytest.mark.parametrize("source", ["my-f5-com", "docs-cloud-f5-com"])
def test_negative_source_lookups_remain_queued_without_replay(cohort, monkeypatch, source):
    _, output, articles, sources, *_ = cohort
    bundle, _ = sources[source]
    receipt = json.loads((bundle / fresh.MARKER).read_bytes())
    for row in receipt["documents"]:
        row["disposition"] = "queued"
    raw = json_bytes(receipt)
    atomic_private(bundle / fresh.MARKER, raw)
    sources[source] = (bundle, sha(raw))
    calls = _watch_replay(monkeypatch)
    inventory = _prepare(cohort)
    assert len(calls) == 4
    assert all(directory != bundle for directory, _ in calls)
    assert len(inventory["documents"]) == len(articles)
    for row in inventory["documents"]:
        original = json.loads((output / row["source_privacy_path"]).read_bytes())
        if original["binding"]["baseline_article"].startswith("content/" + source + "/"):
            assert "context_path" not in row


@pytest.mark.parametrize("target", ["original", "copy", "original-media", "copy-media"])
def test_cohort_source_or_native_drift_cannot_publish_dependency_inventory(
    cohort, monkeypatch, target
):
    _, output, articles, sources, *_ = cohort
    before = (output / "dependency-inventory.json").read_bytes()
    persist = fresh._persist_context

    def changed(out, context, files, visual):
        persist(out, context, files, visual)
        if context["evidence"]["baseline_article"] != articles[-1]:
            return
        source = context["metadata"]["sourceId"]
        directory = (
            sources[source][0]
            if target.startswith("original")
            else (out / context["evidence"]["admitted_source"]["path"])
        )
        article = directory / articles[-1]
        if target.endswith("media"):
            pin = next(iter(context["media"]))
            path = article.parent / "assets" / (pin + ".png")
            pixels = io.BytesIO()
            Image.new("RGB", (16, 16), "red").save(pixels, format="PNG")
            new_bytes = pixels.getvalue()
        else:
            path = article
            new_bytes = path.read_bytes() + b"\nChanged technical instruction.\n"
        atomic_private(path, new_bytes)

    monkeypatch.setattr(fresh, "_persist_context", changed)
    with pytest.raises(ValueError):
        _prepare(cohort)
    assert (output / "dependency-inventory.json").read_bytes() == before


def test_transient_candidate_change_after_shared_replay_is_rejected_by_article_pin(
    cohort, monkeypatch
):
    _, output, articles, *_ = cohort
    before = (output / "dependency-inventory.json").read_bytes()
    actual = fresh._candidate

    def candidate(directory, article, out):
        if article != articles[-1]:
            return actual(directory, article, out)
        path = directory / article
        raw = path.read_bytes()
        atomic_private(path, raw + b"\nInvented instruction.\n")
        try:
            return actual(directory, article, out)
        finally:
            atomic_private(path, raw)

    monkeypatch.setattr(fresh, "_candidate", candidate)
    with pytest.raises(ValueError, match="accepted file"):
        _prepare(cohort)
    assert (output / "dependency-inventory.json").read_bytes() == before


def test_source_record_pin_checked_after_earlier_article_replay(cohort, monkeypatch):
    _, output, articles, *_ = cohort
    actual = fresh._source_evidence
    before = (output / "dependency-inventory.json").read_bytes()

    def evidence(directory, entry, candidate):
        if entry["published_path"] == articles[-1]:
            path = directory / ".admission/review" / entry["record"]
            saved = json.loads(path.read_bytes())
            saved["reason"] = "forged admission"
            atomic_private(path, json_bytes(saved))
        return actual(directory, entry, candidate)

    monkeypatch.setattr(fresh, "_source_evidence", evidence)
    with pytest.raises(ValueError, match="record"):
        _prepare(cohort)
    assert (output / "dependency-inventory.json").read_bytes() == before


def test_cohort_requires_identical_current_controller_and_policy_replay(cohort, monkeypatch):
    actual = fresh.verify_source
    counts = Counter()

    def verify(directory, digest):
        counts[directory] += 1
        receipt = actual(directory, digest)
        if counts[directory] == 2:
            receipt["review_compatibility_transition"] = {"current_controller": "changed"}
        return receipt

    monkeypatch.setattr(fresh, "verify_source", verify)
    with pytest.raises(ValueError, match="changed during"):
        _prepare(cohort)


def test_baseline_bytes_cannot_change_during_cohort_preparation(cohort, monkeypatch):
    *_, baseline_files = cohort
    calls = 0

    def baseline(*args):
        nonlocal calls
        calls += 1
        value = deepcopy(baseline_files)
        if calls == 2:
            value[cohort[2][-1]] += b"\nChanged baseline.\n"
        return value

    monkeypatch.setattr(fresh, "_verified_baseline", baseline)
    with pytest.raises(ValueError, match="baseline.*changed"):
        _prepare(cohort)


def test_failed_or_interrupted_cohort_is_not_cached_or_promoted(cohort, monkeypatch):
    _, output, _, _, records, _ = cohort
    before = (output / "dependency-inventory.json").read_bytes()
    actual = fresh._persist_context
    failed = False

    def interrupted(*args):
        nonlocal failed
        actual(*args)
        if not failed:
            failed = True
            raise InterruptedError("preparation interrupted")

    monkeypatch.setattr(fresh, "_persist_context", interrupted)
    with pytest.raises(InterruptedError):
        _prepare(cohort)
    assert (output / "dependency-inventory.json").read_bytes() == before
    monkeypatch.setattr(fresh, "_persist_context", actual)
    calls = _watch_replay(monkeypatch)
    assert len(_prepare(cohort)["documents"]) == 5
    assert len(calls) == 8
    assert all(Path(path).read_bytes() == raw for path, raw in records.items())


@pytest.mark.parametrize(
    "target",
    [
        "record",
        "original-custody",
        "prepared-context",
        "prepared-record",
        "prepared-article",
        "prepared-media",
    ],
)
def test_earlier_candidate_custody_is_rechecked_at_final_cohort_boundary(
    cohort, monkeypatch, target
):
    _, output, articles, _, records, _ = cohort
    before = (output / "dependency-inventory.json").read_bytes()
    actual = fresh._persist_context
    first_context = None

    def changed(out, context, files, visual):
        nonlocal first_context
        actual(out, context, files, visual)
        if first_context is None:
            first_context = context
        if context["evidence"]["baseline_article"] != articles[-1]:
            return
        if target == "record":
            path = Path(next(iter(records)))
            saved = json.loads(path.read_bytes())
            saved["privacy_feedback"].append("unexpected history change")
            atomic_private(path, json_bytes(saved))
        elif target == "original-custody":
            path = Path(next(iter(records)))
            saved = json.loads(path.read_bytes())
            source = out / "dependency-privacy" / saved["source"]
            article = source / next(
                name for name in saved["input_file_hashes"] if name.endswith("index.md")
            )
            atomic_private(article, article.read_bytes() + b"\nChanged retained input.\n")
        elif target == "prepared-context":
            path = out / first_context["binding_sha256"] / "visual.json"
            atomic_private(path, json_bytes([]))
        elif target == "prepared-record":
            path = out / first_context["binding_sha256"] / "context.json"
            atomic_private(path, json_bytes({}))
        else:
            suffix = ".png" if target == "prepared-media" else "index.md"
            name = next(
                name
                for name in first_context["evidence"]["privacy_candidate"]["file_hashes"]
                if name.endswith(suffix)
            )
            path = out / first_context["binding_sha256"] / "inputs" / name
            atomic_private(path, path.read_bytes() + b"changed prepared bytes")

    monkeypatch.setattr(fresh, "_persist_context", changed)
    with pytest.raises(ValueError):
        _prepare(cohort)
    assert (output / "dependency-inventory.json").read_bytes() == before
