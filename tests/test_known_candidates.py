"""Known candidates remain reviewable when authoritative enumeration changes."""

import io
import json
import sqlite3
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from html_to_markdown import collection_service, content_inventory
from html_to_markdown.adapters.docs_cloud import DocsCloudAdapter
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.fetcher import Fetcher
from html_to_markdown.known_candidates import snapshot_known_candidates
from html_to_markdown.models import DiscoveredPage, FetchResult
from html_to_markdown.work_queue import export_queue

SOURCE = "docs-cloud-f5-com"
KNOWN = "https://docs.cloud.f5.com/docs-v2/platform/how-to/known"
CURRENT = "https://docs.cloud.f5.com/docs-v2/platform/how-to/current"
FIRST_DISCOVERY = "2026-01-01T00:00:00Z"


def seed(path: Path, url: str = KNOWN, source: str = SOURCE) -> bytes:
    data = json.dumps(
        {
            "schema_version": 1,
            "source": source,
            "documents": [
                {
                    "sourceId": source,
                    "url": url,
                    "discovered_at": FIRST_DISCOVERY,
                    "disposition": "policy_excluded",
                }
            ],
        }
    ).encode()
    path.write_bytes(data)
    return data


@pytest.fixture
def capture(monkeypatch):
    calls = []

    async def discover(_self, _fetcher):
        return [DiscoveredPage(source_id=SOURCE, url=CURRENT)]

    async def fetch(_self, _adapter, url):
        calls.append(url)
        return FetchResult(
            url=url,
            final_url=url,
            status_code=200,
            html="<main><h1>Current guide</h1><h2>Resolution</h2><p>Use a 30 second timeout. Expected result is HTTP 200.</p></main>",
        )

    monkeypatch.setattr(DocsCloudAdapter, "discover", discover)
    monkeypatch.setattr(Fetcher, "fetch", fetch)
    return calls


@pytest.mark.asyncio
async def test_missing_known_candidate_is_refetched_without_reusing_exclusion(tmp_path, capture):
    path = tmp_path / "seed.json"
    raw = seed(path)
    output = tmp_path / "capture"
    report = await content_inventory.collect_content(output, source=SOURCE, known_candidates=path)
    assert set(capture) == {KNOWN, CURRENT}
    item = next(item for item in report["documents"] if item["url"] == KNOWN)
    assert item["discovered_at"] == FIRST_DISCOVERY
    assert "previous_inventory" in item["origins"]
    assert "previous_publication" not in item["origins"]
    assert item["disposition"] == "queued" and item["capture_complete"]
    assert item["article_sha256"]
    assert not report["completeness"]["review"]
    binding = json.loads((output / "capture-binding.json").read_bytes())["request"]
    assert binding["known_candidates_sha256"] == sha(raw)
    assert (output / "known-candidates" / (sha(raw) + ".json")).read_bytes() == raw


@pytest.mark.asyncio
async def test_resume_uses_sealed_known_input_and_rejects_changed_input(tmp_path, capture):
    path = tmp_path / "seed.json"
    seed(path)
    output = tmp_path / "capture"
    original = await content_inventory.collect_content(output, source=SOURCE, known_candidates=path)
    seed(path, CURRENT)
    resumed = await content_inventory.collect_content(output, source=SOURCE, resume=True)
    assert resumed["documents"] == original["documents"]
    assert len(capture) == 2
    with pytest.raises(ValueError, match="binding"):
        await content_inventory.collect_content(
            output, source=SOURCE, resume=True, known_candidates=path
        )


@pytest.mark.asyncio
async def test_seed_tampering_prevents_resume_and_export(tmp_path, capture):
    path = tmp_path / "seed.json"
    raw = seed(path)
    root = tmp_path / "queue"
    output = root / "run" / SOURCE
    await content_inventory.collect_content(output, source=SOURCE, known_candidates=path)
    pinned = output / "known-candidates" / (sha(raw) + ".json")
    pinned.write_text("{}")
    with pytest.raises(ValueError, match="known candidate"):
        await content_inventory.collect_content(output, source=SOURCE, resume=True)
    stream = io.BytesIO()
    with pytest.raises(ValueError, match="known candidate"):
        export_queue(root, "run/" + SOURCE, stream)
    assert not stream.getvalue()


@pytest.mark.asyncio
async def test_export_preserves_exact_seed(tmp_path, capture):
    path = tmp_path / "seed.json"
    raw = seed(path)
    root = tmp_path / "queue"
    await content_inventory.collect_content(
        root / "run" / SOURCE, source=SOURCE, known_candidates=path
    )
    stream = io.BytesIO()
    export_queue(root, "run/" + SOURCE, stream)
    stream.seek(0)
    with tarfile.open(fileobj=stream) as archive:
        member = archive.extractfile("known-candidates/" + sha(raw) + ".json")
        assert member and member.read() == raw


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url,source", [("https://unrelated.example/guide", SOURCE), (KNOWN, "my-f5-com")]
)
async def test_invalid_seed_fails_before_fetch(tmp_path, capture, url, source):
    path = tmp_path / "seed.json"
    seed(path, url, source)
    with pytest.raises(ValueError):
        await content_inventory.collect_content(
            tmp_path / "capture", source=SOURCE, known_candidates=path
        )
    assert not capture


def test_snapshot_reconciles_partial_and_previously_excluded_candidates(tmp_path):
    source = "my-f5-com"
    urls = [
        "https://my.f5.com/manage/s/article/K000000001",
        "https://my.f5.com/manage/s/article/K000000002",
    ]
    for run, first, complete in [
        ("one", FIRST_DISCOVERY, False),
        ("two", "2026-02-01T00:00:00Z", True),
    ]:
        directory = tmp_path / run / source
        directory.mkdir(parents=True)
        (directory / "capture-binding.json").write_text(json.dumps({"request": {"source": source}}))
        with sqlite3.connect(directory / "inventory.sqlite") as database:
            database.execute("CREATE TABLE documents(url TEXT PRIMARY KEY,data TEXT NOT NULL)")
            for url in urls[:1] if run == "two" else urls:
                data = {
                    "sourceId": source,
                    "url": url,
                    "discovered_at": first,
                    "capture_complete": complete,
                    "disposition": "policy_excluded",
                }
                database.execute("INSERT INTO documents VALUES (?,?)", (url, json.dumps(data)))
    result = snapshot_known_candidates(tmp_path, source)
    assert {item["url"] for item in result["documents"]} == set(urls)
    assert all(item["discovered_at"] == FIRST_DISCOVERY for item in result["documents"])
    assert len(result["inputs"]) == 2
    assert all(len(item["snapshot_sha256"]) == 64 for item in result["inputs"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(schema_version=2),
        lambda d: d["documents"].append(dict(d["documents"][0])),
        lambda d: d["documents"][0].update(discovered_at="2026-01-01"),
        lambda d: d["documents"][0].update(
            url="https://name:password@docs.cloud.f5.com/docs-v2/current"
        ),
        lambda d: d["documents"][0].update(url=KNOWN + "#fragment"),
        lambda d: d["documents"][0].update(url="https://docs.cloud.f5.com/docs-v2/%2e%2e/private"),
    ],
)
async def test_malformed_or_unsafe_seed_never_captures(tmp_path, capture, mutate):
    path = tmp_path / "seed.json"
    raw = seed(path)
    data = json.loads(raw)
    mutate(data)
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        await content_inventory.collect_content(
            tmp_path / "capture", source=SOURCE, known_candidates=path
        )
    assert not capture


@pytest.mark.asyncio
async def test_missing_discovery_still_keeps_known_candidate_and_incomplete_evidence(
    tmp_path, capture, monkeypatch
):
    async def interrupted(_self, _fetcher):
        raise RuntimeError("navigation interrupted")

    monkeypatch.setattr(DocsCloudAdapter, "discover", interrupted)
    path = tmp_path / "seed.json"
    seed(path)
    report = await content_inventory.collect_content(
        tmp_path / "capture", source=SOURCE, known_candidates=path
    )
    assert capture == [KNOWN]
    assert report["completeness"]["capture"]
    assert not report["completeness"]["enumeration"]
    assert report["documents"][0]["discovered_at"] == FIRST_DISCOVERY


@pytest.mark.asyncio
async def test_scheduler_seals_historical_candidates_separately_from_publication(
    tmp_path, monkeypatch
):
    source = "my-f5-com"
    url = "https://my.f5.com/manage/s/article/K000000001"
    directory = tmp_path / "prior" / source
    directory.mkdir(parents=True)
    (directory / "capture-binding.json").write_text(json.dumps({"request": {"source": source}}))
    with sqlite3.connect(directory / "inventory.sqlite") as database:
        database.execute("CREATE TABLE documents(url TEXT PRIMARY KEY,data TEXT NOT NULL)")
        database.execute(
            "INSERT INTO documents VALUES (?,?)",
            (
                url,
                json.dumps(
                    {
                        "sourceId": source,
                        "url": url,
                        "discovered_at": FIRST_DISCOVERY,
                        "capture_complete": False,
                    }
                ),
            ),
        )
    tag = "content-20260101T000000Z"
    (tmp_path / ".baselines" / tag).mkdir(parents=True)

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, _url):
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"snapshot_tag": tag, "publication_sha256": "a" * 64},
            )

    calls = []

    async def collect(output, **kwargs):
        candidates = json.loads(kwargs["known_candidates"].read_bytes())
        publication = json.loads(kwargs["previous_manifest"].read_bytes())
        assert candidates["documents"] == [
            {"sourceId": source, "url": url, "discovered_at": FIRST_DISCOVERY}
        ]
        assert publication["documents"] == []
        calls.append(output)
        return {
            "counts": {"queued": 1},
            "completeness": {
                "enumeration": True,
                "capture": True,
                "review": False,
                "publication": False,
            },
        }

    monkeypatch.setattr(collection_service.httpx, "AsyncClient", lambda **_kwargs: Client())
    monkeypatch.setattr(
        collection_service,
        "_verified_baseline",
        lambda *_args: {"manifest.json": b'{"documents":[]}'},
    )
    monkeypatch.setattr(collection_service, "collect_content", collect)
    result = await collection_service.scheduled_collection(tmp_path, source=source, full=False)
    assert len(calls) == 1
    assert not result["sources"][source]["completeness"]["publication"]
