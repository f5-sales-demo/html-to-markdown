"""Bound complete dependency reviews without making admission decisions."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from .content_inventory import atomic_private
from .curation import json_bytes
from .enrichment.analysis import sha
from .source_review import review_context_digest

VERSION: Final = "baseline-review-schedule/1"


class ReviewSchedule(BaseModel):
    """Scheduling coordinates cannot assert review or publication approval."""

    model_config = ConfigDict(extra="forbid")
    version: Literal["baseline-review-schedule/1"] = VERSION
    inventory_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    next_index: int = Field(strict=True, ge=0)
    documents: int = Field(strict=True, ge=1)


def review_quantum(
    output: Path, inventory: dict[str, Any], journal: Path, limit: int
) -> dict[str, Any]:
    """Resume a cohort; advance its cursor only after complete history verification."""
    # pylint: disable-next=import-outside-toplevel
    from .baseline_source_review import review_baseline_batch

    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("dependency review limit must be a positive integer")
    documents = inventory["documents"]
    keys = [(item["identity"], item["path"]) for item in documents]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate dependency scheduling identity")
    checkpoint = output / "review-schedule.json"
    if checkpoint.is_symlink():
        raise ValueError("unsafe dependency scheduling checkpoint")
    pin = sha(json_bytes({"inventory": inventory, "request_context": review_context_digest()}))
    previous = None
    if checkpoint.exists():
        try:
            previous = ReviewSchedule.model_validate_json(checkpoint.read_bytes())
        except ValueError as failure:
            raise ValueError("invalid dependency scheduling checkpoint") from failure
        if previous.next_index >= previous.documents:
            raise ValueError("invalid dependency scheduling checkpoint")
    if not documents:
        return {"reviewed": 0, "deferred": 0, "inventory_sha256": pin}
    start = 0
    if previous is not None:
        if previous.inventory_sha256 == pin:
            if previous.documents != len(documents):
                raise ValueError("dependency scheduling inventory coverage changed")
            start = previous.next_index
        else:
            atomic_private(
                output / "scheduling-history" / (sha(json_bytes(previous.model_dump())) + ".json"),
                json_bytes(previous.model_dump()),
            )
    count = min(limit, len(documents))
    selected = [documents[(start + offset) % len(documents)] for offset in range(count)]
    # This executes complete article reviews and closes the orchestrator client.
    # A cursor or an earlier cohort result is never source/privacy evidence.
    result = review_baseline_batch(output, {**inventory, "documents": selected}, journal)
    if not result["history_integrity_verified"]:
        raise ValueError("dependency cohort history integrity failed")
    report = {
        "inventory_sha256": pin,
        "reviewed": count,
        "deferred": len(documents) - count,
        "selected": [item["identity"] for item in selected],
        "review_report_sha256": sha(json_bytes(result)),
        "publication_approved": False,
    }
    atomic_private(output / "quantum-report.json", json_bytes(report))
    atomic_private(
        checkpoint,
        json_bytes(
            {
                "version": VERSION,
                "inventory_sha256": pin,
                "next_index": (start + count) % len(documents),
                "documents": len(documents),
            }
        ),
    )
    return report
