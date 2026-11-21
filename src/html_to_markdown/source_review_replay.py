"""Bounded independent source receipt replay with immutable isolated inputs."""

from __future__ import annotations

import json
import tempfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .content_inventory import atomic_private
from .enrichment.analysis import sha
from .review_controller_compatibility import replay_provenance

Replay = Callable[[Path, Path, Path, str, Any], dict[str, Any]]


def _verify_review_record(
    capture: Path, output: Path, record: dict[str, Any], replay: Replay
) -> tuple[str, dict[str, str]] | None:
    if "path" not in record:
        return None
    path = output / record["path"]
    saved = json.loads(path.read_bytes())
    if saved["disposition"] not in {"accepted", "policy_excluded"}:
        return None
    # Replay into an isolated copy so verification never repairs its target.
    with tempfile.TemporaryDirectory(prefix="source-review-replay-") as directory:
        scratch = Path(directory)
        if (output / "media").exists():
            digests = {media["sha256"] for media in saved["item"]["media"]}
            digests.update(saved.get("file_hashes", {}).values())
            for digest in sorted(digests):
                for version in (2, 3):
                    cache = output / "media" / f"{digest}-native-{version}.json"
                    if cache.is_file():
                        atomic_private(scratch / "media" / cache.name, cache.read_bytes())
        copy = scratch / "document.json"
        atomic_private(copy, path.read_bytes())
        actual = replay(capture, scratch, copy, "replay", None)
        if actual != saved:
            raise ValueError("source admission replay differs from receipt")
    expected = {}
    if saved["disposition"] == "accepted":
        for relative, digest in saved["file_hashes"].items():
            relative = Path(relative).relative_to("articles").as_posix()
            if sha((output / "reviewed" / relative).read_bytes()) != digest:
                raise ValueError("reviewed article or media bytes changed")
            expected[relative] = digest
    return saved["binding"]["request_context_sha256"], expected


def verify_source_reviews(capture: Path, output: Path, replay: Replay) -> dict[str, Any]:
    """Replay independent complete admissions concurrently; inspect every retained byte."""
    inventory = json.loads((output / "review-inventory.json").read_bytes())
    expected = {}
    contexts: set[str] = set()
    with ThreadPoolExecutor(max_workers=4) as pool:
        try:
            # map yields original inventory order, independent of completion order.
            for result in pool.map(
                lambda record: _verify_review_record(capture, output, record, replay),
                inventory["documents"],
            ):
                if result is not None:
                    context, files = result
                    contexts.add(context)
                    expected.update(files)
        except BaseException:
            pool.shutdown(wait=True, cancel_futures=True)
            raise
    actual = {
        path.relative_to(output / "reviewed").as_posix(): sha(path.read_bytes())
        for path in (output / "reviewed").rglob("*")
        if path.is_file()
    }
    if expected != actual:
        raise ValueError("source reviewed inventory differs from accepted admissions")
    return {
        "accepted_files": expected,
        "inventory_sha256": sha((output / "review-inventory.json").read_bytes()),
        **replay_provenance(contexts),
    }
