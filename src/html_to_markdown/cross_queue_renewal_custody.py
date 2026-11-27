"""Recover only the exact archived blocked renewal made by released controller v33."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .curation import json_bytes
from .enrichment.analysis import sha
from .reference_repair_inputs import safe_file
from .review_controller_compatibility import read_registry, validate_registry

_CONTEXT = "54292dd77740af564472e98413b1eb6a03c6b3350e2dd2420cfd7fd80d9e3de1"
_RELEASE = {
    "controller_sha256": "02df33e0a195dfed6570b713c7207c1326d075bb8d448db3acd2acb9d3a17e0b",
    "request_context_sha256": _CONTEXT,
    "software_tag": "v1.14.33",
    "source_commit": "39f0076f505ec00a315b2e9ffee7167ba3fdae7f",
    "wheel_sha256": "cedd8f40dfbf3622630a465b87189c5b52a38b5a948156e688ba421d96d7e555",
}
_HINTS = {"captured_at", "discovered_at", "origins"}


def _expected(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any] | None:
    """Replay v33 preparation followed by its pre-inference blocked failure."""
    binding = current["binding"]
    old = previous.get("binding", {})
    if any(
        (
            not previous.get("cross_queue_seed"),
            not previous.get("cross_queue_evidence_blocked"),
            previous.get("phases"),
            previous.get("boundary_pending"),
            previous.get("privacy_feedback"),
            previous.get("privacy_exhausted"),
            previous.get("reason") != "prior_review_evidence_requires_repair",
            previous.get("error_class") != "CrossQueueEvidenceError",
            previous.get("retryable") is not False,
            previous.get("attempted_binding_sha256") != previous.get("binding_sha256"),
            previous.get("media_encoding_version") is not None,
            old.get("request_context_sha256") == _CONTEXT,
            {k: v for k, v in old.items() if k != "request_context_sha256"}
            != {k: v for k, v in binding.items() if k != "request_context_sha256"},
            {k: v for k, v in previous.get("item", {}).items() if k not in _HINTS}
            != {k: v for k, v in current["item"].items() if k not in _HINTS},
        )
    ):
        return None
    fields = {
        "schema_version",
        "version",
        "item",
        "binding",
        "binding_sha256",
        "phases",
        "disposition",
        "reason",
        "cross_queue_evidence_blocked",
        "cross_queue_seed",
        "cross_queue_storage_retryable",
        "attempted_binding_sha256",
        "retryable",
        "error_class",
    }
    if set(previous) - fields or previous.get("disposition") != "queued":
        return None
    retained = {"cross_queue_evidence_blocked": True}
    if previous.get("cross_queue_storage_retryable"):
        retained["cross_queue_storage_retryable"] = True
    return {
        "schema_version": 1,
        "version": "source-admission/1",
        "item": current["item"],
        "binding": binding,
        "binding_sha256": sha(json_bytes(binding)),
        "phases": {},
        "disposition": "queued",
        "reason": "admission_phase_failed",
        "attempted_binding_sha256": sha(json_bytes(binding)),
        "error_class": "ValueError",
        "retryable": False,
        **retained,
    }


def renewal_snapshot(
    worker: Path, review: Path, current: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Require one exact real pre-renewal snapshot; never synthesize a seed."""
    binding = current["binding"]
    if any(
        (
            current.get("cross_queue_seed"),
            binding.get("request_context_sha256") != _CONTEXT,
            binding.get("model_policy_version") != 2,
            binding.get("editor_model") != "gpt-6.1-sol",
            binding.get("validator_model") != "gpt-6.1-sol",
            binding.get("reasoning") != "high",
        )
    ):
        raise ValueError("seed repair has no qualified archived context renewal")
    registry = read_registry()
    validate_registry(registry)
    if _RELEASE not in registry["previous"]:
        raise ValueError("seed repair historical renewal release is not qualified")
    matches = []
    for path in sorted((review / "history").glob("*.json")):
        path = safe_file(worker, path.relative_to(worker).as_posix())
        raw = path.read_bytes()
        previous = json.loads(raw)
        if not isinstance(previous, dict):
            raise ValueError("seed repair renewal history is opaque")
        if previous.get("item", {}).get("identity") != current["item"]["identity"]:
            continue
        pin = sha(raw)
        if path.stem != pin:
            raise ValueError("seed repair renewal snapshot digest changed")
        if previous.get("binding_sha256") != sha(json_bytes(previous.get("binding", {}))):
            raise ValueError("seed repair renewal binding digest changed")
        if _expected(previous, current) == current:
            matches.append(
                (
                    previous,
                    {
                        "record_sha256": pin,
                        "path": path.relative_to(worker).as_posix(),
                        "qualified_release": _RELEASE,
                        "differing_binding_fields": ["request_context_sha256"],
                    },
                )
            )
    if len(matches) != 1:
        raise ValueError("seed repair requires one unambiguous exact archived context renewal")
    return matches[0]
