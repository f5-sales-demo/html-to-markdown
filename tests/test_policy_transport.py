"""Two-host staging, interrupted activation and stale commits remain publication blocking."""

import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_policy_activation import qualified_bundle

from html_to_markdown import policy_transport
from html_to_markdown.curation import json_bytes
from html_to_markdown.policy_activation import (
    activate_bundle,
    confirm_cohort,
    require_active_cohort,
    stage_bundle,
)
from html_to_markdown.policy_transport import (
    export_policy,
    remote_activate,
    remote_confirm,
    synchronize_policy,
)


@pytest.fixture
def merged_tree(monkeypatch):
    package = Path(__file__).parents[1] / "src/html_to_markdown"
    prefix = "src/html_to_markdown/"

    def git(_, operation, *args):
        if operation == "show":
            path = args[0].split(":", 1)[1]
            assert path.startswith(prefix)
            return (package / path.removeprefix(prefix)).read_bytes()
        assert operation == "ls-tree"
        return "\n".join(
            prefix + p.relative_to(package).as_posix() for p in package.rglob("*.py")
        ).encode()

    monkeypatch.setattr("html_to_markdown.policy_transport._git", git)
    monkeypatch.setattr("html_to_markdown.policy_transport.protected_main", lambda _: "c" * 40)


def test_same_policy_export_still_requires_compatible_installed_code(
    tmp_path, monkeypatch, merged_tree
):
    monkeypatch.setattr("html_to_markdown.policy_transport.controller_digest", lambda: "e" * 64)
    with pytest.raises(ValueError, match="compatible software"):
        export_policy(Path("/repo"), tmp_path)


def test_export_detects_protected_main_change_during_tree_read(tmp_path, monkeypatch, merged_tree):
    commits = iter(["c" * 40, "d" * 40])
    monkeypatch.setattr("html_to_markdown.policy_transport.protected_main", lambda _: next(commits))
    with pytest.raises(ValueError, match="changed while reading"):
        export_policy(Path("/repo"), tmp_path)
    assert not (tmp_path / "active.json").exists()


def test_unchanged_code_and_curation_do_not_hide_a_changed_api_policy(
    tmp_path, monkeypatch, merged_tree
):
    original = policy_transport._git  # pylint: disable=protected-access

    def git(repository, operation, *args):
        if operation == "show" and args[0].endswith(":src/html_to_markdown/content_policy.json"):
            return b"changed-api-policy"
        return original(repository, operation, *args)

    monkeypatch.setattr(policy_transport, "_git", git)
    with pytest.raises(ValueError, match="API policy.*compatible software"):
        export_policy(Path("/repo"), tmp_path)


def test_reactivating_identical_verified_bundle_preserves_current_cohort(tmp_path):
    manifest, files = qualified_bundle()
    candidate = stage_bundle(tmp_path, manifest, files)
    activate_bundle(tmp_path, candidate, expected_parent=None)
    confirm_cohort(tmp_path, candidate)
    activate_bundle(tmp_path, candidate, expected_parent=candidate["bundle_sha256"])
    require_active_cohort(tmp_path)


def test_new_activation_invalidates_old_cohort_even_when_returning_to_an_old_bundle(tmp_path):
    manifest, files = qualified_bundle()
    first = stage_bundle(tmp_path, manifest, files)
    activate_bundle(tmp_path, first, expected_parent=None)
    confirm_cohort(tmp_path, first)
    second = stage_bundle(tmp_path, {**manifest, "source_commit": "d" * 40}, files)
    activate_bundle(tmp_path, second, expected_parent=first["bundle_sha256"])
    assert not (tmp_path / "cohort.json").exists()
    activate_bundle(tmp_path, first, expected_parent=second["bundle_sha256"])
    with pytest.raises(ValueError, match="awaits confirmed"):
        require_active_cohort(tmp_path)


def test_confirmation_rechecks_current_protected_policy_before_cohort_write(tmp_path, monkeypatch):
    manifest, files = qualified_bundle()
    candidate = stage_bundle(tmp_path, manifest, files)
    activate_bundle(tmp_path, candidate, expected_parent=None)

    def superseded(*_):
        raise ValueError("merged policy was superseded")

    monkeypatch.setattr("html_to_markdown.policy_transport.require_current_policy", superseded)
    with pytest.raises(ValueError, match="superseded"):
        remote_confirm(Path("/repo"), tmp_path, candidate)
    assert not (tmp_path / "cohort.json").exists()


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
