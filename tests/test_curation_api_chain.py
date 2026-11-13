"""Exact API replay never accepts altered inputs, metadata or policy pins."""

import json
import shutil

import pytest
from test_curation_renewal_publication import prepared

from html_to_markdown.content_policy import migrate_content
from html_to_markdown.curation import curate_topics
from html_to_markdown.curation_renewal_policy import reviewed_policy
from html_to_markdown.models import PageMetadata
from html_to_markdown.render import serialize_document, split_document


def migrated(tmp_path):
    body = (
        "# Cloud services\n\nUse the [endpoint API]"
        "(https://docs.cloud.f5.com/docs-v2/docs/api/endpoint).\n\n"
        "Expect healthy origins within 30 seconds.\n"
    )
    output, source, _, _, _, _ = prepared(tmp_path, body)
    policy = reviewed_policy(output, [source])
    corpus = tmp_path / "corpus"
    inputs = output / source["binding_sha256"] / "inputs" / "articles"
    shutil.copytree(inputs, corpus)
    migrate_content(corpus, curation_policy=policy)
    article = next(corpus.glob("content/*/**/index.md"))
    record = next((corpus / ".curation-api-inputs").glob("*.json"))
    return corpus, article, record, policy


def test_private_intermediate_and_audit_repeat_byte_identically(tmp_path):
    corpus, _, record, policy = migrated(tmp_path)
    before = {p.relative_to(corpus): p.read_bytes() for p in corpus.rglob("*") if p.is_file()}
    assert record.stat().st_mode & 0o777 == 0o600
    migrate_content(corpus, curation_policy=policy)
    assert {
        p.relative_to(corpus): p.read_bytes() for p in corpus.rglob("*") if p.is_file()
    } == before


@pytest.mark.parametrize("fault", ["body", "metadata", "source", "policy", "catalog", "symlink"])
def test_changed_inputs_cannot_be_restored_over_current_content(tmp_path, fault):
    corpus, article, record, policy = migrated(tmp_path)
    if fault in {"body", "metadata"}:
        metadata, body = split_document(article.read_text())
        if fault == "body":
            body = body.replace("30 seconds", "60 seconds")
        else:
            metadata["title"] = "Cloud services (Legacy)"
        article.write_text(serialize_document(PageMetadata.model_validate(metadata), body))
    elif fault == "symlink":
        original = record.read_bytes()
        record.unlink()
        other = tmp_path / "other.json"
        other.write_bytes(original)
        record.symlink_to(other)
    else:
        data = json.loads(record.read_bytes())
        if fault == "source":
            data["metadata"]["description"] = "A changed source claim."
        elif fault == "policy":
            data["policy_sha256"] = "0" * 64
        else:
            data["api_catalog_sha256"] = "0" * 64
        record.write_text(json.dumps(data))
    current = article.read_bytes()
    with pytest.raises(ValueError):
        curate_topics(corpus, policy=policy)
    assert article.read_bytes() == current
