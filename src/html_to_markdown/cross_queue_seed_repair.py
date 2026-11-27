"""Manual, offline and resumable recovery of a fully preserved unused-cache seed."""

from __future__ import annotations

import copy
import fcntl
import json
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .content_inventory import atomic_private, verify_capture
from .cross_queue_repair_proof import (
    checked_record,
    core,
    digest,
    original_custody,
    strongest_history,
)
from .cross_queue_reviews import _snapshot, _snapshot_media
from .curation import json_bytes
from .enrichment.analysis import sha
from .media_encoding import source_review_encoding
from .reference_repair_inputs import safe_file
from .source_phase_bindings import _privacy_budget, privacy_ledger
from .source_review import binding_for
from .source_review_jobs import _article_html


@dataclass(frozen=True)
class SeedRepair:
    worker: Path
    capture: Path
    review: Path
    inference_queue: Path
    prior_record: Path
    current_sha256: str
    prior_sha256: str


def _root(path: Path) -> None:
    if not path.is_absolute() or not path.is_dir():
        raise ValueError("seed repair roots must be existing absolute directories")
    if any(part.is_symlink() for part in [path, *path.parents]):
        raise ValueError("seed repair roots cannot traverse symbolic links")


def _lock(stack: ExitStack, root: Path, name: str) -> None:
    path = root / name
    if path.is_symlink():
        raise ValueError("seed repair lock cannot be a symbolic link")
    if not path.is_file():
        raise ValueError("seed repair requires an existing orchestrator lock")
    handle = stack.enter_context(path.open("r+b"))
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as failure:
        raise ValueError(
            "seed repair requires idle source and inference orchestrators"
        ) from failure


def _drained(queue: Path) -> None:
    for path in queue.glob("*/*/state.json"):
        checked = safe_file(queue, path.relative_to(queue).as_posix())
        state = json.loads(checked.read_bytes()).get("state")
        if state not in {"received"}:
            raise ValueError("seed repair requires drained, known inference states")


def _unchanged(worker: Path, files: dict[str, str]) -> None:
    for name, pin in files.items():
        if sha(safe_file(worker, name).read_bytes()) != pin:
            raise ValueError("seed repair input changed before commit")


def _transaction_root(inputs: SeedRepair) -> Path:
    transaction = (
        inputs.review
        / "seed-repairs"
        / sha(
            json_bytes(
                {
                    "current_sha256": inputs.current_sha256,
                    "prior_sha256": inputs.prior_sha256,
                }
            )
        )
    )
    transaction.relative_to(inputs.worker)
    for component in [transaction, transaction.parent]:
        if component.is_symlink():
            raise ValueError("seed repair transaction cannot be a symbolic link")
    return transaction


def _resume_transaction(
    inputs: SeedRepair,
    original: dict[str, Any],
    current_raw: bytes,
    intent_path: Path,
    receipt_path: Path,
) -> dict[str, Any] | None:
    if intent_path.exists():
        intent = json.loads(
            safe_file(inputs.worker, intent_path.relative_to(inputs.worker).as_posix()).read_bytes()
        )
        if (
            intent.get("current_sha256") != inputs.current_sha256
            or intent.get("prior_sha256") != inputs.prior_sha256
        ):
            raise ValueError("seed repair transaction pins changed")
        if receipt_path.exists():
            receipt = json.loads(
                safe_file(
                    inputs.worker, receipt_path.relative_to(inputs.worker).as_posix()
                ).read_bytes()
            )
            if receipt != {**intent, "state": "repaired_pending_source_review"}:
                raise ValueError("seed repair receipt changed")
            return dict(receipt)
        if sha(current_raw) == intent["repaired_sha256"]:
            resumed = checked_record(current_raw, original["item"]["identity"])
            if resumed.get("cross_queue_evidence_blocked") or resumed.get(
                "privacy_feedback", []
            ) != original.get("privacy_feedback", []):
                raise ValueError("seed repair committed allowance changed")
            _unchanged(inputs.worker, intent["preserved_inputs_sha256"])
            receipt = {**intent, "state": "repaired_pending_source_review"}
            atomic_private(receipt_path, json_bytes(receipt))
            return dict(receipt)
    return None


def _has_phases(record: dict[str, Any]) -> bool:
    return any(
        record.get(key)
        for key in ("phases", "boundary_pending", "privacy_proposal_pending_validation")
    )


def _check_scope(current: dict[str, Any], original: dict[str, Any]) -> None:
    if (
        not current.get("cross_queue_evidence_blocked")
        or _has_phases(current)
        or original.get("cross_queue_evidence_blocked")
        or _has_phases(original)
        or core(current["binding"]) != core(original["binding"])
    ):
        raise ValueError(
            "seed repair requires an exact original without completed or pending phases"
        )


def _prepared_record(
    inputs: SeedRepair,
    prior_path: Path,
    current: dict[str, Any],
    original: dict[str, Any],
) -> tuple[bytes, bytes]:
    identity = current["item"]["identity"]
    item = current["item"]
    capture_inventory = safe_file(inputs.capture, "inventory.json").read_bytes()
    captured = [
        row for row in json.loads(capture_inventory)["documents"] if row["identity"] == identity
    ]
    if len(captured) != 1 or captured[0] != item:
        raise ValueError("seed repair capture inventory differs from the blocked record")
    verify_capture(inputs.capture, item)
    binding = binding_for(item, source_article_sha256=sha(_article_html(inputs.capture, item)))
    retained = {**_privacy_budget(original, binding), **privacy_ledger(original, binding)}
    if retained.get("privacy_feedback", []) != original.get("privacy_feedback", []):
        raise ValueError("seed repair cannot reset the original privacy allowance")
    if "privacy_exhausted" in original or "privacy_exhausted" in retained:
        retained["privacy_exhausted"] = bool(
            original.get("privacy_exhausted") or retained.get("privacy_exhausted")
        )
    repaired = {
        **copy.deepcopy(original),
        "schema_version": current["schema_version"],
        "version": current["version"],
        "item": item,
        "binding": binding,
        "binding_sha256": sha(json_bytes(binding)),
        "phases": {},
        "media_encoding_version": source_review_encoding(original),
        "disposition": "queued",
        "reason": "original_seed_evidence_repaired_review_required",
        "cross_queue_seed": {
            "record_sha256": inputs.prior_sha256,
            "prior_record": prior_path.relative_to(inputs.worker).as_posix(),
        },
        **retained,
    }
    for key in (
        "attempted_binding_sha256",
        "error_class",
        "retryable",
        "cross_queue_storage_retryable",
    ):
        repaired.pop(key, None)
    return capture_inventory, json_bytes(repaired)


def _transaction(inputs: SeedRepair, prior_path: Path) -> dict[str, Any]:
    original_raw = prior_path.read_bytes()
    if sha(original_raw) != inputs.prior_sha256:
        raise ValueError("seed repair original record pin is stale")
    identity = json.loads(original_raw)["item"]["identity"]
    original = checked_record(original_raw, identity)
    record = safe_file(
        inputs.worker,
        (inputs.review / "documents" / (sha(identity) + ".json"))
        .relative_to(inputs.worker)
        .as_posix(),
    )
    transaction = _transaction_root(inputs)
    intent_path = transaction / "intent.json"
    receipt_path = transaction / "receipt.json"
    current_raw = record.read_bytes()
    resumed = _resume_transaction(inputs, original, current_raw, intent_path, receipt_path)
    if resumed is not None:
        return resumed
    if sha(current_raw) != inputs.current_sha256:
        raise ValueError("seed repair current record pin is stale")
    current = checked_record(current_raw, identity)
    _check_scope(current, original)
    custody = original_custody(
        inputs.worker, inputs.review, current, prior_path, original, inputs.prior_sha256
    )
    inventory = strongest_history(inputs.worker, current, original, prior_path)
    capture_inventory, repaired_raw = _prepared_record(inputs, prior_path, current, original)
    _snapshot(
        prior_path,
        inputs.review / "reuse-evidence" / (inputs.prior_sha256 + ".json"),
        inputs.review,
    )
    _snapshot_media(inputs.worker, inputs.review, prior_path, original, inputs.prior_sha256)
    _snapshot(
        record,
        inputs.review / "history" / (inputs.current_sha256 + ".json"),
        inputs.review,
    )
    preserved = {
        prior_path.relative_to(inputs.worker).as_posix(): inputs.prior_sha256,
        (inputs.review / "history" / (inputs.current_sha256 + ".json"))
        .relative_to(inputs.worker)
        .as_posix(): inputs.current_sha256,
    }
    intent = {
        "schema_version": 1,
        "current_sha256": inputs.current_sha256,
        "prior_sha256": inputs.prior_sha256,
        "repaired_sha256": sha(repaired_raw),
        "capture_inventory_sha256": sha(capture_inventory),
        "preserved_inputs_sha256": preserved,
        "custody": custody,
        "history_inventory": inventory,
        "original_feedback_entries": len(original.get("privacy_feedback", [])),
        "editorial_allowances_reset": False,
        "inference_calls": 0,
        "source_review_required": True,
        "publication_approved": False,
    }
    atomic_private(intent_path, json_bytes(intent))
    _unchanged(inputs.worker, inventory["inspected_files_sha256"])
    _unchanged(inputs.worker, preserved)
    if (
        record.read_bytes() != current_raw
        or safe_file(inputs.capture, "inventory.json").read_bytes() != capture_inventory
    ):
        raise ValueError("seed repair source changed before commit")
    verify_capture(inputs.capture, current["item"])
    atomic_private(record, repaired_raw)
    receipt = {**intent, "state": "repaired_pending_source_review"}
    atomic_private(receipt_path, json_bytes(receipt))
    return receipt


def repair_seed(inputs: SeedRepair) -> dict[str, Any]:
    """Restore custody under both existing locks; normal review still decides admission."""
    digest(inputs.current_sha256)
    digest(inputs.prior_sha256)
    for root in [inputs.worker, inputs.capture, inputs.review, inputs.inference_queue]:
        _root(root)
    inputs.review.relative_to(inputs.worker)
    inputs.capture.relative_to(inputs.worker)
    prior = safe_file(inputs.worker, inputs.prior_record.relative_to(inputs.worker).as_posix())
    with ExitStack() as stack:
        _lock(stack, inputs.worker, "worker.lock")
        _lock(stack, inputs.inference_queue, "orchestrator.lock")
        _drained(inputs.inference_queue)
        return _transaction(inputs, prior)
