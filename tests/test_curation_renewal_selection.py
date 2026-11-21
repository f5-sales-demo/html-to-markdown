"""A compiler upgrade cannot create a fresh allowance for an unchanged source."""

import copy
import json

import pytest
import test_curation_structural_projection as fixtures

from html_to_markdown.curation import json_bytes
from html_to_markdown.curation_renewal_selection import (
    ExistingRenewalRequired,
    select_renewal_context,
)
from html_to_markdown.enrichment.analysis import sha


def preserve(tmp_path, context, *, controller=None, origin=None, state=None):
    saved = copy.deepcopy(context)
    if controller:
        saved["controller_sha256"] = controller
    if origin:
        saved["evidence"]["selected_existing_proposal"] = origin
    saved["binding_sha256"] = sha(
        json_bytes({k: v for k, v in saved.items() if k != "binding_sha256"})
    )
    directory = tmp_path / saved["binding_sha256"]
    directory.mkdir()
    (directory / "context.json").write_bytes(json_bytes(saved))
    if state is not None:
        (directory / "state.json").write_bytes(json_bytes(state))
    return saved, directory


def test_controller_upgrade_preserves_exhausted_previous_attempts(tmp_path):
    context, _ = fixtures.prepared("# Current\n\nKeep port 8443.\n")
    original, directory = preserve(
        tmp_path,
        context,
        controller="d" * 64,
        state={"status": "queued", "attempt": 3, "retryable": False},
    )
    before = (directory / "state.json").read_bytes()
    with pytest.raises(ExistingRenewalRequired):
        select_renewal_context(context, tmp_path)
    assert (directory / "state.json").read_bytes() == before
    assert not (tmp_path / context["binding_sha256"]).exists()
    assert original["binding_sha256"] != context["binding_sha256"]


def test_selected_current_validation_is_adopted_without_resetting_attempts(tmp_path):
    context, _ = fixtures.prepared("# Current\n\nKeep port 8443.\n")
    original, _ = preserve(
        tmp_path, context, controller="d" * 64, state={"status": "queued", "attempt": 3}
    )
    origin = {
        "original_binding_sha256": original["binding_sha256"],
        "original_attempt": 2,
        "new_editor_proposals": 0,
    }
    selected, directory = preserve(
        tmp_path,
        context,
        origin=origin,
        state={
            "status": "independently_validated",
            "attempt": 3,
            "selected_existing_proposal": origin,
        },
    )
    (directory / "validated.json").write_bytes(json_bytes({"independently_validated": True}))
    assert select_renewal_context(context, tmp_path) == selected
    assert json.loads((directory / "state.json").read_bytes())["attempt"] == 3


@pytest.mark.parametrize(
    "field", ["body", "source_files_sha256", "policy_sha256", "metadata", "media"]
)
def test_changed_source_or_policy_cannot_reuse_a_selected_validation(tmp_path, field):
    context, _ = fixtures.prepared("# Current\n\nKeep port 8443.\n")
    preserve(tmp_path, context, controller="d" * 64, state={"status": "queued", "attempt": 3})
    current = copy.deepcopy(context)
    current[field] = {"changed": True} if isinstance(current[field], dict) else "changed"
    assert select_renewal_context(current, tmp_path) == current


def test_current_unchanged_context_keeps_its_existing_ledger(tmp_path):
    context, _ = fixtures.prepared("# Current\n\nKeep port 8443.\n")
    preserve(tmp_path, context, state={"status": "queued", "attempt": 3})
    assert select_renewal_context(context, tmp_path) == context


def test_stale_selected_validation_does_not_grant_a_fresh_allowance(tmp_path):
    context, _ = fixtures.prepared("# Current\n\nKeep port 8443.\n")
    origin = {"original_binding_sha256": "d" * 64, "original_attempt": 2, "new_editor_proposals": 0}
    _, directory = preserve(
        tmp_path,
        context,
        controller="e" * 64,
        origin=origin,
        state={
            "status": "independently_validated",
            "attempt": 3,
            "selected_existing_proposal": origin,
        },
    )
    (directory / "validated.json").write_bytes(json_bytes({"independently_validated": True}))
    with pytest.raises(ExistingRenewalRequired):
        select_renewal_context(context, tmp_path)
