"""Authored classification, strict reviews and normal/offline retirement parity."""

import copy
import json
from pathlib import Path

import pytest
from test_vesctl_curation import BASE, document

from html_to_markdown.adapters.docs_cloud import DocsCloudAdapter
from html_to_markdown.curation import (
    CurationPolicy,
    blocks,
    curate_topics,
    digest,
    json_bytes,
    load_curation_policy,
)
from html_to_markdown.models import DiscoveredPage, FetchResult
from html_to_markdown.pipeline import Pipeline
from html_to_markdown.render import split_document


@pytest.mark.parametrize(
    "metadata,body",
    [
        ({"title": "CE Node Images (Legacy)"}, "# Images\n\nUse these images.\n"),
        ({"tags": ["LEGACY"]}, "# Guide\n\nUse these steps.\n"),
        (
            {"description": "This is a legacy workflow for deploying Customer Edge."},
            "# Guide\n\nUse steps.\n",
        ),
        ({}, "# Images\n\nThe images listed apply only to legacy CE Site deployments.\n"),
        ({}, "# Guide\n\nUse SMSv2 to migrate legacy CE sites, then rollback and clean up.\n"),
    ],
)
def test_authored_whole_classification(
    tmp_path: Path, metadata: dict[str, object], body: str
) -> None:
    path = document(tmp_path, "new", body, **metadata)
    assert curate_topics(tmp_path)["counts"]["remove"] == 1
    assert not path.exists()


@pytest.mark.parametrize(
    "term",
    [
        "Delegated Domains",
        "DELEGATED%20DOMAINS",
        r"Delegated\u0020Domains",
        "SMSv1",
        "ves.io/schema/site",
        "/api/config/v1/sites",
        "old API namespaces",
    ],
)
@pytest.mark.parametrize("location", ["body", "description", "caption", "code", "destination"])
def test_unreviewed_retirement_evidence(tmp_path: Path, term: str, location: str) -> None:
    body = "# Guide\n\nKeep independent routes.\n"
    metadata = {}
    if location == "description":
        metadata["description"] = term
    elif location == "caption":
        body += f"\n![{term}](assets/a.png)\n"
    elif location == "code":
        body += f"\n```text\n{term}\n```\n"
    elif location == "destination":
        body += f"\n[setup](https://example.org/{term.replace(' ', '%20')})\n"
    else:
        body += term + "\n"
    path = document(tmp_path, "new", body, **metadata)
    assert curate_topics(tmp_path)["counts"]["keep"] == 0
    assert not path.exists()


@pytest.mark.parametrize(
    "body",
    [
        "# Workload migration\n\nMigrate legacy applications between clouds.\n",
        "# Requirement\n\nKeep default boot configuration (legacy BIOS).\n",
        "# Discovery\n\nExample: exclude legacy v1 endpoints under /api/v1/* and retain /api/v2/*.\n",
        "# DNS\n\nVerify non-delegated domains with ACME challenge CNAME records.\n",
        "# Kubernetes\n\nGET /api/v1/namespaces/example/pods\n",
    ],
)
def test_current_examples_survive(tmp_path: Path, body: str) -> None:
    path = document(tmp_path, "current", body)
    assert curate_topics(tmp_path)["counts"]["keep"] == 1
    assert split_document(path.read_text())[1] == body


def test_mixed_section_review_and_changed_source(tmp_path: Path) -> None:
    before = "# Guide\n\nCurrent primary DNS instructions.\n\n## Retired option\n\nUse Delegated Domains.\n"
    path = document(tmp_path, "reviewed", before)
    metadata, before = split_document(path.read_text())
    raw = copy.deepcopy(load_curation_policy().raw)
    topic = next(t for t in raw["topics"] if t["id"] == "legacy-retired")
    block = next(
        b for b in blocks(before) if b.kind == "section" and b.text.startswith("## Retired")
    )
    after = "# Guide\n\nCurrent primary DNS instructions.\n"
    from html_to_markdown.legacy_curation import LegacyFilter

    topic["documents"].append(
        {
            "url": BASE + "reviewed",
            "disposition": "keep",
            "source_sha256": LegacyFilter.source_digest(metadata, before),
            "input_sha256": digest(before.encode()),
            "output_sha256": digest(after.encode()),
            "block_removals": [{"location": block.location, "sha256": block.sha256}],
        }
    )
    policy = CurationPolicy(raw, digest(json_bytes(raw)))
    assert curate_topics(tmp_path, policy=policy)["counts"]["keep"] == 1
    assert split_document(path.read_text())[1] == after
    assert curate_topics(tmp_path, policy=policy)["counts"]["keep"] == 1
    # Changed clean output also requires a new review.
    path.write_text(path.read_text() + "\nChanged current step.\n")
    assert curate_topics(tmp_path, policy=policy)["counts"]["omit"] == 1


def test_original_dependency_survives_other_filter_erasure(tmp_path: Path) -> None:
    target = document(
        tmp_path, "z-setup", "# Legacy CE\n\nImages apply only to legacy CE Site deployments.\n"
    )
    linker = document(
        tmp_path,
        "a-linker",
        f"# Guide\n\nCurrent routes.\n\n- AppStack needs [setup]({BASE}z-setup).\n",
    )
    audit = curate_topics(tmp_path)
    assert not target.exists() and not linker.exists()
    assert audit["counts"]["keep"] == 0


@pytest.mark.parametrize(
    "source", json.loads((Path(__file__).parent / "fixtures/clean_break/sources.json").read_text())
)
def test_current_authored_five_fixtures(tmp_path: Path, source: dict[str, str]) -> None:
    url = source["url"]
    html = (
        Path(__file__).parent / "fixtures/clean_break" / (url.rsplit("/", 1)[-1] + ".html")
    ).read_text()
    assert digest(html.encode()) == source["authored_html_sha256"]
    page = DocsCloudAdapter().extract(
        FetchResult(url=url, final_url=url, status_code=200, html=html)
    )
    legacy = load_curation_policy().legacy
    assert legacy is not None and legacy.excludes(url)
    assert legacy.whole(page.metadata.model_dump(by_alias=True), page.html)
    # A new URL still carries the source classification.
    assert legacy.original_whole(page.metadata.model_dump(by_alias=True), page.html, html)
    assert load_curation_policy().excludes(url)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "html",
    [
        '<html><head><meta name="keywords" content="network,Legacy"></head><body><main><h1>Guide</h1><p>Current steps.</p></main></body></html>',
        "<main><h1>Guide</h1><p>AppStack uses Delegated Domains.</p></main>",
    ],
)
async def test_normal_run_checks_authored_html_before_rewriting(tmp_path: Path, html: str) -> None:
    class Fetcher:
        async def fetch(self, adapter: object, url: str) -> FetchResult:
            return FetchResult(url=url, final_url=url, status_code=200, html=html)

    pipeline = Pipeline(tmp_path)
    await pipeline.fetcher.close()
    pipeline.fetcher = Fetcher()  # type: ignore[assignment]
    pipeline.store.discover([DiscoveredPage(source_id="docs-cloud-f5-com", url=BASE + "new")])
    assert await pipeline.scrape("docs-cloud-f5-com") == []
    assert not list(tmp_path.glob("content/**/index.md"))
    pipeline.store.close()


def test_reviewed_retired_image_fails_closed_on_reuse(tmp_path: Path) -> None:
    raw = copy.deepcopy(load_curation_policy().raw)
    topic = next(t for t in raw["topics"] if t["id"] == "legacy-retired")
    image = b"retired menu screenshot"
    topic["media"].append({"sha256": digest(image), "disposition": "remove"})
    path = document(
        tmp_path, "image", "# Guide\n\nCurrent instructions.\n\n![Menu](assets/a.png)\n"
    )
    (path.parent / "assets").mkdir()
    (path.parent / "assets/a.png").write_bytes(image)
    assert (
        curate_topics(tmp_path, policy=CurationPolicy(raw, digest(json_bytes(raw))))["counts"][
            "omit"
        ]
        == 1
    )
    assert not path.exists()


@pytest.mark.parametrize(
    "body,keep",
    [
        ("# Roles\n\nGET /api/web/custom/namespaces/all-ns/roles\n", False),
        ("# Roles\n\nGET /api/web/custom/namespaces/system/roles\n", True),
        ("# Example\n\nGET /api/v1/namespaces/customer/pods\n", True),
    ],
)
def test_exact_retired_namespace_preserves_current_api(
    tmp_path: Path, body: str, keep: bool
) -> None:
    path = document(tmp_path, "namespace", body)
    curate_topics(tmp_path)
    assert path.exists() == keep
