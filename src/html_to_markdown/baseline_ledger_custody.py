"""Exact source custody and immutable original privacy ledger discovery."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .baseline_history_lookup import SOURCE_KEYS, HistoryLookup, history_roots
from .baseline_ledger_scope import scoped_lookup
from .baseline_review_renewal import ORIGINAL_FIELDS
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


def _candidates(
    output: Path,
    record: Path,
    worker_root: Path,
    wanted: dict[str, Any],
    *,
    lookup: HistoryLookup | None = None,
) -> Iterator[tuple[Path, dict[str, Any], bytes]]:
    lookup = lookup if lookup is not None else scoped_lookup(output, worker_root)
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
