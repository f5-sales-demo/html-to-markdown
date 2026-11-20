"""Prioritize current source inventories without discarding candidate history."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .content_inventory import atomic_private
from .curation import json_bytes
from .enrichment.analysis import sha


def _epoch(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return result.astimezone(UTC) if result.tzinfo is not None else None


def review_frontier(
    root: Path, records: list[dict[str, Any]], inventories: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """An older queue defers only to strict newer identity coverage in its source."""
    minimum = datetime.min.replace(tzinfo=UTC)
    sources = [record for record in records if record.get("kind") != "enrichment"]
    epochs = {
        record["queue"]: _epoch(inventories[record["queue"]]["started_at"]) for record in sources
    }
    ordered = sorted(
        sources,
        key=lambda record: (
            epochs[record["queue"]] is not None,
            epochs[record["queue"]] or minimum,
            record["queue"],
        ),
        reverse=True,
    )
    selected: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    uncertain: list[str] = []
    covered: dict[str, dict[str, tuple[datetime, str]]] = {}
    for record in ordered:
        name = record["queue"]
        epoch = epochs[name]
        identities = inventories[name]["identities"]
        prior = covered.setdefault(record["source"], {})
        proof = {
            "queue": name,
            "source": record["source"],
            "fingerprint": record["fingerprint"],
            "covered_identities": len(identities),
            "identity_inventory_sha256": sha(json_bytes(sorted(identities))),
        }
        if (
            epoch is not None
            and prior
            and all(identity in prior and prior[identity][0] > epoch for identity in identities)
        ):
            proof["covering_queues"] = sorted({prior[identity][1] for identity in identities})
            deferred.append(proof)
            continue
        selected.append(record)
        if epoch is None:
            uncertain.append(name)
        else:
            for identity in identities:
                if identity not in prior or epoch > prior[identity][0]:
                    prior[identity] = (epoch, name)
    enrichment = [record for record in records if record.get("kind") == "enrichment"]
    selected.extend(enrichment)
    atomic_private(
        root / "review-frontier.private.json",
        json_bytes(
            {
                "schema_version": 1,
                "selected": selected,
                "deferred": deferred,
                "uncertain": sorted(uncertain),
                "source_approval_granted": False,
                "historical_records_removed": False,
            }
        ),
    )
    return selected
