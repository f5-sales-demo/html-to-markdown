"""Private reviewed retirement inputs and exact qualified API-stage replay."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .legacy_curation import LegacyResult
from .render import normalize_body


def sha(data: bytes | str) -> str:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def _location(path: Path) -> tuple[Path, str]:
    for root in path.parents:
        relative = path.relative_to(root)
        if relative.parts[0] == "content" and (root / "content").is_dir():
            return root, relative.as_posix()
    raise ValueError("API intermediate lacks a corpus article identity")


def preserve_api_input(
    path: Path, metadata: dict[str, Any], body: str, output_body: str, policy: Any, api: Any
) -> None:
    # Deferred imports avoid the source capture/policy initialization cycle.
    from .content_inventory import atomic_private  # pylint: disable=import-outside-toplevel
    from .curation import json_bytes  # pylint: disable=import-outside-toplevel

    root, relative = _location(path)
    target = root / ".curation-api-inputs" / (sha(relative) + ".json")
    record = {
        "schema_version": 1,
        "document": relative,
        "policy_sha256": policy.sha256,
        "api_policy_sha256": api.policy_digest,
        "api_catalog_sha256": api.catalog_digest,
        "metadata": metadata,
        "body": body,
        "input_sha256": sha(body),
        "output_sha256": sha(normalize_body(output_body)),
    }
    if target.exists():
        previous = target.read_bytes()
        saved = json.loads(previous)
        if all(
            saved.get(key) == record[key]
            for key in (
                "document",
                "policy_sha256",
                "api_policy_sha256",
                "api_catalog_sha256",
                "input_sha256",
                "output_sha256",
            )
        ):
            return
        if previous != json_bytes(record):
            atomic_private(target.parent / "history" / (sha(previous) + ".json"), previous)
    atomic_private(target, json_bytes(record))


def api_record_hash(path: Path) -> str | None:
    root, relative = _location(path)
    target = root / ".curation-api-inputs" / (sha(relative) + ".json")
    if not target.exists():
        return None
    if target.is_symlink() or target.parent.is_symlink():
        raise ValueError("unsafe private API intermediate")
    return sha(target.read_bytes())


def api_source(
    policy: Any, path: Path, metadata: dict[str, Any], body: str, api_policy: Any = None
) -> tuple[dict[str, Any], str] | None:
    """Recover the exact prior stage only after independently replaying its API projection."""
    # The API module already imports curation; resolve it only during verification.
    from .content_policy import (  # pylint: disable=import-outside-toplevel
        load_content_policy,
        project_api_body,
    )

    root, relative = _location(path)
    target = root / ".curation-api-inputs" / (sha(relative) + ".json")
    if not target.exists():
        return None
    if target.is_symlink() or target.parent.is_symlink():
        raise ValueError("unsafe private API intermediate")
    record = json.loads(target.read_bytes())
    audit_path = root / "curation-audit.json"
    if audit_path.is_file():
        audit = json.loads(audit_path.read_bytes())
        registrations = audit.get("content_migration", {}).get("api_inputs")
        if (
            registrations is not None
            and audit.get("policy_sha256") == policy.sha256
            and registrations.get(relative) != sha(target.read_bytes())
        ):
            raise ValueError("private API intermediate differs from its preserved audit")
    if policy.legacy is not None:
        authored = policy.legacy.authored(metadata)
        if (
            policy.legacy.whole(metadata, body)
            or policy.legacy.retired(body)
            or any(policy.legacy.retired(str(value)) for value in authored.values())
            or {k: v for k, v in authored.items() if k != "description"}
            != {
                k: v
                for k, v in policy.legacy.authored(record["metadata"]).items()
                if k != "description"
            }
        ):
            raise ValueError("API projection cannot bypass authored retirement or metadata guards")
    api = api_policy or load_content_policy()
    api.validate_projection()
    expected = {
        "schema_version": 1,
        "document": relative,
        "policy_sha256": policy.sha256,
        "api_policy_sha256": api.policy_digest,
        "api_catalog_sha256": api.catalog_digest,
        "input_sha256": sha(record["body"]),
        "output_sha256": sha(body),
    }
    if (
        {key: record.get(key) for key in expected} != expected
        or record["metadata"]["url"] != metadata["url"]
        or record["metadata"].get("canonical_url") != metadata.get("canonical_url")
    ):
        raise ValueError("stale or mismatched private API intermediate")
    if policy.legacy is not None:
        original = policy.legacy.transform(record["metadata"], record["body"])
        if original.disposition != "keep" or original.body != record["body"]:
            raise ValueError("API intermediate fails original retirement guards")
    projected, mappings, _ = project_api_body(
        record["body"], metadata.get("canonical_url") or metadata["url"], api, policy
    )
    if not mappings or normalize_body(projected) != body:
        raise ValueError("API output differs from its exact qualified transformation")
    return record["metadata"], record["body"]


def legacy_result(
    policy: Any, path: Path, metadata: dict[str, Any], body: str, *, dependent: bool = False
) -> LegacyResult:
    """Check both complete stages; the final planner still needs independent approval."""
    result: LegacyResult = policy.legacy.transform(metadata, body, dependent=dependent)
    if result.disposition == "keep" or dependent:
        return result
    if (
        policy.legacy.whole(metadata, body)
        or policy.legacy.retired(body)
        or any(policy.legacy.retired(str(v)) for v in policy.legacy.authored(metadata).values())
    ):
        return result
    if api_source(policy, path, metadata, body) is None:
        return result
    return LegacyResult(body, ["verified_retirement_and_api_stages"])
