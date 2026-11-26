"""Renew exact historical proposals without changing their source or editorial ledger."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .baseline_proposal_history import ProposalHistoryLookup
from .curation import json_bytes
from .enrichment.analysis import sha
from .enrichment.contracts import ResponseEvidence
from .enrichment.gates import request_hash
from .history_preservation import preserve_history
from .source_boundary_review import source_logical_request

VERSION = "baseline-proposal-renewal/1"
ORIGINAL_FIELDS = (
    "binding",
    "binding_sha256",
    "source",
    "item",
    "input_file_hashes",
    "privacy_feedback",
    "phases",
)


def same_source(before: dict[str, Any], after: dict[str, Any]) -> bool:
    keys = (
        "input_kind",
        "baseline_receipt_sha256",
        "baseline_article",
        "baseline_article_sha256",
        "source_files_sha256",
    )
    return all(before.get(key) == after.get(key) for key in keys)


def _history(output: Path, digest: str) -> dict[str, Any]:
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(c not in "0123456789abcdef" for c in digest)
    ):
        raise ValueError("unsafe historical baseline proposal digest")
    path = output / "history" / (digest + ".json")
    if path.is_symlink() or not path.is_file():
        raise ValueError("historical baseline proposal record is missing or changed")
    raw = path.read_bytes()
    if sha(raw) != digest:
        raise ValueError("historical baseline proposal record is missing or changed")
    return dict(json.loads(raw))


def original_proposal(
    output: Path,
    previous: dict[str, Any],
    binding: dict[str, Any],
    *,
    lookup: ProposalHistoryLookup | None = None,
) -> dict[str, Any] | None:
    if not same_source(previous.get("binding", {}), binding):
        return None
    selection = previous.get("selected_existing_validation") or previous.get(
        "existing_proposal_pending"
    )
    if selection:
        digest = selection.get("original_record_sha256", selection.get("source_record_sha256"))
        original = _history(output, digest)
        if any(original.get(key) != previous.get(key) for key in ORIGINAL_FIELDS):
            raise ValueError("historical baseline proposal or correction ledger changed")
        return original
    if previous.get("phases", {}).get("privacy"):
        return previous
    best = None
    score = None
    owned_lookup = lookup is None
    lookup = lookup or ProposalHistoryLookup(output)
    for path in lookup.paths(binding):
        value = _history(output, path.stem)
        if not isinstance(value, dict) or not value.get("phases", {}).get("privacy"):
            continue
        if value.get("identity") != previous.get("identity") or not same_source(
            value.get("binding", {}), binding
        ):
            continue
        rank = (len(value.get("privacy_feedback", [])), path.stat().st_mtime_ns)
        if score is None or rank > score:
            score, best = rank, value
    if owned_lookup:
        lookup.verify()
    return best


def prepare_existing_proposal(
    output: Path,
    previous: dict[str, Any],
    binding: dict[str, Any],
    execution: str,
    *,
    lookup: ProposalHistoryLookup | None = None,
) -> dict[str, Any] | None:
    original = original_proposal(output, previous, binding, lookup=lookup)
    if original is None:
        return None
    data = json_bytes(original)
    digest = sha(data)
    if lookup is None:
        preserve_history(output, data)
    else:
        lookup.preserve(data)
    saved = dict(original)
    saved.pop("selected_existing_validation", None)
    saved.pop("candidate", None)
    saved.pop("privacy_validation_evidence", None)
    saved.pop("privacy_validation_binding_sha256", None)
    saved.update(
        status="privacy_review_required",
        existing_proposal_pending={
            "version": VERSION,
            "source_record_sha256": digest,
            "prepared_binding": binding,
            "prepared_binding_sha256": sha(json_bytes(binding)),
            "execution_sha256": execution,
            "new_editor_proposals": 0,
        },
    )
    return saved


def verify_pending(
    output: Path, saved: dict[str, Any], execution: str, expected: dict[str, Any]
) -> str | None:
    pending = saved.get("existing_proposal_pending")
    if not pending:
        return None
    binding = pending["prepared_binding"]
    checks = (
        pending.get("version") == VERSION,
        pending.get("execution_sha256") == execution,
        pending.get("prepared_binding_sha256") == sha(json_bytes(binding)),
        same_source(saved["binding"], binding),
        pending.get("new_editor_proposals") == 0,
        all(binding.get(key) == value for key, value in expected.items()),
    )
    if not all(checks):
        raise ValueError("stale or changed historical proposal renewal")
    digest = pending["source_record_sha256"]
    original = _history(output, digest)
    if any(original.get(key) != saved.get(key) for key in ORIGINAL_FIELDS):
        raise ValueError("historical baseline proposal or correction ledger changed")
    return str(digest)


def relevance_checkpoint(path: Path, body: dict[str, Any]) -> Path:
    if path.is_symlink():
        raise ValueError("unsafe baseline relevance checkpoint")
    if path.exists():
        previous = ResponseEvidence.model_validate_json(path.read_bytes())
        if source_logical_request(previous) == body:
            return path
    return path.parent.parent / "request-checkpoints" / path.stem / (request_hash(body) + ".json")


def preparation_controller() -> str:
    return sha(
        json_bytes(
            {
                "baseline": sha(Path(__file__).with_name("baseline_source_review.py").read_bytes()),
                "renewal": sha(Path(__file__).read_bytes()),
                "proposal_history": sha(
                    Path(__file__).with_name("baseline_proposal_history.py").read_bytes()
                ),
                "context": sha(Path(__file__).with_name("baseline_source_context.py").read_bytes()),
                "dispositions": sha(
                    Path(__file__).with_name("baseline_dispositions.py").read_bytes()
                ),
            }
        )
    )
