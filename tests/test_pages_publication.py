import re
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]


def test_pages_content_has_one_human_route_and_engineering_docs_are_external() -> None:
    published = sorted(
        path.relative_to(ROOT / "docs") for path in (ROOT / "docs").rglob("*") if path.is_file()
    )
    assert published == [Path("index.mdx"), Path("llms-config.json")]
    llms_config = yaml.safe_load((ROOT / "docs/llms-config.json").read_text(encoding="utf-8"))
    assert llms_config == {
        "progressiveCorpus": {
            "taxonomy": {
                "levels": ["category", "subcategory"],
                "collapseSingletonSubcategories": True,
            },
            "hints": {"strategy": "first-sentence", "maxCharacters": 240},
        }
    }
    assert (ROOT / "engineering/architecture.md").is_file()
    assert (ROOT / "engineering/feature-requirements.md").is_file()
    assert (ROOT / "engineering/provenance.md").is_file()
    assert (ROOT / "engineering/quality-assessment.md").is_file()


def test_development_contract_requires_immutable_snapshot_inputs() -> None:
    text = (ROOT / "DEVELOPING.md").read_text(encoding="utf-8")
    for required in (
        "publication.json",
        "publication-sha256",
        "snapshot-tag",
        "snapshot-verifier-ref",
        "SHA256SUMS",
        "/_llms-txt/",
        "docs-builder@sha256:",
    ):
        assert required in text
    assert re.search(r"Never use .*latest.*content", text, re.IGNORECASE)
    assert re.search(r"Never use .*main.*content", text, re.IGNORECASE)


def test_pages_and_release_workflows_use_immutable_publication_contract() -> None:
    pages_path = ROOT / ".github/workflows/github-pages-deploy.yml"
    pages = yaml.safe_load(pages_path.read_text(encoding="utf-8"))
    dispatch_inputs = pages[True]["workflow_dispatch"]["inputs"]
    assert dispatch_inputs["snapshot-tag"]["required"] is True
    assert dispatch_inputs["publication-sha256"]["required"] is True
    assert set(pages[True]) == {"workflow_dispatch"}
    job = pages["jobs"]["docs"]
    workflow_pin = re.fullmatch(
        r"f5-sales-demo/docs-control/.github/workflows/github-pages-deploy.yml@[0-9a-f]{40}",
        job["uses"],
    )
    assert workflow_pin
    assert job["with"]["snapshot-verifier-ref"] == job["uses"].rsplit("@", 1)[1]
    assert re.fullmatch(
        r"ghcr.io/f5-sales-demo/docs-builder@sha256:[0-9a-f]{64}",
        job["with"]["builder-image"],
    )
    assert job["with"]["snapshot-tag"] == "${{ inputs.snapshot-tag }}"
    assert job["with"]["publication-sha256"] == "${{ inputs.publication-sha256 }}"

    content_release = (ROOT / ".github/workflows/release-content.yml").read_text(encoding="utf-8")
    assert 'gh release create "$release_tag" --draft --latest=false' in content_release
    assert content_release.count("gh workflow run github-pages-deploy.yml --ref main") == 1
    assert "f5-sales-demo/f5-sales-demo.github.io" not in content_release
    assert '"content-ref=$CONTENT_REF"' not in content_release
    assert '"snapshot-tag=$release_tag"' in content_release
    assert '"publication-sha256=$receipt_sha256"' in content_release
    assert "repository_dispatch" not in content_release
    assert "releases/latest" not in content_release


def test_software_version_and_release_are_semver_1_12_0() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    package = (ROOT / "src/html_to_markdown/__init__.py").read_text(encoding="utf-8")
    release = (ROOT / ".github/workflows/release-software.yml").read_text(encoding="utf-8")
    assert 'version = "1.12.0"' in pyproject
    assert '__version__ = "1.12.0"' in package
    assert "refs/tags/v" in release
    assert "uv build" in release
    assert "-name '*.whl' -o -name '*.tar.gz'" in release
    assert "gh release create" in release
    assert "--latest" in release
