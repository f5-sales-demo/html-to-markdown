"""Promote selected source proofs and replay them without editing original envelopes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .content_inventory import atomic_private, verify_capture
from .curation import json_bytes
from .enrichment.analysis import sha
from .source_existing_context import _record, compile_source_proposal, qualify_source_proposal
from .source_existing_selection import _safe, prepare_source_selection, verify_source_selection
from .source_existing_validation import review_source_selection

MARKER = "selected_existing_source"
VERSION = "existing-source-admission/1"


def prepare_existing_source(
    capture: Path, record: Path, history_roots: list[Path], output: Path
) -> Path:
    """Preserve one necessary current ledger snapshot; retain original proposals by reference."""
    _safe(record)
    _safe(output)
    raw = record.read_bytes()
    origin = output / "current-origins" / (sha(raw) + ".json")
    _safe(origin)
    if origin.exists():
        if origin.read_bytes() != raw:
            raise ValueError("immutable current source origin changed")
    else:
        atomic_private(origin, raw)
    journal = prepare_source_selection(capture, origin, sha(raw), history_roots, output / "inputs")
    if record.read_bytes() != raw:
        raise ValueError("current source record changed during preparation")
    return journal


# Keep current admission, original evidence and final compiled files together.
# pylint: disable-next=too-many-locals
def _selected_admission(
    capture: Path, journal_path: Path, validation_output: Path
) -> tuple[dict[str, Any], dict[str, bytes]]:
    journal = verify_source_selection(journal_path)
    selection = review_source_selection(journal_path, validation_output)
    chosen = next(
        row
        for row in journal["candidates"]
        if row["record_sha256"] == selection["selected_record_sha256"]
    )
    origin = Path(journal["current_record"])
    current = _record(origin, journal["current_record_sha256"])
    item = current["item"]
    _safe(capture)
    if sha((capture / "inventory.json").read_bytes()) != sha(
        (Path(journal["capture"]) / "inventory.json").read_bytes()
    ):
        raise ValueError("selected source capture inventory differs from its origin")
    verify_capture(capture, item)
    original = Path(chosen["custody"][0])
    checked = qualify_source_proposal(
        Path(journal["capture"]),
        origin,
        journal["current_record_sha256"],
        original,
        chosen["record_sha256"],
    )
    selected_result = next(
        row for row in selection["results"] if row["record_sha256"] == chosen["record_sha256"]
    )
    candidate, _, _, files = compile_source_proposal(
        Path(journal["capture"]),
        validation_output / journal_path.stem,
        origin,
        original,
        checked,
        require_cached_media=True,
    )
    if (
        candidate["output_sha256"] != selected_result["candidate_sha256"]
        or candidate["file_hashes"] != selected_result["file_hashes"]
        or checked["editor_corrections_consumed"] != selected_result["editor_corrections_consumed"]
    ):
        raise ValueError("selected source candidate differs from independent proof")
    marker = {
        "version": VERSION,
        "journal_path": str(journal_path),
        "journal_sha256": journal_path.stem,
        "validation_output": str(validation_output),
        "selection_sha256": sha(json_bytes(selection)),
        "original_record_sha256": chosen["record_sha256"],
        "current_origin_sha256": journal["current_record_sha256"],
        "editor_corrections_consumed": checked["editor_corrections_consumed"],
        "new_editor_allowances": 0,
    }
    saved = {
        **current,
        "binding": checked["source_binding"],
        "binding_sha256": checked["source_binding_sha256"],
        "privacy_feedback": checked["original_feedback"],
        "privacy_exhausted": True,
        MARKER: marker,
        "disposition": "accepted",
        "reason": "independently_validated_existing_source_admission",
        "output_sha256": candidate["output_sha256"],
        "file_hashes": candidate["file_hashes"],
        "transforms": candidate["media_transforms"],
        "enforcement_findings": 0,
        "curation_sha256": selected_result["curation_sha256"],
    }
    saved.pop("error_class", None)
    saved.pop("privacy_proposal_pending_validation", None)
    return saved, files


def admit_existing_source(
    capture: Path, output: Path, record: Path, journal_path: Path, validation_output: Path
) -> dict[str, Any]:
    """Caller owns the worker lock; CAS requires the exact prepared current record."""
    _safe(output)
    _safe(record)
    before = record.read_bytes()
    current = json.loads(before)
    if current.get(MARKER):
        return replay_existing_source(capture, output, record)
    journal = verify_source_selection(journal_path)
    if str(record) == journal["current_record"] or sha(before) != journal["current_record_sha256"]:
        raise ValueError("admission requires the exact live record and a distinct immutable origin")
    saved, files = _selected_admission(capture, journal_path, validation_output)
    if record.read_bytes() != before:
        raise ValueError("live source record changed during selected admission")
    for relative, raw in files.items():
        path = Path(relative).relative_to("articles")
        _safe(output / "reviewed" / path)
        atomic_private(output / "reviewed" / path, raw)
    atomic_private(record, json_bytes(saved))
    return saved


def replay_existing_source(capture: Path, output: Path, record: Path) -> dict[str, Any]:
    """Replay into the caller's isolated output; never alter the referenced source proof."""
    _safe(record)
    current = json.loads(record.read_bytes())
    marker = current.get(MARKER, {})
    if marker.get("version") != VERSION or marker.get("new_editor_allowances") != 0:
        raise ValueError("invalid selected existing-source admission")
    journal_path = Path(marker["journal_path"])
    if journal_path.stem != marker["journal_sha256"]:
        raise ValueError("selected source input journal pin changed")
    saved, files = _selected_admission(capture, journal_path, Path(marker["validation_output"]))
    if saved != current:
        raise ValueError("selected source admission differs from its independently replayed proof")
    for relative, raw in files.items():
        path = Path(relative).relative_to("articles")
        _safe(output / "reviewed" / path)
        atomic_private(output / "reviewed" / path, raw)
    return saved
