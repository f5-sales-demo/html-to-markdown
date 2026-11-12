"""Retained limitation notices need exact independent document and block guards."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from html_to_markdown.curation import (
    CurationPolicy,
    blocks,
    corpus_digest,
    curate_topics,
    digest,
    json_bytes,
    validate_curation,
)
from html_to_markdown.models import PageMetadata
from html_to_markdown.render import serialize_document, split_document

URL = "https://docs.cloud.f5.com/docs-v2/how-to/mesh-pods"
NOTE = (
    "> **Note:** The site as pods supports only Mesh functionalities and does not "
    "support F5® Distributed Cloud App Stack. The supported Kubernetes version is 1.23.\n"
)
BODY = "# Mesh networking\n\n" + NOTE + "\n1. Deploy the Mesh site.\n2. Verify the tunnel.\n"


def reviewed_policy(body: str = BODY, notice: str = NOTE) -> CurationPolicy:
    parsed = next(block for block in blocks(body) if block.text == notice)
    decision = {
        "url": URL,
        "disposition": "keep",
        "reason": "independently_validated_current_source",
        "input_sha256": digest(body.encode()),
        "output_sha256": digest(body.encode()),
        "block_removals": [],
        "review_index": None,
        "renewal_binding_sha256": "a" * 64,
        "renewal_validation_sha256": "b" * 64,
        "retained_notices": [{"location": parsed.location, "sha256": parsed.sha256}],
    }
    raw = {
        "schema_version": 1,
        "version": "1.0.0",
        "topics": [
            {
                "id": "appstack-retired",
                "mode": "automatic",
                "retired_identities": ["appstack_site"],
                "retired_urls": [URL + "/retired-site"],
                "retired_patterns": [r"\bapp[_ -]?stack\b", r"\bappstack_site\b"],
                "candidate_detectors": [],
                "documents": [decision],
                "media": [],
                "evidence_inputs": [],
            }
        ],
    }
    return CurationPolicy(raw, digest(json_bytes(raw)))


def article(root: Path, body: str = BODY) -> Path:
    path = root / "content/docs-cloud-f5-com/how-to/mesh-pods/index.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        serialize_document(
            PageMetadata(
                sourceId="docs-cloud-f5-com",
                url=URL,
                canonical_url=URL,
                title="Mesh networking",
                slug="mesh-pods",
                category="Guides",
            ),
            body,
        )
    )
    return path


def test_exact_reviewed_notice_keeps_source_bytes_and_version_limit() -> None:
    policy = reviewed_policy()
    assert policy.appstack is not None
    result = policy.appstack.transform(BODY, URL)
    assert not result.omit
    assert result.body == BODY
    assert policy.retired(NOTE)
    assert not policy.retired(BODY, url=URL)
    assert policy.retired(BODY, url=URL + "/other")


def test_application_validation_and_repeat_use_same_notice_guards(tmp_path: Path) -> None:
    path = article(tmp_path)
    policy = reviewed_policy()
    assert policy.appstack is not None
    first = curate_topics(tmp_path, policy=policy)
    assert first["counts"]["keep"] == 1
    assert split_document(path.read_text())[1] == BODY
    validate_curation(tmp_path, policy=policy)
    before = corpus_digest(tmp_path)
    second = curate_topics(tmp_path, policy=policy)
    assert corpus_digest(tmp_path) == before
    assert second == first


@pytest.mark.parametrize("fault", ["missing", "stale", "duplicate", "binding", "validation"])
def test_unqualified_or_stale_notice_guards_never_allow_retirement(fault: str) -> None:
    policy = reviewed_policy()
    assert policy.appstack is not None
    decision = policy.appstack.decisions[URL]
    if fault == "missing":
        decision.pop("retained_notices")
    elif fault == "stale":
        decision["retained_notices"][0]["sha256"] = "0" * 64
    elif fault == "duplicate":
        decision["retained_notices"] *= 2
    else:
        decision.pop("renewal_" + fault + "_sha256")
    assert policy.appstack.transform(BODY, URL).omit
    assert policy.retired(BODY, url=URL)


def test_changed_article_does_not_reuse_reviewed_notice() -> None:
    policy = reviewed_policy()
    assert policy.appstack is not None
    changed = BODY.replace("1.23", "1.24")
    assert policy.retired(changed, url=URL)
    result = policy.appstack.transform(changed, URL)
    assert NOTE not in result.body


@pytest.mark.parametrize(
    "addition",
    [
        "\nRun appstack create site.\n",
        "\nUse the appstack_site API.\n",
        "\n[Deploy](https://docs.cloud.f5.com/docs-v2/how-to/mesh-pods/retired-site)\n",
    ],
)
def test_notice_guard_does_not_cover_other_retired_material(addition: str) -> None:
    body = BODY + addition
    policy = reviewed_policy(body)
    assert policy.appstack is not None
    assert policy.appstack.transform(body, URL).omit


@pytest.mark.parametrize(
    "notice",
    [
        "> **Note:** Deploy App Stack, then configure Mesh. Kubernetes version 1.23.\n",
        "> **Note:** Does not support App Stack. Run `appstack create site`.\n",
        "> **Note:** Does not support [App Stack](https://example.com/retired).\n",
        "```sh\n# does not support App Stack\n```\n",
    ],
)
def test_guard_is_not_permission_for_a_procedure_link_or_code(notice: str) -> None:
    body = "# Mesh networking\n\n" + notice + "\nVerify the tunnel.\n"
    policy = reviewed_policy(body, notice)
    assert policy.appstack is not None
    assert policy.appstack.transform(body, URL).omit


def test_notice_candidates_are_hints_and_have_complete_structural_guards() -> None:
    policy = reviewed_policy()
    assert policy.appstack is not None
    candidates = policy.appstack.notice_rules(BODY)
    assert candidates == policy.appstack.decisions[URL]["retained_notices"]
    unreviewed = copy.deepcopy(policy.raw)
    unreviewed["topics"][0]["documents"] = []
    without_review = CurationPolicy(unreviewed, digest(json_bytes(unreviewed)))
    assert without_review.retired(BODY, url=URL)
    assert without_review.appstack is not None
    result = without_review.appstack.transform(BODY, URL)
    assert NOTE not in result.body


def test_final_validation_rejects_a_changed_notice(tmp_path: Path) -> None:
    article(tmp_path, BODY.replace("1.23", "1.24"))
    with pytest.raises(ValueError, match="AppStack"):
        validate_curation(tmp_path, policy=reviewed_policy())


def test_later_reviewed_output_needs_its_own_document_digest() -> None:
    policy = reviewed_policy()
    assert policy.appstack is not None
    final = BODY + "\nCheck the route status.\n"
    decision = policy.appstack.decisions[URL]
    assert policy.retired(final, url=URL)
    decision["accepted_output_sha256"] = [digest(final.encode())]
    result = policy.appstack.transform(final, URL)
    assert not result.omit
    assert result.body == final
    assert not policy.retired(final, url=URL)


def test_unguarded_metadata_remains_prohibited(tmp_path: Path) -> None:
    path = article(tmp_path)
    metadata, body = split_document(path.read_text())
    metadata["description"] = "App Stack deployment procedure."
    path.write_text(serialize_document(PageMetadata.model_validate(metadata), body))
    with pytest.raises(ValueError, match="prohibited topic"):
        validate_curation(tmp_path, policy=reviewed_policy())
