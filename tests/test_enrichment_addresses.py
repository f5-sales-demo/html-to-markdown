"""Coincident nested spans remain distinct while historical contracts replay unchanged."""

import json
from copy import deepcopy

import pytest
from test_enrichment_v2 import classified, header

from html_to_markdown.enrichment.analysis import inventory, sha
from html_to_markdown.enrichment.contracts import Artifact
from html_to_markdown.enrichment.contracts_v2 import ValidationV2
from html_to_markdown.enrichment.v2 import assemble, sections, validation_request, verify_validation
from html_to_markdown.models import PageMetadata
from html_to_markdown.render import serialize_document


@pytest.fixture
def nested_source(tmp_path):
    path = tmp_path / "content/my-f5-com/K000123456/index.md"
    path.parent.mkdir(parents=True)
    body = "# Debug\n\n1. 1. Collect debug info before downloading.\n   2. Download the archive.\n\n- - ```\n    curl --retry 3 --output debug.tar.bz2\n    ```\n"
    metadata = PageMetadata(
        sourceId="my-f5-com",
        title="Debug",
        slug="debug",
        url="https://my.f5.com/manage/s/article/K000123456",
        category="knowledge",
    )
    path.write_text(serialize_document(metadata, body))
    return path, tmp_path


def test_version_two_span_addresses_cover_distinct_and_identical_nested_ranges(nested_source):
    path, root = nested_source
    legacy = inventory(path, root)
    current = inventory(path, root, address_version=2)
    assert len({b["address"] for b in legacy["blocks"]}) < len(legacy["blocks"])
    assert len({b["address"] for b in current["blocks"]}) == len(current["blocks"])
    assert [b["text"] for b in current["blocks"]] == [b["text"] for b in legacy["blocks"]]
    assert [(b["start_line"], b["end_line"], b["sha256"]) for b in current["blocks"]] == [
        (b["start_line"], b["end_line"], b["sha256"]) for b in legacy["blocks"]
    ]
    groups = sections(current)
    decision, candidate = assemble(
        current,
        groups,
        classified(current, groups),
        [],
        header(current),
        {current["document"]: current},
    )
    assert candidate == current["body"]
    assert len(decision.classifications) == len(current["blocks"])
    assert inventory(path, root, address_version=2) == current
    assert inventory(path, root) == legacy


def test_duplicate_or_missing_nested_classifications_remain_rejected(nested_source):
    path, root = nested_source
    doc = inventory(path, root, address_version=2)
    groups = sections(doc)
    values = classified(doc, groups)
    values[0].classifications[-1] = values[0].classifications[0]
    with pytest.raises(ValueError, match="coverage"):
        assemble(doc, groups, values, [], header(doc), {doc["document"]: doc})


def test_old_artifacts_keep_exact_serialization_and_new_contract_is_explicit():
    raw = {
        "schema_version": 1,
        "prompt_version": "article-enrichment/1",
        "baseline_tag": "content-test",
        "publication_sha256": "a" * 64,
        "baseline_manifest_sha256": "b" * 64,
        "curated_sha256": "c" * 64,
        "analysis_sha256": "d" * 64,
        "policy_sha256": "e" * 64,
        "api_policy_sha256": "f" * 64,
        "curation_audit_sha256": None,
        "documents": [],
        "assets": {},
        "images": {},
        "image_groups": {},
        "unresolved_media": {},
        "repairs": {},
        "canonical_reviews": [],
        "pricing": {},
    }
    old = Artifact.model_validate(raw)
    assert old.structural_address_version == 1
    assert old.model_dump() == raw
    assert json.loads(old.model_dump_json()) == raw
    historical_v2 = {**raw, "schema_version": 2, "prompt_version": "article-enrichment/2"}
    assert Artifact.model_validate(historical_v2).model_dump() == historical_v2
    current = deepcopy(raw)
    current.update(
        schema_version=2, prompt_version="article-enrichment/2", structural_address_version=2
    )
    assert Artifact.model_validate(current).model_dump() == current
    assert sha(old.model_dump_json()) == sha(Artifact.model_validate(raw).model_dump_json())
    current["schema_version"] = 1
    with pytest.raises(ValueError, match="historical address contract"):
        Artifact.model_validate(current)


@pytest.mark.parametrize("failure", [None, "missing", "duplicate", "digest", "payload"])
def test_whole_document_validation_requires_every_nested_fact_and_exact_payload(
    nested_source, failure
):
    path, root = nested_source
    doc = inventory(path, root, address_version=2)
    groups = sections(doc)
    decision, body = assemble(
        doc, groups, classified(doc, groups), [], header(doc), {doc["document"]: doc}
    )
    request = validation_request(doc, body, decision, {}, [])
    payload = json.loads(request["input"][0]["content"][0]["text"])
    addresses = [b["address"] for b in payload["candidate_blocks"]]
    assert len(addresses) == len(set(addresses)) == len(doc["blocks"])
    raw = {
        "document": doc["document"],
        "input_sha256": doc["input_sha256"],
        "candidate_sha256": sha(body),
        "passed": True,
        "disposition_valid": True,
        "description_grounded": True,
        "coherent_standalone": True,
        "action_order_preserved": True,
        "all_necessary_facts_preserved": True,
        "media_valid": True,
        "classifications_complete": True,
        "privacy_valid": True,
        "approved_safe_privacy_findings": [],
        "unsupported_claims": [],
        "failures": [],
        "unresolved": [],
        "protected_facts": [
            {
                "source_address": b["address"],
                "source_sha256": b["sha256"],
                "candidate_addresses": [b["address"]],
                "candidate_sha256": [b["sha256"]],
                "necessary": True,
                "preserved": True,
                "kind": "fact",
            }
            for b in doc["blocks"]
        ],
    }
    if failure == "missing":
        raw["protected_facts"].pop()
    elif failure == "duplicate":
        raw["protected_facts"][-1] = raw["protected_facts"][0]
    elif failure == "digest":
        raw["protected_facts"][-1]["candidate_sha256"] = ["f" * 64]
    elif failure == "payload":
        body = body.replace("--retry 3", "--retry 2")
        raw["candidate_sha256"] = sha(body)
    validation = ValidationV2.model_validate(raw)
    if failure is None:
        assert verify_validation(doc, body, validation).passed is True
    else:
        with pytest.raises(ValueError, match="coverage|digest"):
            verify_validation(doc, body, validation)
