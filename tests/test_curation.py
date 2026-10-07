import copy
import gzip
import json
import tarfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from html_to_markdown.cli import app
from html_to_markdown.content_policy import load_content_policy
from html_to_markdown.curation import (
    CurationPolicy,
    blocks,
    corpus_digest,
    curate_topics,
    digest,
    json_bytes,
    load_curation_policy,
    media_inventory,
    remove_blocks,
    validate_curation,
)
from html_to_markdown.models import PageMetadata
from html_to_markdown.render import serialize_document, split_document

BASE = "https://docs.cloud.f5.com/docs-v2/how-to/"


def document(root: Path, name: str, body: str) -> Path:
    path = root / "content/docs-cloud-f5-com/how-to" / name / "index.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        serialize_document(
            PageMetadata(
                sourceId="docs-cloud-f5-com",
                url=BASE + name,
                canonical_url=BASE + name,
                title="Current guide",
                slug=name,
                category="Guides",
            ),
            body,
        )
    )
    return path


def registry(documents=None, media=None) -> dict:
    return {
        "schema_version": 1,
        "version": "1.0.0",
        "topics": [
            {
                "id": "test",
                "retired_identities": ["securemesh_site"],
                "retired_urls": [BASE + "retired"],
                "retired_patterns": [r"\bsecuremesh_site(?!_v2)\b"],
                "candidate_detectors": [r"\bfleet\b"],
                "documents": documents or [],
                "media": media or [],
                "evidence_inputs": [],
            }
        ],
    }


def policy(raw=None) -> CurationPolicy:
    raw = raw or registry()
    return CurationPolicy(raw, digest(json_bytes(raw)))


def decision(name: str, body: str, removals: list | None = None) -> dict:
    rules = removals or []
    return {
        "url": BASE + name,
        "disposition": "keep",
        "reason": "reviewed",
        "input_sha256": digest(body.encode()),
        "output_sha256": digest(remove_blocks(body, rules).encode()),
        "block_removals": rules,
    }


def guard(block) -> dict:
    return {"location": block.location, "sha256": block.sha256}


@pytest.mark.parametrize(
    "text",
    [
        "securemesh_site",
        r"securemesh\_site",
        "securemesh%5fsite",
        "securemesh&#95;site",
        "aws_vpc_site",
        "volterra_aws_vpc_site",
        "AWS TGW Sites",
        "ves.io/fleet",
        "smsv1",
        "Secure Mesh Site (version 1)",
    ],
)
def test_exact_retired_aliases_and_escapes(text: str) -> None:
    assert load_curation_policy().retired(text)


@pytest.mark.parametrize(
    "text",
    [
        "securemesh_site_v2",
        "views.securemesh_site_v2",
        "customer-managed AWS VPC and Azure VNet networking",
        "TGW Connect with GRE and BGP",
        "a fleet of applications",
        "authentication and API tokens",
        "unversioned Secure Mesh connectivity",
    ],
)
def test_current_scope_survives(text: str) -> None:
    assert not load_curation_policy().retired(text)


def test_catalog_retirement_precedes_mapping_and_fallback() -> None:
    active = load_content_policy()
    raw_catalog = json.loads(
        gzip.decompress(
            Path(str(__import__("html_to_markdown.content_policy", fromlist=["__file__"]).__file__))
            .with_name("api_destination_catalog.json.gz")
            .read_bytes()
        )
    )
    retired = [
        op
        for op in raw_catalog["operations"]
        if op["resource"].rsplit(".", 1)[-1] in load_curation_policy().retired_identities
    ]
    assert len(retired) == 56
    assert len([op for op in retired if op["resource"] != "fleet"]) == 50
    assert not {op["operation_id"] for op in retired} & {
        op["operation_id"] for op in active.operations
    }
    with pytest.raises(ValueError, match="no replacement"):
        active.resolve("https://docs.cloud.f5.com/docs-v2/api/aws-vpc-site#Create")


def test_structural_sections_duplicates_nested_lists_tables_code_html_references() -> None:
    body = """# Guide

## Duplicate

Current paragraph; securemesh_site_v2 remains.

## Duplicate

Mixed paragraph with current text and securemesh_site.

- current item
- fleet
  - securemesh_site

| Current | Value |
| --- | --- |
| retained | 1 |
| securemesh_site | 2 |

```json
{"securemesh_site": {}}
```

<div><p>securemesh_site</p><p>current</p></div>

[old]: https://docs.cloud.f5.com/docs-v2/how-to/retired

## Current

Keep customer-managed TGW Connect and networking.
"""
    parsed = blocks(body)
    assert len({b.location for b in parsed if b.kind == "heading"}) == 4
    assert any(b.kind == "reference" for b in parsed)
    assert any(b.kind == "table_row" for b in parsed)
    obsolete = next(b for b in parsed if b.kind == "section" and "## Duplicate@2" in b.location)
    output = remove_blocks(body, [guard(obsolete)])
    assert "securemesh_site_v2" in output
    assert "customer-managed TGW Connect" in output
    assert '{"securemesh_site"' not in output
    assert "## Duplicate" in output
    with pytest.raises(ValueError, match="overlapping"):
        remove_blocks(body, [guard(obsolete), guard(next(b for b in parsed if b.kind == "fence"))])
    with pytest.raises(ValueError, match="stale"):
        remove_blocks(body.replace("Mixed", "Changed"), [guard(obsolete)])


def test_mixed_paragraph_is_removed_whole_and_empty_section_pruned(tmp_path: Path) -> None:
    body = "# Current\n\nKeep networking.\n\n## Obsolete\n\nCurrent and securemesh_site in one paragraph.\n"
    path = document(tmp_path, "mixed", body)
    block = next(b for b in blocks(body) if b.kind == "paragraph" and "securemesh_site" in b.text)
    active = policy(registry([decision("mixed", body, [guard(block)])]))
    before = path.read_bytes()
    review = curate_topics(tmp_path, policy=active, apply=False)
    assert path.read_bytes() == before
    assert review["counts"]["keep"] == 1
    curate_topics(tmp_path, policy=active)
    _, out = split_document(path.read_text())
    assert "Current and" not in out and "## Obsolete" not in out
    first = path.read_bytes()
    curate_topics(tmp_path, policy=active)
    assert path.read_bytes() == first


@pytest.mark.parametrize("mutation", ["changed", "unknown", "overlap"])
def test_changed_unknown_and_conflicting_rules_omit(tmp_path: Path, mutation: str) -> None:
    body = "# Guide\n\nKeep.\n\nfleet securemesh_site\n"
    block = next(b for b in blocks(body) if b.kind == "paragraph" and "fleet" in b.text)
    d = decision("mixed", body, [guard(block)])
    if mutation == "overlap":
        d["block_removals"].append(guard(block))
    document(tmp_path, "mixed", body + ("changed\n" if mutation == "changed" else ""))
    active = policy(registry([] if mutation == "unknown" else [d]))
    result = curate_topics(tmp_path, policy=active)
    assert result["counts"]["omit"] == 1
    assert not list(tmp_path.glob("content/*/**/index.md"))


def test_relative_fragment_reference_html_and_dependency_fixed_point(tmp_path: Path) -> None:
    active = policy()
    assert active.references(
        "[old](retired#step)\n[ref]: ./retired\n<a href='../how-to/retired'>old</a>",
        BASE + "guide",
        set(),
    )
    document(tmp_path, "retired", "# retired\n\nObsolete.")
    document(tmp_path, "dependent", "# Procedure\n\nPrerequisite [old](retired#setup).")
    document(tmp_path, "transitive", "# Procedure\n\nDepends on [procedure](dependent).")
    document(tmp_path, "current", "# Networking\n\nUse customer-managed AWS VPC and TGW Connect.")
    report = curate_topics(tmp_path, policy=active)
    assert report["counts"] == {"keep": 1, "remove": 1, "omit": 2}


def test_media_requires_exact_review_and_changed_bytes_omit(tmp_path: Path) -> None:
    body = "# Fleet context\n\nCurrent independent fleet of applications.\n\n![Diagram](assets/current.png)\n"
    path = document(tmp_path, "media", body)
    assets = path.parent / "assets"
    assets.mkdir()
    media = assets / "current.png"
    media.write_bytes(b"reviewed pixels")
    raw = registry(
        [decision("media", body)], [{"sha256": digest(media.read_bytes()), "disposition": "keep"}]
    )
    assert curate_topics(tmp_path, policy=policy(raw), apply=False)["counts"]["keep"] == 1
    media.write_bytes(b"changed pixels")
    assert curate_topics(tmp_path, policy=policy(raw))["counts"]["omit"] == 1
    assert not media.exists()


def test_remote_video_is_not_qualified_by_still_image(tmp_path: Path) -> None:
    body = '# fleet\n\n<iframe src="https://www.youtube.com/embed/example"></iframe>\n'
    path = document(tmp_path, "video", body)
    assert any(m["kind"] == "video" for m in media_inventory(path, body))
    result = curate_topics(tmp_path, policy=policy(registry([decision("video", body)])))
    assert result["counts"]["omit"] == 1


def test_evidence_conflicts_and_tampering_fail_closed(tmp_path: Path) -> None:
    raw = registry()
    evidence = tmp_path / "captured.json"
    evidence.write_bytes(b"captured")
    raw["topics"][0]["evidence_inputs"] = [{"path": evidence.name, "sha256": "0" * 64}]
    path = tmp_path / "policy.json"
    path.write_bytes(json_bytes(raw))
    with pytest.raises(ValueError, match="evidence"):
        load_curation_policy(path)
    d = decision("guide", "# guide\n\nbody\n")
    other = copy.deepcopy(d)
    other["output_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="conflicting"):
        policy(registry([d, other]))


def test_cli_archive_audit_separation_and_repeated_bytes(tmp_path: Path) -> None:
    document(
        tmp_path, "current", "# Current\n\nUse authentication and customer-managed VPC networking."
    )
    document(tmp_path, "retired", "# Retired\n\naws_vpc_site\n")
    runner = CliRunner()
    result = runner.invoke(app, ["curate-content", "--output", str(tmp_path)])
    assert result.exit_code == 0, result.exception
    paths = [
        *tmp_path.glob("content/*/**/index.md"),
        *(
            tmp_path / name
            for name in [
                "manifest.json",
                "quality-report.json",
                "quality-report.md",
                "SHA256SUMS",
                "html-to-markdown-content.tar.gz",
                "curation-audit.json",
            ]
        ),
    ]
    before = [p.read_bytes() for p in paths]
    result = runner.invoke(app, ["curate-content", "--output", str(tmp_path)])
    assert result.exit_code == 0, result.exception
    assert [p.read_bytes() for p in paths] == before

    with tarfile.open(tmp_path / "html-to-markdown-content.tar.gz") as archive:
        assert "curation-audit.json" not in archive.getnames()
    audit = json.loads((tmp_path / "curation-audit.json").read_text())
    assert audit["output_sha256"] == corpus_digest(tmp_path)
    assert "topics" in audit
    assert "content_migration" not in json.loads((tmp_path / "quality-report.json").read_text())
    validate_curation(tmp_path, artifacts=True)
    (tmp_path / "quality-report.json").write_text('{"content_migration": {}}')
    with pytest.raises(ValueError, match="leakage"):
        validate_curation(tmp_path, artifacts=True)


@pytest.mark.parametrize(
    "text",
    [
        "AppStack",
        "App Stack",
        "app-stack",
        "VoltStack",
        "Volt Stack",
        r"App\_Stack",
        "App%20Stack",
        "App&#32;Stack",
        r"App\-Stack",
        "Volt%53tack",
        "vesioschemaviewsvoltstack_siteapicreate",
    ],
)
def test_appstack_aliases_and_topic_isolation(text: str) -> None:
    active = load_curation_policy()
    assert active.appstack is not None and active.appstack.retired(text)
    assert not active.sms_candidate(text)


def test_appstack_removes_nested_item_and_keeps_mesh_procedure() -> None:
    appstack = load_curation_policy().appstack
    assert appstack is not None
    body = (
        "# Customer Edge networking\n\n"
        "Configure Mesh connectivity for an existing Kubernetes cluster.\n\n"
        "- Deploy the Mesh site.\n"
        "- Deploy the App Stack site.\n"
        "  - Attach the managed cluster.\n"
        "- Verify the Mesh tunnel.\n"
    )
    result = appstack.transform(body, BASE + "new-mesh-guide")
    assert not result.omit
    assert "Deploy the Mesh site" in result.body
    assert "Verify the Mesh tunnel" in result.body
    assert "managed cluster" not in result.body
    assert "App Stack" not in result.body


def test_appstack_rechecks_changed_inputs_and_retired_links() -> None:
    appstack = load_curation_policy().appstack
    assert appstack is not None
    body = (
        "# Mesh routes\n\n"
        "Keep independent Mesh routing instructions.\n\n"
        "[Old deployment](../site-management/create-app-stack-site)\n\n"
        "Verify the current site route.\n"
    )
    result = appstack.transform(body, BASE + "new-routes")
    assert not result.omit
    assert "Keep independent Mesh" in result.body
    assert "Verify the current site route" in result.body
    assert "create-app-stack-site" not in result.body
    assert appstack.excludes(
        "https://docs.cloud.f5.com/docs-v2/distributed-apps/how-to/app-mgnt/create-deploy-managed-k8s"
    )


def test_appstack_only_document_is_omitted() -> None:
    appstack = load_curation_policy().appstack
    assert appstack is not None
    result = appstack.transform("# AppStack setup\n\n1. Create the site.\n", BASE + "new")
    assert result.omit


def test_appstack_code_and_metadata_are_scanned(tmp_path: Path) -> None:
    active = load_curation_policy()
    assert active.appstack is not None
    body = "# Mesh routing\n\nKeep the existing tunnel.\n\n```sh\nappstack create site\n```\n"
    transformed = active.appstack.transform(body, BASE + "code-example")
    assert not transformed.omit
    assert "Keep the existing tunnel" in transformed.body
    assert "appstack create" not in transformed.body
    path = document(tmp_path, "metadata-example", "# Mesh routing\n\nCurrent tunnel.")
    metadata, current = split_document(path.read_text())
    metadata["title"] = "AppStack setup"
    path.write_text(serialize_document(PageMetadata.model_validate(metadata), current))
    with pytest.raises(ValueError, match="prohibited topic"):
        validate_curation(tmp_path)


def test_appstack_dependency_closure_removes_optional_link(tmp_path: Path) -> None:
    document(tmp_path, "obsolete-example", "# AppStack setup\n\nCreate a site.")
    current = document(
        tmp_path,
        "independent-example",
        "# Mesh routing\n\nKeep the existing Mesh tunnel.\n\n"
        "- [Old setup](obsolete-example)\n"
        "- Verify the Mesh tunnel.\n",
    )
    result = curate_topics(tmp_path)
    assert result["counts"]["keep"] == 1
    assert not (current.parent.parent / "obsolete-example/index.md").exists()
    _, body = split_document(current.read_text())
    assert "Old setup" not in body
    assert "Verify the Mesh tunnel" in body


def test_appstack_media_decisions_are_page_scoped() -> None:
    sha = "a" * 64
    raw = registry(media=[{"sha256": sha, "disposition": "remove"}])
    raw["topics"].append(
        {
            "id": "appstack-retired",
            "mode": "automatic",
            "retired_identities": [],
            "retired_urls": [],
            "retired_patterns": [r"\bapp[ -]?stack\b"],
            "candidate_detectors": [r"\bapp[ -]?stack\b"],
            "documents": [],
            "media": [{"sha256": sha, "disposition": "keep", "documents": [BASE + "current"]}],
            "evidence_inputs": [],
        }
    )
    active = policy(raw)
    assert active.media[sha]["disposition"] == "remove"
    assert active.appstack is not None
    assert active.appstack.media[sha]["disposition"] == "keep"
