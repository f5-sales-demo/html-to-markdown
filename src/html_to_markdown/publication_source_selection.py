"""Rank grounded private receipts before expensive complete source admission replay."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .enrichment.analysis import sha
from .reference_repair_inputs import safe_file
from .source_inputs import MARKER, read_source_metadata, verify_source


def _rank(receipt: dict[str, Any]) -> tuple[str, int]:
    return (
        receipt["ended_at"],
        sum(
            entry["disposition"] in {"accepted", "policy_excluded"}
            for entry in receipt["documents"]
        ),
    )


@dataclass(frozen=True)
class _Candidate:
    directory: Path
    digest: str
    source: str
    rank: tuple[str, int]
    queue: str

    def failure(self, error: Exception) -> dict[str, str]:
        return {
            "input_sha256": self.digest,
            "queue": self.queue,
            "reason": "source_input_verification_failed",
            "error_class": type(error).__name__,
        }


def select_source_inputs(
    queue_root: Path,
) -> tuple[dict[str, tuple[Path, str]], list[dict[str, str]], list[dict[str, str]]]:
    """Every selected input replays fully; failed candidates permit verified fallbacks."""
    candidates: dict[str, list[_Candidate]] = {}
    rejected = []
    for receipt_path in sorted((queue_root / ".reviewed").glob(f"*/*/*/{MARKER}")):
        relative = receipt_path.relative_to(queue_root).as_posix()
        safe_file(queue_root, relative)
        directory = receipt_path.parent
        digest = sha(receipt_path.read_bytes())
        if directory.name != digest:
            raise ValueError("private reviewed input directory does not match its pin")
        queue = directory.parent.parent.name + "/" + directory.parent.name
        try:
            receipt = read_source_metadata(directory, digest)
            if receipt["source"] != directory.parent.name:
                raise ValueError("source admission directory names a different source")
            candidate = _Candidate(directory, digest, receipt["source"], _rank(receipt), queue)
            candidates.setdefault(candidate.source, []).append(candidate)
        except (ValueError, KeyError, TypeError, OSError) as error:
            rejected.append(_Candidate(directory, digest, "", ("", 0), queue).failure(error))
    selected: dict[str, tuple[Path, str]] = {}
    superseded = []
    for source, choices in sorted(candidates.items()):
        # Stable sorting preserves the original first-path winner for equal ranks.
        for candidate in sorted(choices, key=lambda entry: entry.rank, reverse=True):
            if source in selected:
                superseded.append(
                    {
                        "input_sha256": candidate.digest,
                        "queue": candidate.queue,
                        "reason": "source_input_superseded",
                        "superseding_input_sha256": selected[source][1],
                    }
                )
                continue
            try:
                receipt = verify_source(candidate.directory, candidate.digest)
                if receipt["source"] != source or _rank(receipt) != candidate.rank:
                    raise ValueError("verified source selection metadata changed")
            except (ValueError, KeyError, TypeError, OSError) as error:
                rejected.append(candidate.failure(error))
                continue
            selected[source] = candidate.directory, candidate.digest
    return (
        selected,
        sorted(rejected, key=lambda entry: (entry["queue"], entry["input_sha256"])),
        sorted(superseded, key=lambda entry: (entry["queue"], entry["input_sha256"])),
    )
