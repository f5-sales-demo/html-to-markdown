"""Private exact-source ledger inventory, independently of admission eligibility."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .baseline_history_lookup import SOURCE_KEYS, HistoryLookup, safe
from .baseline_ledger_custody import _candidates, _ledger_digest, _original, _verify_source
from .content_inventory import atomic_private
from .curation import json_bytes
from .enrichment.analysis import sha

VERSION = "baseline-ledger-inventory/1"


# One inventory retains all original custody and budget guards together.
# pylint: disable-next=too-many-locals
def inventory_record(
    output: Path,
    record: Path,
    worker_root: Path,
    *,
    lookup: HistoryLookup | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """Record every strongest original without choosing or renewing an editor."""
    safe(output)
    safe(record)
    safe(worker_root)
    before = record.read_bytes()
    current = json.loads(before)
    wanted = current.get("existing_proposal_pending", {}).get(
        "prepared_binding", current["binding"]
    )
    _verify_source(output, current)
    original, data = _original(output, current)
    records: dict[str, dict[str, Any]] = {}

    def retain(path: Path, saved: dict[str, Any], raw: bytes) -> None:
        digest = sha(raw)
        entry = {
            "record_sha256": digest,
            "ledger_sha256": _ledger_digest(saved),
            "feedback_entries": len(saved.get("privacy_feedback", [])),
            "has_privacy_proposal": bool(saved.get("phases", {}).get("privacy")),
            "custody": [],
        }
        if digest not in records:
            records[digest] = entry
        elif {key: value for key, value in records[digest].items() if key != "custody"} != {
            key: value for key, value in entry.items() if key != "custody"
        }:
            raise ValueError("same original digest has inconsistent ledger inventory")
        custody = {"path": str(path), "root": str(path.parent.parent)}
        if custody not in records[digest]["custody"]:
            records[digest]["custody"].append(custody)

    retain(record, original, data)
    owned_lookup = lookup is None
    lookup = lookup or HistoryLookup(output, worker_root)
    for path, saved, raw in _candidates(output, record, worker_root, wanted, lookup=lookup):
        retain(path, saved, raw)
    if owned_lookup:
        lookup.verify()
    if record.read_bytes() != before:
        raise ValueError("source record changed during ledger inventory")
    candidates = sorted(records.values(), key=lambda value: value["record_sha256"])
    for candidate in candidates:
        candidate["custody"].sort(key=lambda value: value["path"])
    strongest = max(value["feedback_entries"] for value in candidates)
    maximum = [value for value in candidates if value["feedback_entries"] == strongest]
    result = {
        "version": VERSION,
        "identity": current["identity"],
        "source_binding": {name: wanted[name] for name in SOURCE_KEYS},
        "source_record_sha256": sha(before),
        "candidates": candidates,
        "strongest_records": maximum,
        "strongest_feedback_entries": strongest,
        "conflicted": bool(strongest and len({value["ledger_sha256"] for value in maximum}) > 1),
        "controller_sha256": sha(Path(__file__).read_bytes()),
        "new_editor_proposals": 0,
        "article_approved": False,
    }
    result["inventory_sha256"] = sha(json_bytes(result))
    if persist:
        atomic_private(output / "ledger-inventory" / record.name, json_bytes(result))
    return result
