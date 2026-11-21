"""Reconcile retained privacy allowances across private prerequisite queues."""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .baseline_history_lookup import SOURCE_KEYS, HistoryLookup, history_roots
from .baseline_review_renewal import ORIGINAL_FIELDS, prepare_existing_proposal
from .content_inventory import atomic_private
from .curation import json_bytes
from .enrichment.analysis import sha


def _same_source(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return all(left.get(key) == right.get(key) for key in SOURCE_KEYS)


def _safe(path: Path) -> None:
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError("unsafe retained privacy ledger path")


def _verify_source(output: Path, saved: dict[str, Any]) -> None:
    if sha(json_bytes(saved["binding"])) != saved["binding_sha256"]:
        raise ValueError("retained privacy ledger binding changed")
    relative = Path(saved["source"])
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("unsafe retained privacy ledger source")
    files = saved["input_file_hashes"]
    if sha(json_bytes(files)) != saved["binding"]["source_files_sha256"]:
        raise ValueError("retained privacy ledger input inventory changed")
    for name, digest in files.items():
        member = Path(name)
        if member.is_absolute() or ".." in member.parts:
            raise ValueError("unsafe retained privacy ledger member")
        path = output / relative / member
        _safe(path)
        if not path.is_file() or sha(path.read_bytes()) != digest:
            raise ValueError("retained privacy ledger source bytes changed")
    article = "articles/" + saved["binding"]["baseline_article"]
    if files.get(article) != saved["binding"]["baseline_article_sha256"]:
        raise ValueError("retained privacy ledger article identity changed")


def _original(output: Path, saved: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
    selection = saved.get("selected_existing_validation") or saved.get("existing_proposal_pending")
    if not selection:
        return saved, json_bytes(saved)
    digest = selection.get("original_record_sha256", selection.get("source_record_sha256"))
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(c not in "0123456789abcdef" for c in digest)
    ):
        raise ValueError("unsafe retained privacy history digest")
    path = output / "history" / (digest + ".json")
    _safe(path)
    data = path.read_bytes()
    if sha(data) != digest:
        raise ValueError("retained privacy history changed")
    original = json.loads(data)
    if any(original.get(key) != saved.get(key) for key in ORIGINAL_FIELDS):
        raise ValueError("retained privacy historical proposal or allowance changed")
    return original, data


def history_root(journal: Path) -> Path:
    configured = os.environ.get("HTML_TO_MARKDOWN_BASELINE_HISTORY_ROOT")
    root = Path(configured) if configured else journal.parent
    if not root.is_absolute():
        raise ValueError("retained privacy history root must be absolute")
    _safe(root)
    return root


@contextmanager
def source_lock(record: Path, worker_root: Path) -> Iterator[None]:
    _safe(record)
    _safe(worker_root)
    binding = json.loads(record.read_bytes())["binding"]
    key = sha(json_bytes({name: binding[name] for name in SOURCE_KEYS}))
    root = worker_root / ".baseline-ledger-locks"
    _safe(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / key
    _safe(path)
    with path.open("a+b") as lock:
        os.fchmod(lock.fileno(), 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _candidates(
    output: Path,
    record: Path,
    worker_root: Path,
    wanted: dict[str, Any],
    *,
    lookup: HistoryLookup | None = None,
) -> Iterator[tuple[Path, dict[str, Any], bytes]]:
    if lookup is not None and (lookup.output != output or lookup.worker_root != worker_root):
        raise ValueError("retained privacy history lookup custody mismatch")
    indexed = lookup.paths(wanted) if lookup is not None else None
    for root in history_roots(output, worker_root):
        target = root / "documents" / record.name
        _safe(target)
        _safe(root / "history")
        if target.is_file():
            data = target.read_bytes()
            if lookup is not None:
                lookup.watch_current(target, sha(data))
            saved = json.loads(data)
            if _same_source(saved.get("binding", {}), wanted):
                _verify_source(root, saved)
                original, original_bytes = _original(root, saved)
                _verify_source(root, original)
                yield target, original, original_bytes
        elif lookup is not None:
            lookup.watch_current(target, None)
        paths = (
            [path for path in indexed if path.parent.parent == root]
            if indexed is not None
            else sorted((root / "history").glob("*.json"))
        )
        for path in paths:
            _safe(path)
            data = path.read_bytes()
            if sha(data) != path.stem:
                raise ValueError("retained privacy history digest changed")
            saved = json.loads(data)
            if not _same_source(saved.get("binding", {}), wanted):
                continue
            _verify_source(root, saved)
            original, original_bytes = _original(root, saved)
            _verify_source(root, original)
            yield path, original, original_bytes


def _ledger_digest(saved: dict[str, Any]) -> str:
    return sha(
        json_bytes(
            {
                "privacy_feedback": saved.get("privacy_feedback", []),
                "privacy": saved.get("phases", {}).get("privacy"),
            }
        )
    )


def reconcile_record(
    output: Path,
    record: Path,
    worker_root: Path,
    execution_sha256: str,
    *,
    lookup: HistoryLookup | None = None,
) -> dict[str, Any]:
    """Carry the strongest exact-source ledger without granting an approval."""
    _safe(output)
    _safe(record)
    _safe(worker_root)
    current_bytes = record.read_bytes()
    current = json.loads(current_bytes)
    wanted = current.get("existing_proposal_pending", {}).get(
        "prepared_binding", current["binding"]
    )
    _verify_source(output, current)
    original, previous_bytes = _original(output, current)
    strongest = (len(original.get("privacy_feedback", [])), original, previous_bytes, str(record))
    strongest_ledgers = {_ledger_digest(original)}
    evidence: dict[str, dict[str, Any]] = {}
    for candidate, previous, previous_bytes in _candidates(
        output, record, worker_root, wanted, lookup=lookup
    ):
        previous_digest = sha(previous_bytes)
        if previous_digest in evidence:
            continue
        count = len(previous.get("privacy_feedback", []))
        evidence[previous_digest] = {"record_sha256": previous_digest, "feedback_entries": count}
        if count > strongest[0]:
            strongest_ledgers = {_ledger_digest(previous)}
        elif count == strongest[0]:
            strongest_ledgers.add(_ledger_digest(previous))
        if (count, bool(previous.get("phases", {}).get("privacy")), previous_digest) > (
            strongest[0],
            bool(strongest[1].get("phases", {}).get("privacy")),
            sha(strongest[2]),
        ):
            strongest = (count, previous, previous_bytes, str(candidate))
    if strongest[0] and len(strongest_ledgers) > 1:
        raise ValueError("conflicting strongest retained privacy ledgers require review")
    changed = False
    if strongest[0] > len(original.get("privacy_feedback", [])) or (
        not original.get("phases", {}).get("privacy")
        and strongest[1].get("phases", {}).get("privacy")
    ):
        if not strongest[1].get("phases", {}).get("privacy"):
            raise ValueError("stronger retained privacy ledger lacks its original proposal")
        # Exact source custody is rechecked at the destination. Only immutable
        # original evidence is copied; response contexts and budgets are intact.
        _verify_source(output, strongest[1])
        atomic_private(output / "history" / (sha(current_bytes) + ".json"), current_bytes)
        atomic_private(output / "history" / (sha(strongest[2]) + ".json"), strongest[2])
        if lookup is not None:
            lookup.remember(output / "history" / (sha(current_bytes) + ".json"))
            lookup.remember(output / "history" / (sha(strongest[2]) + ".json"))
        renewed = prepare_existing_proposal(output, strongest[1], wanted, execution_sha256)
        if renewed is None or renewed["existing_proposal_pending"]["new_editor_proposals"] != 0:
            raise ValueError("retained privacy reconciliation would change an editorial allowance")
        if len(renewed["privacy_feedback"]) != strongest[0]:
            raise ValueError("retained privacy reconciliation lost prior feedback")
        renewed["retained_privacy_ledger"] = {
            "version": 1,
            "original_record_sha256": sha(strongest[2]),
            "source_files_sha256": wanted["source_files_sha256"],
            "new_editor_proposals": 0,
        }
        atomic_private(record, json_bytes(renewed))
        if lookup is not None:
            lookup.update_current(record)
        changed = True
    result = {
        "identity": current["identity"],
        "source_files_sha256": wanted["source_files_sha256"],
        "strongest_feedback_entries": strongest[0],
        "strongest_record_sha256": sha(strongest[2]),
        "matching_evidence": sorted(evidence.values(), key=lambda item: item["record_sha256"]),
        "changed": changed,
        "new_editor_proposals": 0 if changed else None,
        "article_approved": False,
    }
    atomic_private(output / "ledger-reconciliation" / record.name, json_bytes(result))
    return result


def verify_record_ledger(output: Path, saved: dict[str, Any]) -> None:
    """A reconciled source can validate its original proposal, never replace it."""
    proof = saved.get("retained_privacy_ledger")
    if not proof:
        return
    if proof.get("version") != 1 or proof.get("new_editor_proposals") != 0:
        raise ValueError("retained privacy ledger editorial allowance changed")
    digest = proof.get("original_record_sha256")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(c not in "0123456789abcdef" for c in digest)
    ):
        raise ValueError("invalid retained privacy ledger origin")
    path = output / "history" / (digest + ".json")
    _safe(path)
    data = path.read_bytes()
    if sha(data) != digest:
        raise ValueError("retained privacy ledger origin changed")
    original = json.loads(data)
    _verify_source(output, original)
    if (
        not _same_source(saved["binding"], original["binding"])
        or proof["source_files_sha256"] != saved["binding"]["source_files_sha256"]
        or saved.get("privacy_feedback", []) != original.get("privacy_feedback", [])
        or saved.get("phases", {}).get("privacy") != original.get("phases", {}).get("privacy")
    ):
        raise ValueError("retained privacy ledger or original proposal was superseded")


def verify_strongest_ledger(
    output: Path, record: Path, worker_root: Path, *, lookup: HistoryLookup | None = None
) -> None:
    """Read-only final replay rejects a weaker source ledger without repairing it."""
    saved = json.loads(record.read_bytes())
    _verify_source(output, saved)
    verify_record_ledger(output, saved)
    count = len(saved.get("privacy_feedback", []))
    for _, original, _ in _candidates(output, record, worker_root, saved["binding"], lookup=lookup):
        if len(original.get("privacy_feedback", [])) > count:
            raise ValueError("retained source has a stronger prior privacy ledger")
        if (
            count
            and len(original.get("privacy_feedback", [])) == count
            and _ledger_digest(original) != _ledger_digest(saved)
        ):
            raise ValueError("conflicting strongest retained privacy ledgers require review")


def reconcile_batch(
    output: Path, inventory: dict[str, Any], worker_root: Path, execution_sha256: str
) -> list[dict[str, Any]]:
    lookup = HistoryLookup(output, worker_root)
    results = [
        reconcile_record(
            output, output / item["path"], worker_root, execution_sha256, lookup=lookup
        )
        for item in inventory["documents"]
    ]
    lookup.verify()
    return results
