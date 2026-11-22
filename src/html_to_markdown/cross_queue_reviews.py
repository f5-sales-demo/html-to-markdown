"""Private prior inputs seed a batch; only current admission replay grants a disposition."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .content_inventory import atomic_private
from .curation import json_bytes
from .enrichment.analysis import sha
from .reference_repair_inputs import safe_file
from .review_controller_compatibility import admission_binding
from .review_priorities import ReviewPriorities, select_records
from .source_phase_bindings import _privacy_budget, privacy_ledger, source_core
from .source_review_jobs import prepare_reviews


def _snapshot(source: Path, destination: Path, root: Path) -> None:
    relative = destination.relative_to(root)
    component = root
    for part in relative.parts:
        component /= part
        if component.is_symlink():
            raise ValueError("unsafe cross-queue destination link")
    if destination.exists():
        if sha(destination.read_bytes()) != sha(source.read_bytes()):
            raise ValueError("cross-queue native evidence conflicts with current cache")
        return
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.link(source, destination)
    except OSError:
        atomic_private(destination, source.read_bytes())


def _eligible(
    output: Path, limit: int, *, priorities: ReviewPriorities | None = None
) -> list[Path]:
    return select_records(output, limit, priorities)


def _prior(
    worker: Path, output: Path, current: dict[str, Any]
) -> tuple[Path, dict[str, Any]] | None:
    name = sha(current["item"]["identity"]) + ".json"
    best_score: tuple[bool, int, bool, bool, int, Path] | None = None
    best_path: Path | None = None
    best_record: dict[str, Any] | None = None
    for directory in sorted(worker.glob("*/*/review")):
        if directory == output or directory.parent.name != current["item"]["sourceId"]:
            continue
        relative = (directory / "documents" / name).relative_to(worker).as_posix()
        if not (worker / relative).exists():
            continue
        path = safe_file(worker, relative)
        previous = json.loads(path.read_bytes())
        if previous["item"]["identity"] != current["item"]["identity"] or previous[
            "binding_sha256"
        ] != sha(json_bytes(previous["binding"])):
            raise ValueError("cross-queue prior record has invalid source guards")
        budget = {
            **_privacy_budget(previous, current["binding"]),
            **privacy_ledger(previous, current["binding"]),
        }
        if not previous.get("phases") and not budget and not previous.get("boundary_pending"):
            continue
        # Qualification preserves the original request context only when every
        # captured/model/policy guard matches. Seeding never grants admission.
        selected_binding = admission_binding(previous, current["binding"])
        exact = previous["binding"] == selected_binding
        if (
            not exact
            and source_core(previous["binding"]) != source_core(selected_binding)
            and not budget
        ):
            continue
        # A weaker historical branch must never grant fresh privacy corrections.
        score = (
            bool(budget.get("cross_queue_evidence_blocked")),
            len(budget.get("privacy_feedback", [])),
            exact,
            previous.get("disposition") in {"accepted", "policy_excluded"},
            path.stat().st_mtime_ns,
            path,
        )
        if best_score is None or score > best_score:
            best_score, best_path, best_record = score, path, previous
    if best_path is None or best_record is None:
        return None
    return best_path, best_record


def _snapshot_boundaries(
    worker: Path,
    output: Path,
    prior_path: Path,
    previous: dict[str, Any],
) -> None:
    # Complete part checkpoints are private, digest-pinned source evidence.
    # A pending first relevance part may have no completed phase yet.
    boundary_digests = {
        value["logical_request_sha256"] for value in previous.get("boundary_pending", {}).values()
    }
    for phase in previous.get("phases", {}).values():
        if proof := phase.get("source_boundary"):
            boundary_digests.add(proof["coverage_plan"]["logical_request_sha256"])
    for boundary_digest in sorted(boundary_digests):
        if len(boundary_digest) != 64 or any(
            value not in "0123456789abcdef" for value in boundary_digest
        ):
            raise ValueError("unsafe cross-queue native boundary digest")
        relative_root = Path("native-boundaries") / boundary_digest
        prior_root = prior_path.parent.parent / relative_root
        if prior_root.is_symlink():
            raise ValueError("unsafe cross-queue native boundary link")
        for cache_path in sorted(prior_root.rglob("*")):
            if cache_path.is_symlink():
                raise ValueError("unsafe cross-queue native boundary link")
            if cache_path.is_file():
                cache = safe_file(worker, cache_path.relative_to(worker).as_posix())
                _snapshot(
                    cache,
                    output / relative_root / cache_path.relative_to(prior_root),
                    output,
                )


def seed_review_batch(
    capture: Path,
    output: Path,
    worker: Path,
    *,
    limit: int,
    priorities: ReviewPriorities | None = None,
) -> dict[str, Any]:
    """Run under the worker lock; atomically preserve inputs before restarting phases."""
    if limit < 1:
        raise ValueError("cross-queue reuse requires a positive batch limit")
    if worker.is_symlink() or output.is_symlink():
        raise ValueError("unsafe cross-queue evidence root")
    output.relative_to(worker)
    result: dict[str, Any] = {
        "schema_version": 1,
        "seeded": [],
        "exceptions": {},
        "publication_approved": False,
    }
    for record in _eligible(output, limit, priorities=priorities):
        current = json.loads(record.read_bytes())
        # An existing transaction, pending proposal or exhausted ledger is authoritative.
        if (
            current.get("phases")
            or current.get("boundary_pending")
            or current.get("privacy_feedback")
            or current.get("cross_queue_seed")
        ):
            continue
        try:
            prior = _prior(worker, output, current)
            if prior is None:
                continue
            path, previous = prior
            digest = sha(path.read_bytes())
            evidence = output / "reuse-evidence" / (digest + ".json")
            _snapshot(path, evidence, output)
            caches = {media["sha256"] for media in previous["item"]["media"]}
            caches.update(previous.get("file_hashes", {}).values())
            for cache_digest in sorted(caches):
                for relative in (
                    f"media/{cache_digest}-native-2.json",
                    f"media/{cache_digest}-native-3.json",
                ):
                    if (path.parent.parent / relative).exists():
                        cache = safe_file(
                            worker, (path.parent.parent / relative).relative_to(worker).as_posix()
                        )
                        destination = output / relative
                        if destination.is_symlink():
                            raise ValueError("unsafe cross-queue native evidence link")
                        _snapshot(cache, destination, output)
            _snapshot_boundaries(worker, output, path, previous)
            seeded = dict(previous)
            if seeded["disposition"] in {"accepted", "policy_excluded"}:
                seeded.update(disposition="queued", reason="cross_queue_current_replay_required")
                seeded.pop("attempted_binding_sha256", None)
                seeded.pop("error_class", None)
            seeded["cross_queue_seed"] = {
                "record_sha256": digest,
                "prior_record": path.relative_to(worker).as_posix(),
            }
            atomic_private(record, json_bytes(seeded))
            result["seeded"].append(
                {"identity": current["item"]["identity"], "record_sha256": digest}
            )
        except (ValueError, KeyError, TypeError, OSError) as failure:
            atomic_private(
                output / "reuse-errors" / (record.name + ".json"),
                json_bytes({"error": str(failure)}),
            )
            current.update(
                disposition="queued",
                reason="prior_review_evidence_requires_repair",
                error_class="CrossQueueStorageError"
                if isinstance(failure, OSError)
                else "CrossQueueEvidenceError",
                attempted_binding_sha256=current["binding_sha256"],
                retryable=isinstance(failure, OSError),
                cross_queue_evidence_blocked=True,
                cross_queue_storage_retryable=isinstance(failure, OSError),
            )
            atomic_private(record, json_bytes(current))
            result["exceptions"][record.name] = current["error_class"]
    # The existing reconciliation binds fresh raw capture receipts and preserves
    # exact phase evidence/ledgers; changed inputs use its ordinary invalidation.
    if result["seeded"]:
        prepare_reviews(capture, output)
    atomic_private(output / "cross-queue-reuse.json", json_bytes(result))
    return result
