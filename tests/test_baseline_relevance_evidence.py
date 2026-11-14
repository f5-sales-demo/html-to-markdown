"""Historical inclusion claims are guarded context, never implicit admission approvals."""

import json

import pytest

from html_to_markdown.baseline_source_review import _relevance_evidence
from html_to_markdown.enrichment.analysis import sha


def test_prior_relevance_evidence_is_optional_exact_source_bound_and_private(tmp_path):
    article = "content/docs-cloud-f5-com/reference/api-ref/index.md"
    assert _relevance_evidence(tmp_path, article, "a" * 64, "b" * 64) == {}
    path = tmp_path / "relevance-evidence" / (sha(article) + ".json")
    path.parent.mkdir()
    evidence = {
        "baseline_article": article,
        "baseline_article_sha256": "a" * 64,
        "baseline_receipt_sha256": "b" * 64,
        "prior_necessary_facts": ["API navigation"],
    }
    path.write_text(json.dumps(evidence))
    assert _relevance_evidence(tmp_path, article, "a" * 64, "b" * 64) == evidence
    with pytest.raises(ValueError, match="different source"):
        _relevance_evidence(tmp_path, article, "c" * 64, "b" * 64)
    target = tmp_path / "evidence.json"
    target.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(target)
    with pytest.raises(ValueError, match="unsafe"):
        _relevance_evidence(tmp_path, article, "a" * 64, "b" * 64)
