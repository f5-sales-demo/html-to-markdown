"""Replay known historical source controllers without changing their model requests."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .policy_activation import controller_digest

REGISTRY = "source_review_compatibility.json"


def read_registry() -> dict[str, Any]:
    path = Path(__file__).with_name(REGISTRY)
    if path.is_symlink():
        raise ValueError("unsafe review controller compatibility registry")
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError("invalid review controller compatibility registry type")
    return dict(value)


def validate_registry(value: dict[str, Any]) -> None:
    if (
        set(value) != {"schema_version", "controller_sha256", "previous"}
        or not isinstance(value["schema_version"], int)
        or isinstance(value["schema_version"], bool)
        or value["schema_version"] != 1
    ):
        raise ValueError("unsupported review controller compatibility registry")
    if not isinstance(value["controller_sha256"], str) or not re.fullmatch(
        r"[0-9a-f]{64}", value["controller_sha256"]
    ):
        raise ValueError("invalid review controller compatibility code pin")
    if not isinstance(value["previous"], list):
        raise ValueError("invalid review controller compatibility entries")
    contexts = set()
    for item in value["previous"]:
        if not isinstance(item, dict) or set(item) != {
            "source_commit",
            "software_tag",
            "wheel_sha256",
            "controller_sha256",
            "request_context_sha256",
        }:
            raise ValueError("invalid review controller compatibility entry")
        if (
            any(not isinstance(v, str) for v in item.values())
            or not re.fullmatch(r"[0-9a-f]{40}", str(item["source_commit"]))
            or not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", str(item["software_tag"]))
            or any(
                not re.fullmatch(r"[0-9a-f]{64}", str(item[k]))
                for k in ("wheel_sha256", "controller_sha256", "request_context_sha256")
            )
            or item["request_context_sha256"] in contexts
        ):
            raise ValueError("invalid review controller compatibility qualification pins")
        contexts.add(item["request_context_sha256"])


def _sol_policy_migration(old: dict[str, Any], current: dict[str, Any]) -> bool:
    """Only policy 2 to 3 retains the same complete Sol High model contract."""
    for binding in (old, current):
        version = binding.get("model_policy_version")
        if not isinstance(version, int) or isinstance(version, bool):
            return False
    return (
        old["model_policy_version"] == 2
        and current["model_policy_version"] == 3
        and all(
            binding.get("editor_model") == "gpt-6.1-sol"
            and binding.get("validator_model") == "gpt-6.1-sol"
            and binding.get("reasoning") == "high"
            for binding in (old, current)
        )
    )


def admission_binding(record: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Keep a qualified original context only when every captured core guard is identical.

    This reproduces the original logical request. It grants no approval: source,
    native, response, privacy, curation and output replay still must pass.
    """
    old = record.get("binding", {})
    if not old or old == current:
        return current
    registry = read_registry()
    validate_registry(registry)
    if registry["controller_sha256"] != controller_digest():
        return current
    if old.get("request_context_sha256") not in {
        item["request_context_sha256"] for item in registry["previous"]
    }:
        return current
    ignored = {"request_context_sha256"}
    if _sol_policy_migration(old, current):
        ignored.add("model_policy_version")
    if {k: v for k, v in old.items() if k not in ignored} != {
        k: v for k, v in current.items() if k not in ignored
    }:
        return current
    return dict(old)


def replay_provenance(contexts: set[str]) -> dict[str, Any]:
    registry = read_registry()
    validate_registry(registry)
    previous = sorted(contexts & {item["request_context_sha256"] for item in registry["previous"]})
    if not previous or registry["controller_sha256"] != controller_digest():
        return {}
    from .enrichment.analysis import sha  # pylint: disable=import-outside-toplevel

    return {
        "review_compatibility": {
            "registry_sha256": sha(Path(__file__).with_name(REGISTRY).read_bytes()),
            "controller_sha256": registry["controller_sha256"],
            "prior_request_context_sha256": previous,
        }
    }
