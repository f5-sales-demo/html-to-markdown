import copy
import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from typer.testing import CliRunner

from html_to_markdown.cli import app
from html_to_markdown.content_policy import (
    ContentPolicy,
    load_content_policy,
    migrate_content,
    rewrite_text,
)
from html_to_markdown.models import DiscoveredPage, PageMetadata, PageStatus
from html_to_markdown.pipeline import Pipeline
from html_to_markdown.reconcile import (
    include_inventory_urls,
    include_previous_urls,
    reconcile_previous,
)
from html_to_markdown.render import serialize_document, split_document
from html_to_markdown.state import StateStore
from html_to_markdown.urls import stable_path

LEGACY = "https://docs.cloud.f5.com/docs-v2/api"
OP = "ves.io.schema.http_loadbalancer.API.Create"
DEST = "https://f5-sales-demo.github.io/api-specs-enriched/api-reference/load_balancers/operations/create/"
OVERVIEW = "https://f5-sales-demo.github.io/api-specs-enriched/api-reference/load_balancers/"
FALLBACK = "https://f5-sales-demo.github.io/api-specs-enriched/en/api-reference/"


@pytest.fixture
def policy() -> ContentPolicy:
    return ContentPolicy.from_catalog(
        {
            "schema_version": 1,
            "version": "1.0.0",
            "exclusions": [LEGACY, "https://docs.cloud.f5.com/docs-v2/platform/reference/api-ref"],
            "fallback": FALLBACK,
            "overrides": [],
        },
        {
            "schema_version": 1,
            "fallback": FALLBACK,
            "operations": [
                {
                    "operation_id": OP,
                    "method": "POST",
                    "path": "/api/config/namespaces/{namespace}/http_loadbalancers",
                    "resource": "http_loadbalancer",
                    "domain": "load_balancers",
                    "url": DEST,
                    "overview_url": OVERVIEW,
                    "summary": "Create HTTP Load Balancer",
                    "legacy_url": "https://docs.cloud.f5.com/docs-v2/platform/reference/api-ref/ves-io-schema-http_loadbalancer-api-create",
                }
            ],
        },
    )


def test_exact_operation_resource_fragment_and_fallback(policy: ContentPolicy) -> None:
    assert policy.resolve(LEGACY + "/http-loadbalancer#Create")["destination"] == DEST
    assert policy.resolve(LEGACY + "/http-loadbalancer#operation/" + OP)["destination"] == DEST
    assert policy.resolve(LEGACY + "/http-loadbalancer#" + OP)["destination"] == DEST
    assert (
        policy.resolve(
            "https://docs.cloud.f5.com/docs-v2/platform/reference/api-ref/ves-io-schema-http_loadbalancer-api-create"
        )["destination"]
        == DEST
    )
    resource = policy.resolve(LEGACY + "/http-loadbalancer")
    assert resource["destination"] == OVERVIEW
    assert resource["related_operations"] == []
    missing = policy.resolve(LEGACY + "/http-loadbalanc")
    assert missing["destination"] == FALLBACK
    assert missing["reason"] == "unmatched"
    assert not policy.excludes(LEGACY + "-guides")
    assert not policy.excludes("https://docs.cloud.f5.com/docs-v2/how-to/api-management")
    raw = copy.deepcopy(policy.raw)
    raw["overrides"] = [{"legacy_url": LEGACY + "/missing", "destination": DEST}]
    assert (
        ContentPolicy.from_catalog(raw, policy.catalog).resolve(LEGACY + "/missing")["reason"]
        == "explicit_override"
    )


def test_cli_examine_and_migrate_are_offline_and_stable(tmp_path: Path) -> None:
    write_doc(
        tmp_path,
        "https://docs.cloud.f5.com/docs-v2/how-to/guide",
        f"# Guide\n\nSee {LEGACY}/missing.",
    )
    runner = CliRunner()
    before = next(tmp_path.glob("content/*/**/index.md")).read_bytes()
    result = runner.invoke(app, ["examine-content", "--output", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["counts"]["mappings"] == 1
    assert next(tmp_path.glob("content/*/**/index.md")).read_bytes() == before
    result = runner.invoke(app, ["migrate-content", "--output", str(tmp_path)])
    assert result.exit_code == 0, result.output
    paths = [
        *tmp_path.glob("content/*/**/index.md"),
        tmp_path / "quality-report.json",
        tmp_path / "quality-report.md",
        tmp_path / "manifest.json",
        tmp_path / "html-to-markdown-content.tar.gz",
    ]
    digests = [p.read_bytes() for p in paths]
    result = runner.invoke(app, ["migrate-content", "--output", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert [p.read_bytes() for p in paths] == digests


def test_ambiguous_resource_and_operation_never_choose_first(policy: ContentPolicy) -> None:
    catalog = copy.deepcopy(policy.catalog)
    other = copy.deepcopy(catalog["operations"][0])
    other.update(
        operation_id="ves.io.schema.http-loadbalancer.API.Get",
        domain="other",
        url=DEST.replace("create", "get"),
        overview_url=OVERVIEW.replace("load_balancers", "other"),
    )
    catalog["operations"].append(other)
    ambiguous = ContentPolicy.from_catalog(policy.raw, catalog).resolve(
        LEGACY + "/http-loadbalancer"
    )
    assert ambiguous["destination"] == FALLBACK
    assert ambiguous["reason"] == "ambiguous_resource"
    assert len(ambiguous["candidates"]) == 2


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/api-reference/a/",
        "https://f5-sales-demo.github.io.evil.example/api-specs-enriched/api-reference/a/",
        "https://f5-sales-demo.github.io/api-specs-enriched/api-reference/../evil/",
        "https://f5-sales-demo.github.io/api-specs-enriched/en/api-reference/a/",
        DEST + "?bad=1",
    ],
)
def test_destination_boundary_checks(policy: ContentPolicy, url: str) -> None:
    catalog = copy.deepcopy(policy.catalog)
    catalog["operations"][0]["url"] = url
    with pytest.raises(ValueError):
        ContentPolicy.from_catalog(policy.raw, catalog)


def test_malformed_override_and_digest_rejected(policy: ContentPolicy, tmp_path: Path) -> None:
    raw = copy.deepcopy(policy.raw)
    raw["overrides"] = [
        {"legacy_url": LEGACY + "/a", "destination": DEST.replace("create", "unqualified")}
    ]
    with pytest.raises(ValueError):
        ContentPolicy.from_catalog(raw, policy.catalog)
    raw["catalog"] = {"path": "catalog.json", "sha256": "0" * 64}
    (tmp_path / "catalog.json").write_text(json.dumps(policy.catalog))
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="digest"):
        load_content_policy(path)


def write_doc(root: Path, url: str, body: str, source: str = "docs-cloud-f5-com") -> Path:
    path = root / "content" / source / stable_path(source, url) / "index.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        serialize_document(
            PageMetadata(
                sourceId=source,
                url=url,
                canonical_url=url,
                title="Guide",
                slug="guide",
                category="Guides",
            ),
            body,
        )
    )
    return path


def test_offline_migration_all_text_forms_images_rest_and_idempotence(
    tmp_path: Path, policy: ContentPolicy
) -> None:
    url = "https://docs.cloud.f5.com/docs-v2/how-to/guide"
    legacy_op = "https://docs.cloud.f5.com/docs-v2/platform/reference/api-ref/ves-io-schema-http_loadbalancer-api-create"
    body = f'''# Guide

See [resource]({LEGACY}/http-loadbalancer), [operation]({legacy_op}), and [relative](../api/http-loadbalancer).

[reference]: /docs-v2/api/http-loadbalancer
<a href="{legacy_op}">HTML</a>
Bare {LEGACY}/missing.
```sh
curl /api/config/namespaces/default/http_loadbalancers
echo "{legacy_op}"
```
![Diagram](assets/diagram.png)
'''
    path = write_doc(tmp_path, url, body)
    assets = path.parent / "assets"
    assets.mkdir()
    (assets / "diagram.png").write_bytes(b"image pixels")
    (assets / "orphan.png").write_bytes(b"orphan")
    retired = write_doc(tmp_path, LEGACY + "/http-loadbalancer", "retired")
    (retired.parent / "assets").mkdir()
    (retired.parent / "assets/orphan.png").write_bytes(b"retired image")
    report = migrate_content(tmp_path, policy=policy, apply=False)
    assert retired.exists()
    assert report["counts"]["excluded_documents"] == 1
    report = migrate_content(tmp_path, policy=policy)
    _, migrated = split_document(path.read_text())
    assert "docs-v2/api" not in migrated and "reference/api-ref" not in migrated
    assert "/api/config/namespaces/default/http_loadbalancers" in migrated
    assert migrated.count("## Related API reference") == 1
    assert (assets / "diagram.png").read_bytes() == b"image pixels"
    assert not (assets / "orphan.png").exists()
    assert not retired.exists()
    before = path.read_bytes()
    assert migrate_content(tmp_path, policy=policy) == report
    assert path.read_bytes() == before
    assert hashlib.sha256(before).hexdigest()


def test_seed_restore_resumed_stale_and_packaging_exclusions(tmp_path: Path) -> None:
    policy = load_content_policy()
    previous = tmp_path / "previous"
    retired = write_doc(previous, LEGACY + "/http-loadbalancer", "retired")
    guide_url = "https://docs.cloud.f5.com/docs-v2/how-to/guide"
    guide = write_doc(previous, guide_url, "guide")
    manifest = previous / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "documents": [
                    {
                        "sourceId": "docs-cloud-f5-com",
                        "url": u,
                        "path": str(p.relative_to(previous)),
                    }
                    for u, p in [(LEGACY + "/http-loadbalancer", retired), (guide_url, guide)]
                ]
            }
        )
    )
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps({"urls": [LEGACY + "/http-loadbalancer", guide_url]}))
    output = tmp_path / "output"
    store = StateStore(output / "state.sqlite")
    include_previous_urls(store, manifest, {"docs-cloud-f5-com"})
    include_inventory_urls(store, inventory, {"docs-cloud-f5-com"})
    assert [r["canonical_url"] for r in store.rows()] == [guide_url]
    stale = write_doc(output, LEGACY + "/http-loadbalancer", "stale")
    store.discover(
        [DiscoveredPage(source_id="docs-cloud-f5-com", url=LEGACY + "/http-loadbalancer")]
    )
    store.mark(LEGACY + "/http-loadbalancer", PageStatus.FAILED)
    store.mark(guide_url, PageStatus.FAILED)
    reconcile_previous(output, store, manifest, retain_previous=True)
    migrate_content(output, store, policy=policy)
    assert not stale.exists()
    assert all(not policy.excludes(r["canonical_url"]) for r in store.rows())
    assert (output / guide.relative_to(previous)).exists()
    store.close()


@pytest.mark.asyncio
async def test_fresh_discovery_filters_explicit_and_adapter_inventory(tmp_path: Path) -> None:
    pipeline = Pipeline(tmp_path)
    assert await pipeline.discover("docs-cloud-f5-com", LEGACY + "/http-loadbalancer") == 0
    with patch(
        "html_to_markdown.adapters.docs_cloud.DocsCloudAdapter.discover",
        new=AsyncMock(return_value=[DiscoveredPage(source_id="docs-cloud-f5-com", url=LEGACY)]),
    ):
        assert await pipeline.discover("docs-cloud-f5-com") == 0
    assert not pipeline.store.rows()
    pipeline.store.close()


@pytest.mark.parametrize(
    ("description", "body"),
    [
        (
            "BGP communities let you inspect the complete AS path.",
            "# Description\n\n"
            + "BGP attributes remain available for diagnostics. " * 4
            + "AS-path example: 6453 35280 55002 64501.",
        ),
        (
            "Route-leak detection identifies an unexpected change to the advertised path.",
            "# Description\n\n"
            + "Inspect the route advertisements before changing policy. " * 5
            + "Route-leak detection identifies the path change.",
        ),
        (
            "Investigate the reported connection errors and apply the documented workaround.",
            "# Description\n\n"
            + "Collect the observed diagnostic output and compare the documented conditions. " * 4
            + "Error messages support the documented workaround.",
        ),
    ],
)
def test_migration_preserves_complete_unchanged_description(
    tmp_path: Path, policy: ContentPolicy, description: str, body: str
) -> None:
    path = write_doc(tmp_path, "https://docs.cloud.f5.com/docs-v2/how-to/guide", body)
    metadata, _ = split_document(path.read_text())
    metadata["description"] = description
    path.write_text(serialize_document(PageMetadata.model_validate(metadata), body))
    migrate_content(tmp_path, policy=policy)
    result, retained = split_document(path.read_text())
    assert result["description"] == description
    assert retained.strip() == body.strip()


def test_missing_description_keeps_complete_first_paragraph(
    tmp_path: Path, policy: ContentPolicy
) -> None:
    paragraph = "Inspect the full path before changing the route policy. " * 5
    paragraph += "AS-path example: 6453 35280 55002 64501."
    body = f"# Description\n\n{paragraph}\n\nLater diagnostic details.\n"
    path = write_doc(tmp_path, "https://docs.cloud.f5.com/docs-v2/how-to/guide", body)
    migrate_content(tmp_path, policy=policy)
    metadata, _ = split_document(path.read_text())
    assert metadata["description"] == paragraph


def test_changed_body_rebuilds_complete_description_without_generated_references(
    tmp_path: Path, policy: ContentPolicy
) -> None:
    prose = f"Inspect the [operation]({LEGACY}/http-loadbalancer#Create) before configuring the listener. "
    prose += "The documented route policy and complete AS path remain applicable. " * 5
    body = "# Guide\n\n![Diagram](assets/diagram.png)\n\n```text\nnot prose\n```\n\n" + prose
    path = write_doc(tmp_path, "https://docs.cloud.f5.com/docs-v2/how-to/guide", body)
    (path.parent / "assets").mkdir()
    (path.parent / "assets/diagram.png").write_bytes(b"image bytes")
    metadata, _ = split_document(path.read_text())
    metadata["description"] = "Original summary referencing a replaced API destination."
    path.write_text(serialize_document(PageMetadata.model_validate(metadata), body))
    migrate_content(tmp_path, policy=policy)
    metadata, migrated = split_document(path.read_text())
    assert (
        metadata["description"]
        == prose.replace(f"[operation]({LEGACY}/http-loadbalancer#Create)", "operation").strip()
    )
    assert "Related API reference" in migrated
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    migrate_content(tmp_path, policy=policy)
    assert {
        p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()
    } == before


@pytest.mark.parametrize(
    "resource", ["endpoint", "cluster", "route", "advertise-policy", "virtual-host"]
)
def test_verified_docs_api_alias_is_excluded_and_resolves_identically(resource: str) -> None:
    active = load_content_policy()
    alias = "https://docs.cloud.f5.com/docs-v2/docs/api/" + resource
    canonical = "https://docs.cloud.f5.com/docs-v2/api/" + resource
    assert active.excludes(alias)
    actual, expected = active.resolve(alias), active.resolve(canonical)
    assert actual == {**expected, "legacy_url": alias}
    body = f"See [API]({alias}).\n"
    migrated, mappings = rewrite_text(body, "https://docs.cloud.f5.com/docs-v2/guide", active)
    assert migrated == f"See [API]({expected['destination']}).\n"
    assert mappings == [actual]


@pytest.mark.parametrize(
    "url",
    [
        "https://docs.cloud.f5.com/docs-v2/docs/api-guides/endpoint",
        "https://docs.cloud.f5.com/docs-v2/docs2/api/endpoint",
        "https://docs.cloud.f5.com.evil.example/docs-v2/docs/api/endpoint",
        "https://docs.cloud.f5.com:444/docs-v2/docs/api/endpoint",
        "http://docs.cloud.f5.com/docs-v2/docs/api/endpoint",
    ],
)
def test_api_alias_normalization_preserves_exact_host_and_path_boundaries(url: str) -> None:
    assert not load_content_policy().excludes(url)


def test_redirected_retired_api_identity_never_gets_a_replacement() -> None:
    active = load_content_policy()
    with pytest.raises(ValueError, match="no replacement"):
        active.resolve("https://docs.cloud.f5.com/docs-v2/docs/api/aws-vpc-site#Create")


def test_resource_links_do_not_invent_crud_related_references(
    tmp_path: Path, policy: ContentPolicy
) -> None:
    path = write_doc(
        tmp_path,
        "https://docs.cloud.f5.com/docs-v2/how-to/guide",
        f"# Guide\n\nSee [the resource]({LEGACY}/http-loadbalancer).\n",
    )
    migrate_content(tmp_path, policy=policy)
    _, body = split_document(path.read_text())
    assert f"[the resource]({OVERVIEW})" in body
    assert "Related API reference" not in body
    assert DEST not in body


def test_explicit_operation_can_supply_a_grounded_related_reference(
    tmp_path: Path, policy: ContentPolicy
) -> None:
    path = write_doc(
        tmp_path,
        "https://docs.cloud.f5.com/docs-v2/how-to/guide",
        f"# Guide\n\nSee [the operation]({LEGACY}/http-loadbalancer#Create).\n",
    )
    migrate_content(tmp_path, policy=policy)
    _, body = split_document(path.read_text())
    assert f"[the operation]({DEST})" in body
    assert "Related API reference" in body
    assert body.count(DEST) == 2
