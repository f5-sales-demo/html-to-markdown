"""Select acknowledged source checkpoints for baseline dependency replacements."""

from __future__ import annotations

import json
from pathlib import Path

from .content_inventory import atomic_private
from .curation import json_bytes
from .enrichment.analysis import sha
from .source_inputs import MARKER, verify_source
from .urls import SOURCE_ROOTS


def selected_inputs(output: Path) -> dict[str, tuple[Path, str]]:
    selected: dict[str, tuple[Path, str]] = {}
    ranks: dict[str, str] = {}
    errors = []
    for checkpoint in sorted(output.glob("*/*/checkpoint.json")):
        try:
            if checkpoint.is_symlink():
                raise ValueError("unsafe admitted source checkpoint")
            pin = json.loads(checkpoint.read_bytes()).get("returned_admission_sha256")
            if not pin:
                continue
            matches = [
                p.parent
                for p in checkpoint.parent.glob("sealed/*/" + MARKER)
                if not p.is_symlink() and sha(p.read_bytes()) == pin
            ]
            if not matches:
                raise ValueError("acknowledged source checkpoint lacks an exact sealed input")
            receipt = verify_source(matches[0], pin)
            source = receipt["source"]
            if source not in SOURCE_ROOTS:
                raise ValueError("admitted source checkpoint selects an unknown source")
            if source not in selected or receipt["ended_at"] > ranks[source]:
                selected[source], ranks[source] = (matches[0], pin), receipt["ended_at"]
        except (ValueError, KeyError, TypeError, OSError) as failure:
            errors.append(
                {
                    "checkpoint": checkpoint.relative_to(output).as_posix(),
                    "error_class": type(failure).__name__,
                    "reason": "source_input_verification_failed",
                }
            )
    atomic_private(
        output / "dependency-source-inputs.json",
        json_bytes(
            {
                "sources": {
                    source: {"path": str(path.relative_to(output)), "sha256": pin}
                    for source, (path, pin) in selected.items()
                },
                "errors": errors,
            }
        ),
    )
    return selected
