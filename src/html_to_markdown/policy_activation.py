"""Immutable qualified policy bundles and atomic installed-worker activation."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from .content_inventory import atomic_private

POLICY = "curation_policy.json"
RECEIPT = "curation-renewal-receipt.json"
CATALOGS = {"legacy_review.json", "appstack_review.json", "vesctl_review.json"}
EVIDENCE = {
    "captured-documents.json.gz",
    "legacy_media_review.json",
    "smsv2-contract-manifest.json.gz",
    "smsv2-contract.json.gz",
    "smsv2-evidence-receipt.json.gz",
    "smsv2-specification.json.gz",
    "source-inventory.json.gz",
    "terraform_destination_catalog.json",
    "terraform_linked_examples.json",
    "terraform_registry_13_1_1.json.gz",
    "vesctl_media_review.json",
}


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def _pin(value: Any, length: int = 64) -> str:
    if not isinstance(value, str) or not re.fullmatch(f"[0-9a-f]{{{length}}}", value):
        raise ValueError("invalid policy activation pin")
    return value


def controller_digest(root: Path | None = None) -> str:
    root = root or Path(__file__).parent
    return _digest(
        _json(
            {
                p.relative_to(root).as_posix(): _digest(p.read_bytes())
                for p in sorted(root.rglob("*.py"))
                if not p.is_symlink()
            }
        )
    )


def _policy_files(registry: bytes) -> set[str]:
    raw = json.loads(registry)
    files = {POLICY}
    for topic in raw["topics"]:
        if catalog := topic.get("review_catalog"):
            if catalog["path"] not in CATALOGS:
                raise ValueError("policy bundle catalog path is outside the public allowlist")
            files.add(catalog["path"])
        for evidence in topic["evidence_inputs"]:
            if "path" in evidence:
                if evidence["path"] not in EVIDENCE:
                    raise ValueError("policy evidence path is outside the public package allowlist")
                files.add(evidence["path"])
    return files


def create_bundle(
    files: dict[str, bytes],
    qualification: bytes,
    *,
    source_commit: str,
    expected_policy_sha256: str,
    expected_controller_sha256: str,
) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Inputs originate from an exact protected-main tree; inference evidence stays private."""
    _pin(source_commit, 40)
    _pin(expected_policy_sha256)
    _pin(expected_controller_sha256)
    if POLICY not in files or set(files) != _policy_files(files[POLICY]):
        raise ValueError("policy bundle public catalog set is incomplete or unexpected")
    receipt = json.loads(qualification)
    if (
        receipt.get("policy_sha256") != expected_policy_sha256
        or not receipt.get("post_planner_validated")
        or receipt.get("enforcement_findings") != 0
    ):
        raise ValueError(
            "policy activation requires independently qualified zero-enforcement output"
        )
    _pin(receipt.get("qualification_sha256"))
    _pin(receipt.get("baseline_receipt_sha256"))
    changed = receipt.get("files", {})
    if not changed or set(changed) - set(files):
        raise ValueError("policy qualification file set differs from protected-main bundle")
    for name, pin in changed.items():
        if _digest(files[name]) != _pin(pin):
            raise ValueError("qualified policy catalog bytes changed")
    payload = {**files, RECEIPT: qualification}
    manifest = {
        "schema_version": 1,
        "source_repository": "f5-sales-demo/html-to-markdown",
        "source_commit": source_commit,
        "policy_sha256": expected_policy_sha256,
        "controller_sha256": expected_controller_sha256,
        "qualification_sha256": receipt["qualification_sha256"],
        "files": {name: _digest(data) for name, data in sorted(payload.items())},
    }
    return manifest, payload


def verify_bundle(
    root: Path, digest: str, *, expected_controller: str | None = None
) -> dict[str, Any]:
    _pin(digest)
    if root.is_symlink() or any(p.is_symlink() for p in root.iterdir()):
        raise ValueError("policy bundle cannot contain symlinks")
    manifest_bytes = (root / "manifest.json").read_bytes()
    if _digest(manifest_bytes) != digest:
        raise ValueError("policy activation manifest changed")
    manifest: dict[str, Any] = json.loads(manifest_bytes)
    if (
        manifest.get("schema_version") != 1
        or manifest.get("source_repository") != "f5-sales-demo/html-to-markdown"
    ):
        raise ValueError("unknown policy activation contract or repository")
    _pin(manifest["source_commit"], 40)
    if manifest["controller_sha256"] != (expected_controller or controller_digest()):
        raise ValueError("policy bundle requires different installed software")
    files = manifest["files"]
    if set(files) != _policy_files((root / POLICY).read_bytes()) | {RECEIPT}:
        raise ValueError("policy bundle catalog set changed")
    if {p.name for p in root.iterdir()} != set(files) | {"manifest.json"}:
        raise ValueError("policy bundle contains unexpected evidence or members")
    payload = {}
    for name, pin in files.items():
        data = (root / name).read_bytes()
        if _digest(data) != _pin(pin):
            raise ValueError("policy activation catalog digest changed")
        payload[name] = data
    recreated, _ = create_bundle(
        {name: data for name, data in payload.items() if name != RECEIPT},
        payload[RECEIPT],
        source_commit=manifest["source_commit"],
        expected_policy_sha256=manifest["policy_sha256"],
        expected_controller_sha256=manifest["controller_sha256"],
    )
    if recreated != manifest:
        raise ValueError("policy activation qualification changed")
    from .curation import load_curation_policy  # pylint: disable=import-outside-toplevel

    if load_curation_policy(root / POLICY).sha256 != manifest["policy_sha256"]:
        raise ValueError("policy bundle does not reproduce its qualified policy pin")
    return manifest


def stage_bundle(
    state_root: Path, manifest: dict[str, Any], files: dict[str, bytes]
) -> dict[str, Any]:
    digest = _digest(_json(manifest))
    destination = state_root / "bundles" / digest
    if state_root.is_symlink() or destination.parent.is_symlink() or destination.is_symlink():
        raise ValueError("unsafe policy activation directory")
    if not destination.exists():
        import tempfile  # pylint: disable=import-outside-toplevel

        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="policy-stage-", dir=destination.parent
        ) as temporary:
            candidate = Path(temporary) / "bundle"
            candidate.mkdir(mode=0o700)
            if set(files) != set(manifest["files"]):
                raise ValueError("staged policy bundle payload set changed")
            for name, data in files.items():
                if name not in {POLICY, RECEIPT, *CATALOGS, *EVIDENCE}:
                    raise ValueError("unsafe policy activation file")
                atomic_private(candidate / name, data)
            atomic_private(candidate / "manifest.json", _json(manifest))
            verify_bundle(candidate, digest)
            candidate.rename(destination)
    verified = verify_bundle(destination, digest)
    return {
        "bundle_sha256": digest,
        **{
            key: verified[key]
            for key in (
                "source_commit",
                "policy_sha256",
                "controller_sha256",
                "qualification_sha256",
            )
        },
    }


def activate_bundle(
    state_root: Path, candidate: dict[str, Any], *, expected_parent: str | None
) -> dict[str, Any]:
    """Caller holds the worker/publication lock and verifies protected-main freshness."""
    current = state_root / "active.json"
    previous = json.loads(current.read_bytes()) if current.exists() else {}
    if previous.get("bundle_sha256") != expected_parent:
        raise ValueError("policy activation parent was superseded")
    manifest = verify_bundle(
        state_root / "bundles" / _pin(candidate["bundle_sha256"]), candidate["bundle_sha256"]
    )
    expected = {
        "bundle_sha256": candidate["bundle_sha256"],
        **{
            key: manifest[key]
            for key in (
                "source_commit",
                "policy_sha256",
                "controller_sha256",
                "qualification_sha256",
            )
        },
    }
    if candidate != expected:
        raise ValueError("policy activation candidate pin changed")
    if previous != expected:
        (state_root / "cohort.json").unlink(missing_ok=True)
    atomic_private(current, _json(expected))
    return expected


def active_policy_path(state_root: Path | None = None) -> Path | None:
    state_root = state_root or Path(
        os.environ.get(
            "HTML_TO_MARKDOWN_POLICY_STATE",
            str(Path.home() / ".local/share/html-to-markdown/policy"),
        )
    )
    current = state_root / "active.json"
    if not current.exists():
        return None
    if current.is_symlink():
        raise ValueError("policy activation pointer cannot be a symlink")
    active = json.loads(current.read_bytes())
    root = state_root / "bundles" / _pin(active["bundle_sha256"])
    manifest = verify_bundle(root, active["bundle_sha256"])
    for key in ("source_commit", "policy_sha256", "controller_sha256", "qualification_sha256"):
        if active[key] != manifest[key]:
            raise ValueError("policy activation pointer differs from qualified bundle")
    return root / POLICY


def require_cohort(local: dict[str, Any], remote: dict[str, Any]) -> None:
    if not local or local != remote:
        raise ValueError("Mac and Ubuntu policy activation pins do not agree")


def require_active_cohort(state_root: Path | None = None) -> None:
    state_root = state_root or Path(
        os.environ.get(
            "HTML_TO_MARKDOWN_POLICY_STATE",
            str(Path.home() / ".local/share/html-to-markdown/policy"),
        )
    )
    pointer = state_root / "active.json"
    if pointer.exists():
        active_policy_path(state_root)
        cohort = state_root / "cohort.json"
        if not cohort.exists():
            raise ValueError("policy activation awaits confirmed Mac and Ubuntu agreement")
        require_cohort(json.loads(pointer.read_bytes()), json.loads(cohort.read_bytes()))


def confirm_cohort(state_root: Path, peer: dict[str, Any]) -> dict[str, Any]:
    active_policy_path(state_root)
    local: dict[str, Any] = json.loads((state_root / "active.json").read_bytes())
    require_cohort(local, peer)
    atomic_private(state_root / "cohort.json", _json(local))
    return local
