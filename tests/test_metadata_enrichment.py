import json
from pathlib import Path

import pytest

from html_to_markdown.metadata import (
    MetadataPolicy,
    classify_metadata,
    enrich_snapshot,
    load_metadata_policy,
)
from html_to_markdown.models import DiscoveredPage, PageMetadata, PageStatus
from html_to_markdown.render import serialize_document, split_document
from html_to_markdown.state import StateStore
from html_to_markdown.validation import validate_snapshot

DOCS_ROOT = "https://docs.cloud.f5.com/docs-v2"
MYF5_ROOT = "https://my.f5.com/manage/s/article"


def write_document(
    output: Path, source: str, stable: str, metadata: PageMetadata, body: str
) -> Path:
    path = output / "content" / source / stable / "index.md"
    path.parent.mkdir(parents=True)
    path.write_text(serialize_document(metadata, body), encoding="utf-8")
    return path


def add_document(store: StateStore, url: str, source: str, path: Path, output: Path) -> None:
    store.discover([DiscoveredPage(source_id=source, url=url)])
    store.mark(
        url,
        PageStatus.SUCCESS,
        output_path=path.relative_to(output).as_posix(),
        digest="body",
    )


def policy() -> MetadataPolicy:
    return MetadataPolicy.model_validate(
        {
            "schema_version": 1,
            "classification_rules": [
                {
                    "source_id": "docs-cloud-f5-com",
                    "path_prefix": "/docs-v2",
                    "product": None,
                    "content_type": "reference",
                    "task_type": "reference",
                },
                {
                    "source_id": "docs-cloud-f5-com",
                    "path_prefix": "/docs-v2/client-side-defense/how-to",
                    "product": "client-side-defense",
                    "content_type": "how_to",
                    "task_type": "configure",
                },
                {
                    "source_id": "my-f5-com",
                    "path_prefix": "/manage/s/article",
                    "product": None,
                    "content_type": "knowledge_article",
                    "task_type": "support",
                },
            ],
            "alias_groups": [
                {
                    "product": "client-side-defense",
                    "aliases": ["Client-Side Defense", "CSD", "CSD"],
                }
            ],
            "overrides": [
                {
                    "url": f"{DOCS_ROOT}/client-side-defense/how-to/old",
                    "aliases": ["Legacy CSD"],
                    "lifecycle": "superseded",
                    "replacement_url": f"{DOCS_ROOT}/client-side-defense/how-to/new",
                }
            ],
            "relationships": [
                {
                    "source_url": f"{DOCS_ROOT}/client-side-defense/how-to/new",
                    "target_url": f"{MYF5_ROOT}/K000654321",
                }
            ],
        }
    )


def test_checked_in_policy_is_schema_valid_and_rules_are_most_specific() -> None:
    checked_in = load_metadata_policy()
    assert checked_in.schema_version == 1
    assert any(rule.product == "client-side-defense" for rule in checked_in.classification_rules)
    generic_how_to = classify_metadata(
        checked_in,
        "docs-cloud-f5-com",
        f"{DOCS_ROOT}/dns-management/how-to/configure-dns-load-balancer",
    )
    assert generic_how_to.product is None
    assert generic_how_to.content_type == "how_to"
    assert generic_how_to.task_type == "configure"


def test_policy_rejects_conflicting_rules_and_invalid_lifecycle() -> None:
    raw = policy().model_dump(mode="json")
    raw["classification_rules"].append(dict(raw["classification_rules"][0]))
    with pytest.raises(ValueError, match="conflicting classification"):
        MetadataPolicy.model_validate(raw)

    raw = policy().model_dump(mode="json")
    raw["overrides"][0]["replacement_url"] = None
    with pytest.raises(ValueError, match="replacement_url"):
        MetadataPolicy.model_validate(raw)


def test_enrichment_resolves_metadata_aliases_relationships_and_dates(tmp_path: Path) -> None:
    output = tmp_path / "snapshot"
    store = StateStore(output / "state.sqlite")
    source_url = f"{DOCS_ROOT}/client-side-defense/how-to/new"
    target_url = f"{MYF5_ROOT}/K000123456"
    source = PageMetadata(
        sourceId="docs-cloud-f5-com",
        title="Configure Client-Side Defense",
        slug="new",
        url=source_url,
        category="client-side-defense",
        publication_date="2024-01-01",
        modification_date="2026-09-28",
    )
    target = PageMetadata(
        sourceId="my-f5-com",
        title="Troubleshooting Client-Side Defense",
        slug="k000123456",
        url=target_url,
        category="support-solution",
        publication_date="2025-01-01",
        canonical_url=f"{MYF5_ROOT}/K000654321",
    )
    source_path = write_document(
        output,
        "docs-cloud-f5-com",
        "client-side-defense/how-to/new",
        source,
        "# Configure\n\nDo it.\n",
    )
    target_path = write_document(
        output, "my-f5-com", "K000123456", target, "# Troubleshoot\n\nFix it.\n"
    )
    add_document(store, source_url, source.source_id, source_path, output)
    add_document(store, target_url, target.source_id, target_path, output)
    store.replace_candidate_links(source_url, [target_url, source_url, "https://example.com/no"])

    enrich_snapshot(output, store, policy())
    validate_snapshot(output, store)

    enriched, body = split_document(source_path.read_text(encoding="utf-8"))
    assert body == "# Configure\n\nDo it.\n"
    assert enriched["metadata_schema"] == 1
    assert enriched["product"] == "client-side-defense"
    assert enriched["content_type"] == "how_to"
    assert enriched["task_type"] == "configure"
    assert enriched["canonical_url"] == source_url
    assert enriched["last_updated"] == "2026-09-28"
    assert enriched["language"] == "en"
    assert enriched["aliases"] == ["Client-Side Defense", "CSD"]
    assert enriched["lifecycle"] == "current"
    assert enriched["replacement_url"] is None
    assert enriched["related_documents"] == [
        {
            "relation": "support",
            "title": "Troubleshooting Client-Side Defense",
            "canonical_url": f"{MYF5_ROOT}/K000654321",
            "source_id": "my-f5-com",
            "stable_path": "K000123456",
        }
    ]
    store.close()


def test_source_last_modified_precedes_publication_and_superseded_target_must_exist(
    tmp_path: Path,
) -> None:
    output = tmp_path / "snapshot"
    store = StateStore(output / "state.sqlite")
    old_url = f"{DOCS_ROOT}/client-side-defense/how-to/old"
    metadata = PageMetadata(
        sourceId="docs-cloud-f5-com",
        title="Old configuration",
        slug="old",
        url=old_url,
        category="client-side-defense",
        publication_date="2024-01-01",
    )
    path = write_document(
        output, "docs-cloud-f5-com", "client-side-defense/how-to/old", metadata, "# Old\n"
    )
    store.discover(
        [
            DiscoveredPage(
                source_id=metadata.source_id,
                url=old_url,
                source_last_modified="Sun, 27 Sep 2026 12:00:00 GMT",
            )
        ]
    )
    store.mark(
        old_url,
        PageStatus.SUCCESS,
        output_path=path.relative_to(output).as_posix(),
        digest="body",
    )

    with pytest.raises(ValueError, match="replacement target is not in snapshot"):
        enrich_snapshot(output, store, policy())
    store.close()


def test_ambiguous_redirect_targets_are_not_emitted(tmp_path: Path) -> None:
    output = tmp_path / "snapshot"
    store = StateStore(output / "state.sqlite")
    source_url = f"{DOCS_ROOT}/client-side-defense/how-to/new"
    canonical_target = f"{DOCS_ROOT}/api"
    source = PageMetadata(
        sourceId="docs-cloud-f5-com",
        title="Source",
        slug="source",
        url=source_url,
        category="client-side-defense",
    )
    source_path = write_document(output, source.source_id, "source", source, "# Source\n")
    add_document(store, source_url, source.source_id, source_path, output)
    for index in (1, 2):
        alias_url = f"{DOCS_ROOT}/api/alias-{index}"
        target = PageMetadata(
            sourceId="docs-cloud-f5-com",
            title=f"Alias {index}",
            slug=f"alias-{index}",
            url=alias_url,
            category="api",
            canonical_url=canonical_target,
        )
        target_path = write_document(
            output, target.source_id, f"alias-{index}", target, f"# Alias {index}\n"
        )
        add_document(store, alias_url, target.source_id, target_path, output)
    store.replace_candidate_links(source_url, [canonical_target])

    empty_policy = policy().model_copy(update={"relationships": [], "overrides": []})
    enrich_snapshot(output, store, empty_policy)

    enriched, _ = split_document(source_path.read_text(encoding="utf-8"))
    assert enriched["related_documents"] == []
    store.close()


def test_policy_file_is_deterministically_serializable() -> None:
    loaded = load_metadata_policy()
    encoded = json.dumps(loaded.model_dump(mode="json"), sort_keys=True)
    assert json.loads(encoded)["schema_version"] == 1
