"""Prove custody and strongest allowances before repairing an interrupted seed."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .curation import json_bytes
from .enrichment.analysis import sha
from .media_encoding import source_review_encoding
from .reference_repair_inputs import safe_file
from .source_phase_bindings import _privacy_budget, privacy_ledger

CONFLICT = "cross-queue native evidence conflicts with current cache"
_IGNORED = {
    "curation_policy_sha256",
    "api_policy_sha256",
    "request_context_sha256",
    "editor_model",
    "validator_model",
    "model_policy_version",
}


def digest(value: str) -> str:
    """Reject traversal and malformed transaction pins."""
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("seed repair requires exact SHA-256 pins")
    return value


def core(binding: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in binding.items() if key not in _IGNORED}


def checked_record(data: bytes, identity: str) -> dict[str, Any]:
    value = json.loads(data)
    if (
        not isinstance(value, dict)
        or value.get("item", {}).get("identity") != identity
        or not isinstance(value.get("binding"), dict)
        or value.get("binding_sha256") != sha(json_bytes(value["binding"]))
    ):
        raise ValueError("seed repair source record guards are invalid")
    feedback = value.get("privacy_feedback", [])
    if not isinstance(feedback, list) or any(not isinstance(item, str) for item in feedback):
        raise ValueError("seed repair original privacy ledger is malformed")
    return value


def _unused_conflicts(
    worker: Path, review: Path, original_path: Path, original: dict[str, Any]
) -> list[dict[str, str]]:
    required = 2 if source_review_encoding(original) == 1 else 3
    conflicts = []
    digests = {item["sha256"] for item in original["item"]["media"]}
    digests.update(original.get("file_hashes", {}).values())
    for media_digest in sorted(digests):
        digest(media_digest)
        for generation in (2, 3):
            if generation == required:
                continue
            relative = Path("media") / f"{media_digest}-native-{generation}.json"
            a = original_path.parent.parent / relative
            b = review / relative
            if a.exists() and b.exists():
                a = safe_file(worker, a.relative_to(worker).as_posix())
                b = safe_file(worker, b.relative_to(worker).as_posix())
                original_pin = sha(a.read_bytes())
                destination_pin = sha(b.read_bytes())
                if original_pin != destination_pin:
                    conflicts.append(
                        {
                            "path": relative.as_posix(),
                            "original_sha256": original_pin,
                            "destination_sha256": destination_pin,
                        }
                    )
    if not conflicts:
        raise ValueError("seed repair has no proven unused-generation conflict")
    return conflicts


def original_custody(
    worker: Path,
    review: Path,
    current: dict[str, Any],
    original_path: Path,
    original: dict[str, Any],
    original_digest: str,
) -> dict[str, Any]:
    """Follow immutable seed snapshots to the failed transaction, never a weaker branch."""
    identity = current["item"]["identity"]
    chain = []
    seen = set()
    selected = current
    while selected.get("cross_queue_seed"):
        seed = selected["cross_queue_seed"]
        pin = digest(seed["record_sha256"])
        if pin in seen:
            raise ValueError("seed repair custody chain loops")
        seen.add(pin)
        snapshot = safe_file(
            worker, (review / "reuse-evidence" / f"{pin}.json").relative_to(worker).as_posix()
        )
        data = snapshot.read_bytes()
        if sha(data) != pin:
            raise ValueError("seed repair immutable custody changed")
        selected = checked_record(data, identity)
        path = safe_file(worker, seed["prior_record"])
        if path.parent.name != "documents" or path.parent.parent.name != "review":
            raise ValueError("seed repair custody record placement is invalid")
        if (
            not selected.get("cross_queue_evidence_blocked")
            or selected.get("phases")
            or selected.get("boundary_pending")
            or core(selected["binding"]) != core(current["binding"])
        ):
            raise ValueError("seed repair chain is not an interrupted blocked seed")
        review = path.parent.parent
        chain.append({"record_sha256": pin, "prior_record": seed["prior_record"]})
    error = safe_file(
        worker,
        (review / "reuse-errors" / (sha(identity) + ".json.json")).relative_to(worker).as_posix(),
    )
    if json.loads(error.read_bytes()) != {"error": CONFLICT}:
        raise ValueError("seed repair requires the original native-cache conflict")
    snapshot = safe_file(
        worker,
        (review / "reuse-evidence" / f"{original_digest}.json").relative_to(worker).as_posix(),
    )
    if (
        snapshot.read_bytes() != original_path.read_bytes()
        or sha(snapshot.read_bytes()) != original_digest
    ):
        raise ValueError("seed repair original transaction custody is unavailable")
    return {
        "seed_chain": chain,
        "error_sha256": sha(error.read_bytes()),
        "unused_conflicts": _unused_conflicts(worker, review, original_path, original),
    }


def _timestamp_wrapper(value: dict[str, Any]) -> str | None:
    item = value["item"]
    captured_at = item.get("captured_at")
    if not isinstance(captured_at, str):
        return None
    try:
        timestamp = datetime.fromisoformat(captured_at)
    except ValueError:
        return None
    if timestamp.tzinfo is None:
        return None
    # Every field other than this one timestamp remains in the digest guard.
    return sha(json_bytes({**value, "item": {k: v for k, v in item.items() if k != "captured_at"}}))


def _timestamp_aliases(
    blocked_records: dict[str, dict[str, Any]], proven: set[str]
) -> list[dict[str, Any]]:
    qualified: dict[str, str] = {}
    for pin in sorted(proven & blocked_records.keys()):
        key = _timestamp_wrapper(blocked_records[pin])
        if key is not None:
            qualified.setdefault(key, pin)
    aliases = []
    for pin in sorted(blocked_records.keys() - proven):
        key = _timestamp_wrapper(blocked_records[pin])
        known = qualified.get(key) if key is not None else None
        if known is None or (
            blocked_records[pin]["item"]["captured_at"]
            == blocked_records[known]["item"]["captured_at"]
        ):
            raise ValueError("seed repair has unproven blocked history")
        aliases.append(
            {
                "historical_sha256": pin,
                "proven_blocked_sha256": known,
                "differing_fields": ["item.captured_at"],
            }
        )
    return aliases


def strongest_history(
    worker: Path, current: dict[str, Any], original: dict[str, Any], original_path: Path
) -> dict[str, Any]:
    """Inspect all preserved matching source histories; opaque evidence stays blocked."""
    identity = current["item"]["identity"]
    rows = []
    ledgers: list[list[str]] = []
    inspected = {}
    blocked_records = {}
    proven_blocked = set()
    for root in sorted(worker.glob("*/*/review")):
        if root.parent.name != current["item"]["sourceId"]:
            continue
        paths = [root / "documents" / (sha(identity) + ".json")]
        paths.extend(sorted((root / "history").glob("*.json")))
        paths.extend(sorted((root / "reuse-evidence").glob("*.json")))
        for path in paths:
            if not path.exists():
                continue
            path = safe_file(worker, path.relative_to(worker).as_posix())
            data = path.read_bytes()
            inspected[path.relative_to(worker).as_posix()] = sha(data)
            value = json.loads(data)
            if not isinstance(value, dict):
                raise ValueError("seed repair history is opaque")
            if value.get("item", {}).get("identity") != identity:
                continue
            value = checked_record(data, identity)
            if core(value["binding"]) != core(current["binding"]):
                continue
            if path.parent.name in {"history", "reuse-evidence"} and path.stem != sha(data):
                raise ValueError("seed repair historical snapshot digest changed")
            if value.get("cross_queue_evidence_blocked"):
                blocked_records[sha(data)] = value
                if path.parent.name == "documents":
                    custody = original_custody(
                        worker,
                        root,
                        value,
                        original_path,
                        original,
                        sha(original_path.read_bytes()),
                    )
                    proven_blocked.add(sha(data))
                    proven_blocked.update(row["record_sha256"] for row in custody["seed_chain"])
            feedback = {
                **_privacy_budget(value, current["binding"]),
                **privacy_ledger(value, current["binding"]),
            }.get("privacy_feedback", [])
            ledgers.append(feedback)
            rows.append(
                {
                    "path": path.relative_to(worker).as_posix(),
                    "record_sha256": sha(data),
                    "feedback_entries": len(feedback),
                    "feedback_sha256": sha(json_bytes(feedback)),
                }
            )
    aliases = _timestamp_aliases(blocked_records, proven_blocked)
    if not ledgers:
        raise ValueError("seed repair has no complete matching privacy history")
    maximum = max(map(len, ledgers))
    strongest = {sha(json_bytes(value)) for value in ledgers if len(value) == maximum}
    feedback = original.get("privacy_feedback", [])
    if (
        len(strongest) != 1
        or len(feedback) != maximum
        or sha(json_bytes(feedback)) not in strongest
    ):
        raise ValueError("seed repair cannot select a weaker or ambiguous strongest ledger")
    return {
        "history": rows,
        "inspected_files_sha256": inspected,
        "timestamp_wrapper_aliases": aliases,
        "strongest_feedback_entries": maximum,
        "strongest_feedback_sha256": sha(json_bytes(feedback)),
    }
