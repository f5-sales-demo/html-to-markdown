"""Shared retired-menu masks retain one final image and all unrelated pixels."""

import io
from copy import deepcopy

import pytest
from PIL import Image
from test_curation_renewal import context, proposal

from html_to_markdown.curation_existing_validation import _qualified_compilation_failure
from html_to_markdown.curation_media import prepare_mask, replay_mask
from html_to_markdown.curation_renewal import Proposal, compile_proposal, proposal_context
from html_to_markdown.enrichment.analysis import sha


def source_and_proposal():
    image = Image.new("RGB", (16, 16), (0, 0, 255))
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    raw = stream.getvalue()
    digest = sha(raw)
    source = context()
    source = proposal_context(
        source["metadata"],
        source["body"] + "\n![API controls](assets/" + digest + ".png)\n",
        {digest: {}},
        source["source_files_sha256"],
        source["policy_sha256"],
    )
    value = proposal(source)
    for topic in value["topics"]:
        topic["media"] = [
            {
                "sha256": digest,
                "disposition": "keep",
                "reason": "Preserve API controls",
                "masks": [],
            }
        ]
    first = {
        "frame": 0,
        "rectangle": [1, 1, 4, 4],
        "color": [255, 255, 255],
        "reason": "Retired Azure VNet menu",
    }
    second = {
        "frame": 0,
        "rectangle": [10, 1, 13, 4],
        "color": [255, 255, 255],
        "reason": "Retired AppStack menu",
    }
    value["topics"][0]["media"][0]["masks"] = [first]
    value["topics"][2]["media"][0]["masks"] = [second]
    return source, value, raw, digest, first, second


def test_shared_masks_compile_once_with_one_final_asset_and_unchanged_proposal():
    source, value, raw, digest, first, second = source_and_proposal()
    original = deepcopy(value)
    result = compile_proposal(source, value, {digest: raw})
    assert value == original and result["proposal"] == original
    assert result == compile_proposal(source, value, {digest: raw})
    assert len(result["media_transforms"]) == 1
    transform = result["media_transforms"][digest]
    expected, specification = prepare_mask(raw, [first, second])
    assert transform["specification"] == specification
    assert replay_mask(raw, transform["specification"]) == expected
    final = transform["output_sha256"]
    assert final + ".png" in result["body"] and digest + ".png" not in result["body"]
    for stage in result["stages"]:
        assert any(m.get("output_sha256", m["sha256"]) == final for m in stage["media"])
    with Image.open(io.BytesIO(expected)) as pixels:
        assert pixels.getpixel((2, 2)) == (255, 255, 255)
        assert pixels.getpixel((11, 2)) == (255, 255, 255)
        assert pixels.getpixel((7, 7)) == (0, 0, 255)
    assert "Use port 8443" in result["body"]
    assert result["independent_validation_required"] is True


def test_single_mask_preserves_the_existing_transformation_contract():
    source, value, raw, digest, first, _ = source_and_proposal()
    value["topics"][2]["media"][0]["masks"] = []
    result = compile_proposal(source, value, {digest: raw})
    _, expected = prepare_mask(raw, [first])
    assert result["media_transforms"][digest]["specification"] == expected
    assert result["media_transforms"][digest]["topic"] == "legacy-retired"


@pytest.mark.parametrize(
    "kind", ["unsupported", "remove_with_mask", "conflicting_disposition", "overlap"]
)
def test_shared_mask_conflicts_remain_rejected(kind):
    source, value, raw, digest, _, second = source_and_proposal()
    if kind == "unsupported":
        value["topics"][1]["media"][0]["masks"] = [second]
    elif kind == "remove_with_mask":
        value["topics"][2]["media"][0]["disposition"] = "remove"
    elif kind == "conflicting_disposition":
        value["topics"][3]["media"][0]["disposition"] = "remove"
    else:
        value["topics"][2]["media"][0]["masks"][0]["rectangle"] = [2, 2, 5, 5]
    with pytest.raises(ValueError):
        compile_proposal(source, value, {digest: raw})


def test_only_proved_shared_mask_failure_qualifies_for_existing_proposal_validation():
    _, value, _, _, _, _ = source_and_proposal()
    failure = {"error": "retirement masks need one current AppStack or Legacy keep placement"}
    assert _qualified_compilation_failure(Proposal.model_validate(value), failure)
    assert not _qualified_compilation_failure(
        Proposal.model_validate(value), {"error": "model refusal"}
    )
    value["topics"][2]["media"][0]["masks"] = []
    assert not _qualified_compilation_failure(Proposal.model_validate(value), failure)
