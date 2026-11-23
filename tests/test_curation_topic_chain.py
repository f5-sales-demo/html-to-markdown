"""Original retirement guards must survive later qualified topic migrations."""

import json
import shutil

import pytest
import yaml
from test_curation_renewal_publication import prepared

from html_to_markdown.content_policy import migrate_content
from html_to_markdown.curation import curation_validation_errors
from html_to_markdown.curation_renewal_policy import reviewed_policy
from html_to_markdown.render import split_document


@pytest.mark.parametrize("with_api", [False, True])
def test_original_legacy_guard_survives_later_terraform_migration(tmp_path, with_api):
    console = (
        "https://github.com/f5devcentral/f5-xc-terraform-examples/blob/main/"
        "workflow-guides/api-security/f5-xc-apisec-on-re/README.md"
    )
    body = (
        "# Cloud connectivity\n\n"
        "Independent cloud routes connect application servers to healthy origin pools. "
        "Configure the origin pool first, then configure the application route. "
        "Expect healthy origins within 30 seconds.\n\n"
        f"Use the [console guide]({console}) to configure application API protection.\n"
    )
    if with_api:
        body += "\nUse the [endpoint API](https://docs.cloud.f5.com/docs-v2/docs/api/endpoint).\n"
    output, source, _, _, _, _ = prepared(tmp_path, body)
    policy = reviewed_policy(output, [source])
    corpus = tmp_path / "corpus"
    shutil.copytree(output / source["binding_sha256"] / "inputs" / "articles", corpus)
    migrate_content(corpus, curation_policy=policy)
    article = next(corpus.glob("content/*/**/index.md"))
    _, final = split_document(article.read_text())
    assert console not in final
    assert "console.md" in final
    assert "Configure the origin pool first, then configure the application route." in final
    assert "Expect healthy origins within 30 seconds." in final
    assert not curation_validation_errors(corpus, policy)


def chain_corpus(tmp_path):
    console = (
        "https://github.com/f5devcentral/f5-xc-terraform-examples/blob/main/"
        "workflow-guides/api-security/f5-xc-apisec-on-re/README.md"
    )
    body = (
        "# Cloud connectivity\n\n"
        "Independent cloud routes connect healthy application backends. "
        "Configure the origin pool before the application route. "
        "Expect healthy origins within 30 seconds.\n\n"
        f"Use the [console guide]({console}) for application protection.\n\n"
        "Use the [endpoint API](https://docs.cloud.f5.com/docs-v2/docs/api/endpoint).\n"
    )
    output, source, _, _, _, _ = prepared(tmp_path, body)
    policy = reviewed_policy(output, [source])
    corpus = tmp_path / "corpus"
    shutil.copytree(output / source["binding_sha256"] / "inputs" / "articles", corpus)
    migrate_content(corpus, curation_policy=policy)
    return corpus, next(corpus.glob("content/*/**/index.md")), policy


@pytest.mark.parametrize(
    "fault",
    [
        "original",
        "record",
        "registration",
        "missing_record",
        "missing_original",
        "extra_original",
        "body",
        "title",
        "category",
        "product",
        "description",
        "hash",
        "last_updated",
        "related_documents",
    ],
)
def test_current_stage_chain_rejects_tampered_inputs_outputs_and_metadata(tmp_path, fault):
    corpus, article, policy = chain_corpus(tmp_path)
    directory = corpus / ".curation-topic-inputs"
    record = directory / "record.json"
    data = json.loads(record.read_bytes())
    original = directory / data["identity"] / "source" / article.relative_to(corpus)
    if fault == "original":
        original.write_text(original.read_text().replace("30 seconds", "60 seconds"))
    elif fault == "record":
        data["state_context"]["links"] = {"https://example.com": ["https://other.example.com"]}
        record.write_text(json.dumps(data))
    elif fault in {"registration", "missing_record", "missing_original"}:
        target = {
            "registration": directory / "registration.json",
            "missing_record": record,
            "missing_original": original,
        }[fault]
        target.unlink()
    elif fault == "extra_original":
        original.parent.joinpath("extra.md").write_text("unregistered source")
    else:
        metadata, body = split_document(article.read_text())
        changes = {
            "title": "Changed cloud title",
            "category": "changed-category",
            "product": "Changed product",
            "description": "Ungrounded source claim.",
            "hash": "0" * 64,
            "last_updated": "2099-01-01T00:00:00Z",
            "related_documents": [
                {"canonical_url": "https://example.com/invented", "title": "Invented"}
            ],
        }
        if fault == "body":
            body = body.replace("30 seconds", "60 seconds")
        else:
            metadata["content_hash" if fault == "hash" else fault] = changes[fault]
        article.write_text("---\n" + yaml.safe_dump(metadata, sort_keys=False) + "---\n\n" + body)
    current = article.read_bytes()
    assert curation_validation_errors(corpus, policy)
    assert article.read_bytes() == current


def test_repeated_complete_stage_migration_preserves_all_bytes(tmp_path):
    corpus, _, policy = chain_corpus(tmp_path)
    original = {p.relative_to(corpus): p.read_bytes() for p in corpus.rglob("*") if p.is_file()}
    migrate_content(corpus, curation_policy=policy)
    assert {
        p.relative_to(corpus): p.read_bytes() for p in corpus.rglob("*") if p.is_file()
    } == original
