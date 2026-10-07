"""Whole-document guards, dependency isolation and snapshot replay."""

import copy
from pathlib import Path

import pytest
from typer.testing import CliRunner

from html_to_markdown.cli import app
from html_to_markdown.curation import (
    CurationPolicy,
    curate_topics,
    digest,
    json_bytes,
    load_curation_policy,
)
from html_to_markdown.models import PageMetadata
from html_to_markdown.render import serialize_document, split_document
from html_to_markdown.state import StateStore

BASE = "https://docs.cloud.f5.com/docs-v2/platform/how-to/"


def document(root: Path, name: str, body: str, **metadata: object) -> Path:
    path = root / "content/docs-cloud-f5-com/platform/how-to" / name / "index.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        serialize_document(
            PageMetadata.model_validate(
                {
                    "sourceId": "docs-cloud-f5-com",
                    "url": BASE + name,
                    "title": "Independent guide",
                    "slug": name,
                    "category": "Guides",
                    **metadata,
                }
            ),
            body,
        )
    )
    return path


@pytest.mark.parametrize(
    "value",
    [
        "vesctl",
        "VeScTl",
        "%76%65%73%63%74%6c",
        "%2576esctl",
        "v&#101;sctl",
        "v&amp;#101;sctl",
        r"\u0076esctl",
        r"v\x65sctl",
        r"ves\ctl",
    ],
)
@pytest.mark.parametrize(
    "location", ["body", "title", "description", "tags", "code", "link", "caption"]
)
def test_original_mentions_remove_whole_page(tmp_path: Path, value: str, location: str) -> None:
    body = "# Guide\n\nKeep an independent networking explanation.\n"
    metadata = {}
    if location in {"title", "description"}:
        metadata[location] = value
    elif location == "tags":
        metadata["tags"] = [value]
    elif location == "code":
        body += f"\n```sh\n{value}\n```\n"
    elif location == "link":
        body += f"\n[tool](https://example.org/{value})\n"
    elif location == "caption":
        body += f"\n![{value}](assets/picture.png)\n"
    else:
        body += value + "\n"
    path = document(tmp_path, "new", body, **metadata)
    audit = curate_topics(tmp_path)
    assert not path.exists()
    assert audit["counts"] == {"keep": 0, "remove": 1, "omit": 0}
    stage = next(s for s in audit["documents"][0]["stages"] if s["topic"] == "vesctl-retired")
    assert stage["output_sha256"] is None
    assert stage["original_sha256"]


def test_original_guard_precedes_appstack_erasure(tmp_path: Path) -> None:
    path = document(
        tmp_path, "mixed", "# Guide\n\nIndependent network.\n\n- AppStack uses vesctl.\n"
    )
    assert (
        load_curation_policy()
        .appstack.transform(split_document(path.read_text())[1], BASE + "mixed")
        .body.find("vesctl")
        == -1
    )
    assert curate_topics(tmp_path)["counts"]["remove"] == 1


def test_new_dependency_is_omitted_without_transitive_block_deletion(tmp_path: Path) -> None:
    retired = "https://www.f5.com/products/distributed-cloud-services/distributed-cloud-waf"
    path = document(tmp_path, "changed", f"# Procedure\n\nFollow [required setup]({retired}).\n")
    unrelated = document(tmp_path, "unrelated", "# Network\n\nKeep subnet routes.\n")
    audit = curate_topics(tmp_path)
    assert not path.exists() and unrelated.exists()
    assert audit["counts"] == {"keep": 1, "remove": 0, "omit": 1}


def test_reviewed_dependency_guards_and_reapplication(tmp_path: Path) -> None:
    raw = copy.deepcopy(load_curation_policy().raw)
    topic = next(t for t in raw["topics"] if t["id"] == "vesctl-retired")
    target = "https://www.f5.com/products/distributed-cloud-services/distributed-cloud-waf"
    before = f"# Guide\n\nKeep routes. See [product]({target}).\n"
    after = "# Guide\n\nKeep routes.\n"
    topic["documents"].append(
        {
            "url": BASE + "reviewed",
            "disposition": "keep",
            "input_sha256": digest(before.encode()),
            "output_sha256": digest(after.encode()),
            "replacements": [
                {"source": f" See [product]({target}).", "destination": "", "count": 1}
            ],
        }
    )
    policy = CurationPolicy(raw, digest(json_bytes(raw)))
    path = document(tmp_path, "reviewed", before)
    audit = curate_topics(tmp_path, policy=policy)
    assert split_document(path.read_text())[1] == after
    assert [s["topic"] for s in audit["documents"][0]["stages"]] == [
        "smsv2-current",
        "appstack-retired",
        "vesctl-retired",
        "terraform-provider-current",
    ]
    assert curate_topics(tmp_path, policy=policy)["counts"]["keep"] == 1
    path.write_text(
        serialize_document(
            PageMetadata.model_validate(split_document(path.read_text())[0]),
            before + "\nChanged step.\n",
        )
    )
    assert curate_topics(tmp_path, policy=policy)["counts"]["omit"] == 1


def test_derived_relationship_is_pruned_without_omitting_source(tmp_path: Path) -> None:
    path = document(
        tmp_path,
        "independent",
        "# Guide\n\nKeep useful steps.\n",
        related_documents=[
            {
                "relation": "configure",
                "title": "Vesctl",
                "canonical_url": BASE + "volt-automation/vesctl",
                "source_id": "docs-cloud-f5-com",
                "stable_path": "platform/how-to/volt-automation/vesctl",
            }
        ],
    )
    assert curate_topics(tmp_path)["counts"]["keep"] == 1
    assert split_document(path.read_text())[0]["related_documents"] == []


def test_offline_rechecks_carried_and_restored_content(tmp_path: Path) -> None:
    document(tmp_path, "keep", "# Guide\n\nKeep useful subnet routes.\n")
    path = document(tmp_path, "retire", "# Guide\n\nUse vesctl.\n")
    original = path.read_text()
    store = StateStore(tmp_path / "state.sqlite")
    store.close()
    runner = CliRunner()
    first = runner.invoke(app, ["curate-content", "--output", str(tmp_path)])
    assert first.exit_code == 0, first.output
    artifacts = {
        p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file() and p.suffix != ".sqlite"
    }
    assert runner.invoke(app, ["curate-content", "--output", str(tmp_path)]).exit_code == 0
    assert artifacts == {
        p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file() and p.suffix != ".sqlite"
    }
    path.write_text(original)
    assert runner.invoke(app, ["curate-content", "--output", str(tmp_path)]).exit_code == 0
    assert not path.exists()


def test_confirmed_image_reference_removes_entire_page(tmp_path: Path) -> None:
    raw = copy.deepcopy(load_curation_policy().raw)
    topic = next(t for t in raw["topics"] if t["id"] == "vesctl-retired")
    image = b"reviewed image containing retired commands"
    topic["media"].append({"sha256": digest(image), "disposition": "remove"})
    policy = CurationPolicy(raw, digest(json_bytes(raw)))
    path = document(
        tmp_path, "image-only", "# Guide\n\nIndependent text.\n\n![Commands](assets/commands.png)\n"
    )
    (path.parent / "assets").mkdir()
    (path.parent / "assets/commands.png").write_bytes(image)
    assert curate_topics(tmp_path, policy=policy)["counts"]["remove"] == 1
    assert not path.exists() and not (path.parent / "assets/commands.png").exists()


def test_unknown_dependency_is_checked_before_appstack_removal(tmp_path: Path) -> None:
    target = "https://www.f5.com/products/distributed-cloud-services/distributed-cloud-waf"
    path = document(
        tmp_path,
        "required",
        f"# Guide\n\nKeep routes.\n\n- AppStack requires [product]({target}).\n",
    )
    assert curate_topics(tmp_path)["counts"]["omit"] == 1
    assert not path.exists()
