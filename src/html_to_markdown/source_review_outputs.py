"""Preserve private emitted bytes when an admission becomes stale."""

from pathlib import Path
from typing import Any

from .content_inventory import atomic_private
from .enrichment.analysis import sha


def retire_output(output: Path, saved: dict[str, Any]) -> None:
    """Preserve previously emitted bytes privately when admission becomes stale."""
    for relative in saved.get("file_hashes", {}):
        path = output / "reviewed" / Path(relative).relative_to("articles")
        if path.is_file():
            data = path.read_bytes()
            atomic_private(output / "history" / "published" / sha(data), data)
            path.unlink()
