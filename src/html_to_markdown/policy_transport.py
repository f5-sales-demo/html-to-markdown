"""Protected-main policy export and authenticated cross-host activation."""

from __future__ import annotations

import base64
import fcntl
import json
import platform
import subprocess  # nosec B404
from pathlib import Path
from typing import Any

from .curation import json_bytes, load_curation_policy
from .policy_activation import (
    POLICY,
    RECEIPT,
    _policy_files,
    activate_bundle,
    confirm_cohort,
    controller_digest,
    create_bundle,
    stage_bundle,
)

REPOSITORY = "f5-sales-demo/html-to-markdown"


def _git(repository: Path, *args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=repository, capture_output=True, check=True).stdout  # nosec B603


def protected_main(repository: Path) -> str:
    if platform.system() != "Linux":
        raise ValueError("protected policy export runs on Ubuntu")
    origin = _git(repository, "remote", "get-url", "origin").decode().strip()
    if origin not in {
        "https://github.com/f5-sales-demo/html-to-markdown.git",
        "git@github.com:f5-sales-demo/html-to-markdown.git",
    }:
        raise ValueError("policy export repository identity mismatch")
    _git(repository, "fetch", "--prune")
    commit = _git(repository, "rev-parse", "origin/main").decode().strip()
    response = subprocess.run(
        ["gh", "api", "repos/" + REPOSITORY + "/branches/main"], capture_output=True, check=True
    ).stdout  # nosec B603
    branch = json.loads(response)
    if not branch.get("protected") or branch["commit"]["sha"] != commit:
        raise ValueError("policy export is not the current protected-main commit")
    return commit


def export_policy(repository: Path, state_root: Path) -> dict[str, Any]:
    commit = protected_main(repository)
    prefix = "src/html_to_markdown/"
    registry = _git(repository, "show", commit + ":" + prefix + POLICY)
    files = {
        name: _git(repository, "show", commit + ":" + prefix + name)
        for name in _policy_files(registry)
    }
    from .enrichment.analysis import sha  # pylint: disable=import-outside-toplevel

    linked = bytearray(registry)
    for topic in json.loads(registry)["topics"]:
        if catalog := topic.get("review_catalog"):
            linked.extend(files[catalog["path"]])
    policy_pin = sha(bytes(linked))
    if policy_pin == load_curation_policy().sha256:
        pointer = state_root / "active.json"
        if not pointer.exists():
            return {"state": "policy_current", "source_commit": commit, "policy_sha256": policy_pin}
        candidate = json.loads(pointer.read_bytes())
        root = state_root / "bundles" / candidate["bundle_sha256"]
        manifest = json.loads((root / "manifest.json").read_bytes())
        from .policy_activation import verify_bundle  # pylint: disable=import-outside-toplevel

        verify_bundle(root, candidate["bundle_sha256"])
        return {
            "state": "policy_already_active",
            "candidate": candidate,
            "manifest": manifest,
            "files": {
                name: base64.b64encode((root / name).read_bytes()).decode()
                for name in manifest["files"]
            },
        }
    names = _git(repository, "ls-tree", "-r", "--name-only", commit, prefix).decode().splitlines()
    code = {
        name.removeprefix(prefix): sha(_git(repository, "show", commit + ":" + name))
        for name in names
        if name.endswith(".py")
    }
    if sha(json_bytes(code)) != controller_digest():
        raise ValueError("merged policy requires a compatible software release before activation")
    qualification = _git(repository, "show", commit + ":engineering/" + RECEIPT)
    manifest, payload = create_bundle(
        files,
        qualification,
        source_commit=commit,
        expected_policy_sha256=policy_pin,
        expected_controller_sha256=controller_digest(),
    )
    candidate = stage_bundle(state_root, manifest, payload)
    return {
        "state": "policy_staged",
        "candidate": candidate,
        "manifest": manifest,
        "files": {name: base64.b64encode(data).decode() for name, data in payload.items()},
    }


def remote_activate(
    repository: Path, state_root: Path, candidate: dict[str, Any]
) -> dict[str, Any]:
    state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (state_root / "activation.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if protected_main(repository) != candidate["source_commit"]:
            raise ValueError("staged policy commit was superseded")
        pointer = state_root / "active.json"
        previous = json.loads(pointer.read_bytes()) if pointer.exists() else {}
        return activate_bundle(state_root, candidate, expected_parent=previous.get("bundle_sha256"))


def synchronize_policy(
    transport: Path,
    host: str,
    executable: str,
    repository: str,
    remote_state: str,
    local_state: Path,
    queue_root: str,
) -> dict[str, Any]:
    if platform.system() != "Darwin":
        raise ValueError("policy synchronization runs on the MacBook")
    base = [str(transport), "--host", host, "--", executable]
    result = subprocess.run(
        [*base, "export-policy-runtime", "--repository", repository, "--state-root", remote_state],
        capture_output=True,
        check=True,
        timeout=120,
    ).stdout  # nosec B603
    exported = json.loads(result)
    if exported["state"] == "policy_current":
        return dict(exported)
    payload = {
        name: base64.b64decode(value, validate=True) for name, value in exported["files"].items()
    }
    candidate = stage_bundle(local_state, exported["manifest"], payload)
    if candidate != exported["candidate"]:
        raise ValueError("cross-host staged policy pins differ")
    args = [
        str(transport),
        "--host",
        host,
        "--stdin",
        "--",
        executable,
        "activate-policy-runtime",
        "--repository",
        repository,
        "--state-root",
        remote_state,
        "--queue-root",
        queue_root,
    ]
    activated = (
        candidate
        if exported["state"] == "policy_already_active"
        else json.loads(
            subprocess.run(
                args, input=json_bytes(candidate), capture_output=True, check=True, timeout=120
            ).stdout
        )
    )  # nosec B603
    pointer = local_state / "active.json"
    previous = json.loads(pointer.read_bytes()) if pointer.exists() else {}
    local = activate_bundle(local_state, candidate, expected_parent=previous.get("bundle_sha256"))
    if local != activated:
        raise ValueError("cross-host activated policy pins differ")
    args = [
        str(transport),
        "--host",
        host,
        "--stdin",
        "--",
        executable,
        "confirm-policy-runtime",
        "--state-root",
        remote_state,
        "--queue-root",
        queue_root,
    ]
    peer = json.loads(
        subprocess.run(
            args, input=json_bytes(local), capture_output=True, check=True, timeout=120
        ).stdout
    )  # nosec B603
    confirm_cohort(local_state, peer)
    return {"state": "policy_active", "candidate": local}
