"""Private deterministic retirement/API previews preserve admitted upstream bytes."""

from __future__ import annotations

import fcntl
import json
import tempfile
from pathlib import Path
from typing import Any

from .content_inventory import atomic_private
from .content_policy import load_content_policy, migrate_content, validate_content_policy
from .curation import corpus_digest, json_bytes, load_curation_policy
from .enrichment.analysis import sha
from .render import split_document


class CurationRequired(ValueError):
    reason = "source_curation_requires_review"


def record_exception(output: Path, source_pin: str, metadata: dict[str, Any]) -> None:
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (output / "curation-exceptions.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = output / "curation-exceptions.json"
        saved = json.loads(path.read_bytes()) if path.exists() else {}
        saved[source_pin] = {
            "sourceId": metadata["sourceId"],
            "url": metadata["url"],
            "source_files_sha256": source_pin,
            "reason": CurationRequired.reason,
            "curation_policy_sha256": load_curation_policy().sha256,
            "api_policy_sha256": load_content_policy().policy_digest,
        }
        atomic_private(path, json_bytes(saved))


def _record_preview(output: Path, source_pin: str, preview: dict[str, Any]) -> None:
    destination = output / "curation" / (source_pin + ".json")
    if destination.exists():
        previous = destination.read_bytes()
        if previous != json_bytes(preview):
            atomic_private(output / "curation-history" / (sha(previous) + ".json"), previous)
    atomic_private(destination, json_bytes(preview))


def _resolve_exception(output: Path, source_pin: str) -> None:
    path = output / "curation-exceptions.json"
    if not path.exists():
        return
    with (output / "curation-exceptions.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        saved = json.loads(path.read_bytes())
        previous = saved.pop(source_pin, None)
        if previous is not None:
            data = json_bytes(previous)
            atomic_private(output / "curation-history" / (sha(data) + ".json"), data)
            atomic_private(path, json_bytes(saved))


def preview_curation(output: Path, files: dict[str, bytes], article: str) -> dict[str, Any]:
    """Rebuild the actual planner; cached previews never authorize transformations."""
    relative_files = {}
    for name, data in sorted(files.items()):
        path = Path(name)
        if path.is_absolute() or ".." in path.parts or path.parts[:2] != ("articles", "content"):
            raise ValueError("unsafe source curation input path")
        relative_files[path.relative_to("articles").as_posix()] = data
    if article not in files or not article.endswith("/index.md"):
        raise ValueError("source curation article is absent")
    source_pin = sha(json_bytes({name: sha(data) for name, data in sorted(files.items())}))
    article_path = Path(article).relative_to("articles").as_posix()
    metadata, _ = split_document(files[article].decode())
    with tempfile.TemporaryDirectory(prefix="admission-curation-") as directory:
        root = Path(directory)
        for name, data in relative_files.items():
            atomic_private(root / name, data)
        migrate_content(root)
        audit = json.loads((root / "curation-audit.json").read_bytes())
        destination = root / article_path
        if not destination.is_file():
            # A planner omission from stale/missing review is an exception, never
            # independently justified source exclusion.
            _record_preview(output, source_pin, audit)
            record_exception(output, source_pin, metadata)
            raise CurationRequired(CurationRequired.reason)
        validate_content_policy(root)
        final_meta, final_body = split_document(destination.read_text())
        final_files = {
            path.relative_to(root).as_posix(): sha(path.read_bytes())
            for path in sorted((root / "content").rglob("*"))
            if path.is_file()
        }
        preview = {
            "source_files_sha256": source_pin,
            "curation_policy_sha256": load_curation_policy().sha256,
            "api_policy_sha256": load_content_policy().policy_digest,
            "output_sha256": corpus_digest(root),
            "file_hashes": final_files,
            "metadata": final_meta,
            "body": final_body,
            "audit": audit,
        }
        preview["curation_sha256"] = sha(json_bytes(preview))
        _record_preview(output, source_pin, preview)
        _resolve_exception(output, source_pin)
        return preview
