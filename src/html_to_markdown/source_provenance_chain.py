"""Compose immutable source replay provenance through released registry qualifications."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from . import review_controller_compatibility as compatibility
from .enrichment.analysis import sha
from .policy_activation import controller_digest

HISTORY = "source-review-provenance-history.json"
QUALIFICATION = {
    "source_commit",
    "software_tag",
    "wheel_sha256",
    "controller_sha256",
    "request_context_sha256",
}


def _expected(registry: dict[str, Any], pin: str, contexts: set[str]) -> dict[str, Any]:
    return {
        "registry_sha256": pin,
        "controller_sha256": registry["controller_sha256"],
        "prior_request_context_sha256": sorted(
            contexts & {item["request_context_sha256"] for item in registry["previous"]}
        ),
    }


def _historical_registry(
    original: dict[str, Any], current: dict[str, Any]
) -> tuple[dict[str, Any], str, str]:
    path = Path(__file__).with_name(HISTORY)
    if path.is_symlink():
        raise ValueError("unsafe source provenance history")
    raw = path.read_bytes()
    history = json.loads(raw)
    if not isinstance(history, dict) or set(history) != {"schema_version", "registries"}:
        raise ValueError("invalid source provenance history")
    if (
        not isinstance(history["schema_version"], int)
        or isinstance(history["schema_version"], bool)
        or history["schema_version"] != 1
        or not isinstance(history["registries"], list)
    ):
        raise ValueError("invalid source provenance history")
    selected = []
    known = set()
    for item in history["registries"]:
        if (
            not isinstance(item, dict)
            or set(item) != QUALIFICATION | {"registry_sha256", "registry_text"}
            or not isinstance(item["registry_text"], str)
        ):
            raise ValueError("invalid historical registry qualification")
        pin = item["registry_sha256"]
        if (
            not isinstance(pin, str)
            or not re.fullmatch(r"[0-9a-f]{64}", pin)
            or pin in known
            or sha(item["registry_text"]) != pin
        ):
            raise ValueError("historical registry provenance digest changed")
        known.add(pin)
        qualified = {key: item[key] for key in QUALIFICATION}
        if qualified not in current["previous"]:
            raise ValueError("historical registry has no current released qualification")
        registry = json.loads(item["registry_text"])
        compatibility.validate_registry(registry)
        if registry["controller_sha256"] != item["controller_sha256"]:
            raise ValueError("historical registry controller qualification changed")
        if pin == original.get("registry_sha256"):
            selected.append((registry, item["request_context_sha256"]))
    if len(selected) != 1:
        raise ValueError("source provenance uses an unknown historical registry")
    registry, active_context = selected[0]
    return registry, sha(raw), active_context


def verify_provenance_transition(
    original: dict[str, Any],
    current: dict[str, Any] | None,
    contexts: set[str],
    receipt_sha256: str,
) -> dict[str, Any]:
    """Complete source/native/decision/output replay remains a separate prerequisite."""
    registry = compatibility.read_registry()
    compatibility.validate_registry(registry)
    registry_path = Path(compatibility.__file__).with_name(compatibility.REGISTRY)
    expected = _expected(registry, sha(registry_path.read_bytes()), contexts)
    if (
        registry["controller_sha256"] != controller_digest()
        or current != expected
        or not expected["prior_request_context_sha256"]
        or not re.fullmatch(r"[0-9a-f]{64}", receipt_sha256)
    ):
        raise ValueError("current source provenance changed or is unqualified")
    if original == current:
        return {}
    previous, history_pin, active_context = _historical_registry(original, registry)
    permitted = {active_context} | {item["request_context_sha256"] for item in previous["previous"]}
    if (
        not contexts <= permitted
        or original != _expected(previous, original.get("registry_sha256", ""), contexts)
        or not original.get("prior_request_context_sha256")
    ):
        raise ValueError("original source provenance context coverage changed")
    return {
        "version": "source-controller-replay/1",
        "original": original,
        "current": current,
        "history_sha256": history_pin,
        "source_receipt_sha256": receipt_sha256,
        "source_and_decisions_unchanged": True,
    }
