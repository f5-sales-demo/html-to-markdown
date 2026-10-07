"""Integration cases for the automatic Terraform topic and topic ordering."""

import copy
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from html_to_markdown.cli import app
from html_to_markdown.content_policy import migrate_content
from html_to_markdown.curation import (
    CurationPolicy,
    blocks,
    curate_topics,
    digest,
    json_bytes,
    load_curation_policy,
    remove_blocks,
    validate_curation,
)
from html_to_markdown.models import PageMetadata, PageStatus
from html_to_markdown.reconcile import include_previous_urls, reconcile_previous
from html_to_markdown.render import serialize_document, split_document
from html_to_markdown.state import StateStore

BASE = "https://docs.cloud.f5.com/docs-v2/platform/how-to/"


def test_mixed_hcl_preserves_aws_and_quoted_interpolation() -> None:
    body = """# Terraform networking

An AWS VPC connects application instances to their own subnets and routes. This
network design can be managed independently by an AWS provider. Its route tables
and subnets remain useful even when the application delivery layer changes.

```not-hcl
resource "volterra_origin_pool" "old" {
  name = "${replace(var.name, "{", "") }"
}
resource "aws_vpc" "current" {
  cidr_block = "10.0.0.0/16"
}
```
"""
    active = load_curation_policy().terraform
    assert active is not None
    result = active.transform(body, BASE + "networking")
    assert not result.omit
    assert 'resource "aws_vpc" "current"' in result.body
    assert "volterra_origin_pool" not in result.body
    assert result.destinations == [
        "https://registry.terraform.io/providers/f5-sales-demo/xcsh/latest/docs/resources/origin_pool"
    ]
    assert active.transform(result.body, BASE + "networking").body == result.body


def test_malformed_fence_and_alias_leave_no_legacy_code() -> None:
    body = """# Provider discussion

Terraform is used to describe infrastructure. The network explanation remains
useful after the old provider configuration is removed from this guide.

```json
provider "volterra" {
  alias = "legacy"
  api_p12_file = "cert.p12"

Other independent words explain the design and are not configuration steps.
"""
    active = load_curation_policy().terraform
    assert active is not None
    result = active.transform(body, BASE + "alias")
    assert 'provider "volterra"' not in result.body
    assert "api_p12_file" not in result.body


def test_required_provider_alias_removes_only_retired_alias() -> None:
    body = """# Terraform networking

The application uses an AWS VPC and an HTTP data source to prepare its
independent cloud network. These declarations remain useful after provider
specific delivery resources have been removed from the example.

```hcl
terraform {
  required_providers {
    volterrarm = { source = "volterraedge/volterra" }
    aws = { source = "hashicorp/aws" }
  }
}
provider "volterrarm" { api_p12_file = "cert.p12" }
resource "aws_vpc" "current" { cidr_block = "10.0.0.0/16" }
```
"""
    active = load_curation_policy().terraform
    assert active is not None
    result = active.transform(body, BASE + "aliased")
    assert 'source = "hashicorp/aws"' in result.body
    assert 'resource "aws_vpc"' in result.body
    assert "volterrarm" not in result.body
    assert "volterraedge/volterra" not in result.body


def test_relative_reference_table_and_html_links_close_dependencies() -> None:
    body = """# Terraform context

The retained content describes network connectivity and security outcomes for
applications across multiple clouds without depending on the old provider.

[Read old setup][old]

[old]: ./volt-automation/terraform

| Guide | Purpose |
| --- | --- |
| [old](https://registry.terraform.io/providers/volterraedge/volterra/latest/docs) | obsolete |
| AWS networking | independent |

<div><p><a href="https://registry.terraform.io/providers/volterraedge/volterra/latest/docs">old</a></p><p>Current console explanation.</p></div>
"""
    active = load_curation_policy().terraform
    assert active is not None
    result = active.transform(body, BASE + "guide")
    assert "[old]" not in result.body
    assert "AWS networking" in result.body
    assert "Current console explanation" in result.body
    assert "volterraedge/volterra" not in result.body
    assert active.transform(result.body, BASE + "guide").body == result.body


def test_nested_legacy_list_removes_parent_and_preserves_duplicate_sections() -> None:
    body = """# Terraform reference

AWS VPC routes and subnets remain useful for network planning independently of
the provider. They describe where backends live and how client traffic reaches
them, including health checks and regional failover paths.

## Examples

- AWS VPC example remains.
- Legacy setup
  - provider "volterra" { api_p12_file = "cert.p12" }

## Examples

The second example explains independent routing choices and service health
monitoring across clouds.
"""
    active = load_curation_policy().terraform
    assert active is not None
    result = active.transform(body, BASE + "nested")
    assert "Legacy setup" not in result.body
    assert 'provider "volterra"' not in result.body
    assert "AWS VPC example remains" in result.body
    assert result.body.count("## Examples") == 2


def test_current_provider_api_fields_and_cloud_examples_survive() -> None:
    body = """# API and infrastructure

The Volterra API service domain remains a valid endpoint. The API payload has
volterra_trusted_ca as a field and uses a certificate for authorization.

```yaml
volterra_trusted_ca: {}
url: https://example.console.ves.volterra.io/api
```

```terraform
resource "aws_vpc" "main" { cidr_block = "10.0.0.0/16" }
resource "azurerm_resource_group" "main" { name = "example" }
resource "google_compute_network" "main" { name = "example" }
resource "kubernetes_namespace" "main" { metadata { name = "example" } }
resource "helm_release" "main" { name = "example" }
resource "xcsh_origin_pool" "main" { name = "example" }
```
"""
    active = load_curation_policy().terraform
    assert active is not None
    assert active.transform(body, BASE + "api").body == body


def test_console_link_uses_pinned_guide_without_unrelated_provider_link() -> None:
    body = """# API security

The console workflow shows how to discover and protect application APIs on
regional edges. Teams can use this guide to configure the service and review
the resulting inventory and enforcement settings.

[Console guide](https://github.com/f5devcentral/f5-xc-terraform-examples/blob/main/workflow-guides/api-security/f5-xc-apisec-on-re/README.md)
"""
    active = load_curation_policy().terraform
    assert active is not None
    result = active.transform(body, BASE + "console")
    assert "console.md" in result.body
    assert "blob/main" not in result.body
    assert "registry.terraform.io/providers/f5-sales-demo/xcsh" not in result.body
    assert active.transform(result.body, BASE + "console").body == result.body


def test_equal_length_link_decisions_have_stable_audit_order() -> None:
    aws = "https://github.com/f5devcentral/f5-xc-terraform-examples/blob/main/workflow-guides/waf/f5-xc-waf-on-ce/aws/README.rst"
    gcp = "https://github.com/f5devcentral/f5-xc-terraform-examples/blob/main/workflow-guides/waf/f5-xc-waf-on-ce/gcp/README.rst"
    assert len(aws) == len(gcp)
    body = f"""# Console guides

These guides explain how to configure WAF protection through the console in
two independent cloud environments. Each guide has its own deployment steps
and does not require the retired provider.

[GCP guide]({gcp})

[AWS guide]({aws})
"""
    active = load_curation_policy().terraform
    assert active is not None
    result = active.transform(body, BASE + "console-guides")
    sources = [
        item["source"] for item in result.findings if item["reason"] == "replaced_linked_example"
    ]
    assert sources == [aws, gcp]


def _document(output: Path, body: str) -> Path:
    path = output / "content/docs-cloud-f5-com/platform/how-to/guide/index.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        serialize_document(
            PageMetadata(
                sourceId="docs-cloud-f5-com",
                url=BASE + "guide",
                canonical_url=BASE + "guide",
                title="Terraform network design",
                slug="guide",
                category="platform",
            ),
            body,
        )
    )
    return path


def test_changed_page_uses_same_topic_planner_and_repeated_artifacts(tmp_path: Path) -> None:
    body = """# Terraform network design

An origin pool routes requests to healthy application servers. The backend
networking design is useful without any particular provider configuration.
Operators can choose healthy upstream servers and review the traffic path
without using the retired Terraform resources.

```auto
resource "volterra_origin_pool" "old" { name = "old" }
```
"""
    path = _document(tmp_path, body)
    orphan = path.parent / "assets/orphan.png"
    orphan.parent.mkdir()
    orphan.write_bytes(b"unreferenced screenshot")
    original = path.read_bytes()
    runner = CliRunner()
    examination = runner.invoke(app, ["examine-content", "--output", str(tmp_path)])
    assert examination.exit_code == 0, examination.exception
    proposed = json.loads(examination.stdout)
    assert proposed["topic_inventory"]["documents"][0]["terraform"]["destinations"]
    assert proposed["counts"]["removed_assets"] == 1
    assert path.read_bytes() == original
    proposal = curate_topics(tmp_path, apply=False)
    assert path.read_bytes() == original
    assert proposal["documents"][0]["stages"][3]["topic"] == "terraform-provider-current"
    assert proposal["documents"][0]["terraform"]["removals"]
    result = runner.invoke(app, ["curate-content", "--output", str(tmp_path)])
    assert result.exit_code == 0, result.exception
    assert not orphan.exists()
    audit = json.loads((tmp_path / "curation-audit.json").read_text())
    assert audit["content_migration"]["counts"]["removed_assets"] == 1
    _, curated = split_document(path.read_text())
    assert "volterra_origin_pool" not in curated
    validate_curation(tmp_path, artifacts=True)
    paths = [
        path,
        *(
            tmp_path / name
            for name in (
                "manifest.json",
                "quality-report.json",
                "quality-report.md",
                "SHA256SUMS",
                "html-to-markdown-content.tar.gz",
                "curation-audit.json",
            )
        ),
    ]
    first = [item.read_bytes() for item in paths]
    result = runner.invoke(app, ["curate-content", "--output", str(tmp_path)])
    assert result.exit_code == 0, result.exception
    assert [item.read_bytes() for item in paths] == first
    metadata, curated = split_document(path.read_text())
    restored_legacy = (
        curated.rstrip()
        + '\n\n```hcl\nresource "volterra_origin_pool" "restored" { name = "old" }\n```\n'
    )
    path.write_text(serialize_document(PageMetadata.model_validate(metadata), restored_legacy))
    result = runner.invoke(app, ["curate-content", "--output", str(tmp_path)])
    assert result.exit_code == 0, result.exception
    assert "volterra_origin_pool" not in split_document(path.read_text())[1]
    # A changed explanation is still analyzed; the old body digest is not a gate.
    changed = body.replace("healthy application servers", "several healthy backend servers")
    automatic = load_curation_policy().terraform
    assert automatic is not None
    assert automatic.transform(changed, BASE + "guide").body != changed


def test_overlapping_topics_revalidate_final_body_without_audit(tmp_path: Path) -> None:
    body = """# Terraform fleet guide

An AWS VPC supplies application networking, routes, and subnets. Its cloud
infrastructure design remains useful even when the delivery provider changes.
The network addresses can be planned and reviewed independently.

legacy_site setup is obsolete.

```hcl
resource "volterra_origin_pool" "retired" { name = "old" }
```
"""
    old = next(
        block
        for block in blocks(body)
        if block.kind == "paragraph" and "legacy_site setup" in block.text
    )
    after_sms = remove_blocks(body, [{"location": old.location, "sha256": old.sha256}])
    reviewed = {
        "id": "smsv2-current",
        "retired_identities": [],
        "retired_urls": [],
        "retired_patterns": [r"\blegacy_site\b"],
        "candidate_detectors": [r"\bfleet\b"],
        "documents": [
            {
                "url": BASE + "guide",
                "disposition": "keep",
                "reason": "reviewed",
                "input_sha256": digest(body.encode()),
                "output_sha256": digest(after_sms.encode()),
                "block_removals": [{"location": old.location, "sha256": old.sha256}],
            }
        ],
        "media": [],
        "evidence_inputs": [],
    }
    automatic = load_curation_policy().topics[-1]
    raw = {"schema_version": 1, "topics": [reviewed, automatic]}
    active = CurationPolicy(raw, digest(json_bytes(raw)))
    path = _document(tmp_path, body)
    first = curate_topics(tmp_path, policy=active)
    assert first["counts"]["keep"] == 1
    assert (
        first["documents"][0]["stages"][0]["output_sha256"]
        == first["documents"][0]["stages"][1]["input_sha256"]
    )
    _, curated = split_document(path.read_text())
    assert "legacy_site" not in curated and "volterra_origin_pool" not in curated
    second = curate_topics(tmp_path, policy=active)
    assert second["counts"]["keep"] == 1
    assert split_document(path.read_text())[1] == curated


def test_carried_forward_legacy_page_is_curated_after_reconciliation(tmp_path: Path) -> None:
    previous = tmp_path / "previous"
    body = """# Terraform network design

An origin pool distributes traffic to healthy application backends. Its routing
and health concepts remain useful when the provider implementation changes.
Operators can reason about backend selection and failure handling independently.

```hcl
resource "volterra_origin_pool" "old" { name = "old" }
```
"""
    source = _document(previous, body)
    prior = previous / "manifest.json"
    prior.write_text(
        json.dumps(
            {
                "documents": [
                    {
                        "sourceId": "docs-cloud-f5-com",
                        "url": BASE + "guide",
                        "path": str(source.relative_to(previous)),
                    }
                ]
            }
        )
    )
    output = tmp_path / "output"
    store = StateStore(output / "state.sqlite")
    try:
        include_previous_urls(store, prior, {"docs-cloud-f5-com"})
        store.mark(BASE + "guide", PageStatus.FAILED, error=RuntimeError("timeout"))
        reconcile_previous(output, store, prior, retain_previous=True)
        restored = output / source.relative_to(previous)
        assert restored.is_file()
        assert store.rows()[0]["status"] == PageStatus.CARRIED_FORWARD
        migrate_content(output, store)
        _, curated = split_document(restored.read_text())
        assert "volterra_origin_pool" not in curated
        assert "healthy application backends" in curated
        assert "xcsh origin pool documentation" in curated
        assert store.rows()[0]["status"] == PageStatus.CARRIED_FORWARD
    finally:
        store.close()


def test_retained_media_digest_is_revalidated_after_curation(tmp_path: Path) -> None:
    body = """# Terraform network design

The architecture diagram explains how an AWS VPC connects private application
backends to an origin pool. The network paths and upstream health decisions
remain meaningful independently of the retired provider implementation.

![Current network diagram](assets/diagram.png)

```hcl
resource "volterra_origin_pool" "old" { name = "old" }
```
"""
    path = _document(tmp_path, body)
    asset = path.parent / "assets/diagram.png"
    asset.parent.mkdir()
    asset.write_bytes(b"reviewed diagram pixels")
    raw = copy.deepcopy(load_curation_policy().raw)
    raw["topics"][-1]["media"].append({"sha256": digest(asset.read_bytes()), "disposition": "keep"})
    active = CurationPolicy(raw, digest(json_bytes(raw)))
    assert curate_topics(tmp_path, policy=active)["counts"]["keep"] == 1
    validate_curation(tmp_path, policy=active)
    asset.write_bytes(b"changed diagram pixels")
    with pytest.raises(ValueError, match="unreviewed Terraform media"):
        validate_curation(tmp_path, policy=active)


def test_changed_reviewed_page_still_closes_structural_dependencies() -> None:
    body = """# Network architecture

An AWS VPC carries traffic between the application and backend servers. The
subnet layout and route choices remain useful without a provider-specific
configuration. Operators can review this architecture independently.

## Retired Terraform setup

Set the old credentials, then run the following provider configuration.

```hcl
resource "volterra_origin_pool" "old" { name = "old" }
```

Run terraform apply to deploy it.
"""
    raw = copy.deepcopy(load_curation_policy().raw)
    raw["topics"][-1]["profiles"][BASE + "changed"] = {
        "input_sha256": digest(body.encode()),
        "remove_sections": ["## Retired Terraform setup"],
    }
    active = CurationPolicy(raw, digest(json_bytes(raw))).terraform
    assert active is not None
    changed = body.replace("route choices", "independent route choices")
    result = active.transform(changed, BASE + "changed")
    assert "Retired Terraform setup" not in result.body
    assert "terraform apply" not in result.body
    assert "independent route choices" in result.body
