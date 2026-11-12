"""Two-host staging, interrupted activation and stale commits remain publication blocking."""

import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_policy_activation import qualified_bundle

from html_to_markdown.curation import json_bytes
from html_to_markdown.policy_activation import (
    activate_bundle,
    confirm_cohort,
    require_active_cohort,
    stage_bundle,
)
from html_to_markdown.policy_transport import remote_activate, synchronize_policy


@pytest.mark.parametrize("already_active", [False, True])
def test_cross_host_activation_agrees_before_publication_and_recovers_sleep_resume(
    tmp_path, monkeypatch, already_active
):
    manifest, files = qualified_bundle()
    remote = tmp_path / "remote"
    local = tmp_path / "local"
    candidate = stage_bundle(remote, manifest, files)
    if already_active:
        activate_bundle(remote, candidate, expected_parent=None)
    monkeypatch.setattr("html_to_markdown.policy_transport.platform.system", lambda: "Darwin")
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if "export-policy-runtime" in args:
            value = {
                "state": "policy_already_active" if already_active else "policy_staged",
                "candidate": candidate,
                "manifest": manifest,
                "files": {name: base64.b64encode(data).decode() for name, data in files.items()},
            }
        elif "activate-policy-runtime" in args:
            assert json.loads(kwargs["input"]) == candidate
            value = activate_bundle(remote, candidate, expected_parent=None)
        elif "confirm-policy-runtime" in args:
            value = confirm_cohort(remote, json.loads(kwargs["input"]))
        else:
            raise AssertionError(args)
        return SimpleNamespace(stdout=json_bytes(value))

    monkeypatch.setattr("html_to_markdown.policy_transport.subprocess.run", run)
    result = synchronize_policy(
        Path("/transport"), "worker.invalid", "/cli", "/repo", str(remote), local, "/queue"
    )
    assert result["state"] == "policy_active"
    assert json.loads((remote / "active.json").read_bytes()) == json.loads(
        (local / "active.json").read_bytes()
    )
    require_active_cohort(remote)
    require_active_cohort(local)
    assert bool(any("activate-policy-runtime" in c for c in calls)) is not already_active
    assert all(
        "--stdin" in c
        for c in calls
        if "activate-policy-runtime" in c or "confirm-policy-runtime" in c
    )


def test_staged_policy_superseded_before_activation_does_not_replace_pointer(tmp_path, monkeypatch):
    manifest, files = qualified_bundle()
    candidate = stage_bundle(tmp_path, manifest, files)
    monkeypatch.setattr("html_to_markdown.policy_transport.protected_main", lambda _: "d" * 40)
    with pytest.raises(ValueError, match="superseded"):
        remote_activate(Path("/repo"), tmp_path, candidate)
    assert not (tmp_path / "active.json").exists()


def test_interrupted_between_host_activations_blocks_publication(tmp_path):
    manifest, files = qualified_bundle()
    candidate = stage_bundle(tmp_path, manifest, files)
    activate_bundle(tmp_path, candidate, expected_parent=None)
    with pytest.raises(ValueError, match="awaits confirmed"):
        require_active_cohort(tmp_path)
    with pytest.raises(ValueError, match="do not agree"):
        confirm_cohort(tmp_path, {**candidate, "policy_sha256": "e" * 64})
    assert not (tmp_path / "cohort.json").exists()
    confirm_cohort(tmp_path, candidate)
    require_active_cohort(tmp_path)
