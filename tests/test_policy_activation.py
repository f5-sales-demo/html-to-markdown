"""Qualified policy activation is immutable, compatible and atomic."""

import json
from pathlib import Path

import pytest

from html_to_markdown.curation import json_bytes, load_curation_policy
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.policy_activation import (
    POLICY,
    RECEIPT,
    _policy_files,
    activate_bundle,
    active_policy_path,
    controller_digest,
    create_bundle,
    require_cohort,
    stage_bundle,
    verify_bundle,
)


def qualified_bundle():
    package = Path(__file__).parents[1] / "src/html_to_markdown"
    registry = (package / POLICY).read_bytes()
    files = {name: (package / name).read_bytes() for name in _policy_files(registry)}
    pin = load_curation_policy().sha256
    receipt = json_bytes(
        {
            "policy_sha256": pin,
            "post_planner_validated": True,
            "enforcement_findings": 0,
            "qualification_sha256": "a" * 64,
            "baseline_receipt_sha256": "b" * 64,
            "files": {POLICY: sha(registry)},
        }
    )
    return create_bundle(
        files,
        receipt,
        source_commit="c" * 40,
        expected_policy_sha256=pin,
        expected_controller_sha256=controller_digest(),
    )


def test_complete_qualified_bundle_activates_only_with_matching_parent(tmp_path):
    manifest, files = qualified_bundle()
    state = tmp_path / "policy"
    first = stage_bundle(state, manifest, files)
    assert stage_bundle(state, manifest, files) == first
    assert active_policy_path(state) is None
    activated = activate_bundle(state, first, expected_parent=None)
    assert active_policy_path(state) == state / "bundles" / first["bundle_sha256"] / POLICY
    assert verify_bundle(active_policy_path(state).parent, first["bundle_sha256"]) == manifest
    require_cohort(activated, first)
    pointer = (state / "active.json").read_bytes()
    with pytest.raises(ValueError, match="superseded"):
        activate_bundle(state, first, expected_parent=None)
    assert (state / "active.json").read_bytes() == pointer
    with pytest.raises(ValueError, match="do not agree"):
        require_cohort(activated, {**first, "source_commit": "d" * 40})


@pytest.mark.parametrize("change", ["catalog", "receipt", "extra", "symlink"])
def test_tampered_or_incomplete_active_bundle_fails_closed(tmp_path, change):
    manifest, files = qualified_bundle()
    state = tmp_path / "policy"
    candidate = stage_bundle(state, manifest, files)
    activate_bundle(state, candidate, expected_parent=None)
    root = state / "bundles" / candidate["bundle_sha256"]
    if change == "catalog":
        (root / POLICY).write_text("{}")
    elif change == "receipt":
        (root / RECEIPT).write_text("{}")
    elif change == "extra":
        (root / "raw-response.json").write_text("private")
    else:
        (root / POLICY).unlink()
        (root / POLICY).symlink_to(tmp_path / "other")
    with pytest.raises((ValueError, KeyError)):
        active_policy_path(state)


def test_incompatible_software_and_unqualified_policy_never_replace_activation(tmp_path):
    manifest, files = qualified_bundle()
    state = tmp_path / "policy"
    candidate = stage_bundle(state, manifest, files)
    activate_bundle(state, candidate, expected_parent=None)
    root = state / "bundles" / candidate["bundle_sha256"]
    with pytest.raises(ValueError, match="different installed software"):
        verify_bundle(root, candidate["bundle_sha256"], expected_controller="f" * 64)
    invalid = json.loads(files[RECEIPT])
    invalid["enforcement_findings"] = 1
    with pytest.raises(ValueError, match="zero-enforcement"):
        create_bundle(
            {k: v for k, v in files.items() if k != RECEIPT},
            json_bytes(invalid),
            source_commit="c" * 40,
            expected_policy_sha256=manifest["policy_sha256"],
            expected_controller_sha256=manifest["controller_sha256"],
        )
    assert active_policy_path(state) == root / POLICY
