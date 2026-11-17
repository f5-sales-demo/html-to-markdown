"""Resumable capture sharing; source records and live collectors remain independent."""

from __future__ import annotations

import fcntl
import json
import sqlite3
from pathlib import Path
from typing import Any

from .capture_payloads import capture_payloads
from .capture_store import capture_digest, share_file
from .content_inventory import atomic_private
from .curation import json_bytes
from .enrichment.analysis import sha
from .urls import SOURCE_ROOTS


def _files(root: Path, source: str) -> dict[Path, str]:
    files: dict[Path, str] = {}
    for database in sorted(root.glob("*/" + source + "/inventory.sqlite")):
        if any(path.is_symlink() for path in (database, *database.parents)):
            raise ValueError("unsafe capture storage inventory path")
        connection = sqlite3.connect("file:" + str(database) + "?mode=ro", uri=True)
        try:
            connection.execute("BEGIN")
            for row in connection.execute("SELECT data FROM documents"):
                item = json.loads(row[0])
                for name, expected in capture_payloads(item).items():
                    path = database.parent / name
                    if path in files and files[path] != expected:
                        raise ValueError("conflicting capture payload digests")
                    files[path] = expected
        finally:
            connection.close()
        for family, pattern in (("responses", "*.body"), ("browser-evidence", "*.html")):
            for path in (database.parent / family).glob(pattern):
                named_digest = capture_digest(path.relative_to(database.parent))
                if named_digest is not None:
                    files[path] = named_digest
    return files


def _share_source(root: Path, output: Path, source: str) -> dict[str, Any]:
    checkpoint = output / (source + ".private.json")
    try:
        files = _files(root, source)
    except (OSError, ValueError, sqlite3.Error) as failure:
        if checkpoint.exists():
            atomic_private(
                output / "history" / (sha(checkpoint.read_bytes()) + ".json"),
                checkpoint.read_bytes(),
            )
        atomic_private(
            output / "errors" / (source + ".private.json"),
            json_bytes({"error": str(failure), "stage": "enumeration"}),
        )
        atomic_private(
            checkpoint,
            json_bytes(
                {
                    "schema_version": 1,
                    "source": source,
                    "state": "interrupted",
                    "stage": "enumeration",
                    "error_class": type(failure).__name__,
                    "source_records_changed": False,
                    "model_inference_run": False,
                }
            ),
        )
        raise
    inventory_pin = sha(
        json_bytes(
            {path.relative_to(root).as_posix(): value for path, value in sorted(files.items())}
        )
    )
    previous = json.loads(checkpoint.read_bytes()) if checkpoint.exists() else {}
    if previous and previous.get("inventory_sha256") != inventory_pin:
        atomic_private(
            output / "history" / (sha(checkpoint.read_bytes()) + ".json"), checkpoint.read_bytes()
        )
        previous = {}
    state = {
        "schema_version": 1,
        "source": source,
        "inventory_sha256": inventory_pin,
        "available_files": len(files),
        "processed_files": previous.get("processed_files", 0),
        "shared_files": previous.get("shared_files", 0),
        "minimum_reclaimed_bytes": previous.get("minimum_reclaimed_bytes", 0),
        "last_file": previous.get("last_file", ""),
        "state": "processing",
        "source_records_changed": False,
        "model_inference_run": False,
    }
    store = root / ".capture-store"
    try:
        for path, expected in sorted(files.items()):
            relative = path.relative_to(root).as_posix()
            if relative <= state["last_file"]:
                continue
            before = path.stat()
            share_file(store, path, expected)
            after = path.stat()
            cached = (store / "objects" / expected).stat()
            if before.st_ino != after.st_ino and (cached.st_dev, cached.st_ino) == (
                after.st_dev,
                after.st_ino,
            ):
                state["shared_files"] += 1
                if before.st_nlink == 1:
                    state["minimum_reclaimed_bytes"] += before.st_blocks * 512
            state["processed_files"] += 1
            state["last_file"] = relative
            if state["processed_files"] % 128 == 0:
                atomic_private(checkpoint, json_bytes(state))
        state["state"] = "complete"
    except (OSError, ValueError) as failure:
        state.update(state="interrupted", error_class=type(failure).__name__)
        atomic_private(
            output / "errors" / (source + ".private.json"),
            json_bytes({"error": str(failure), "next_file": relative}),
        )
        raise
    finally:
        atomic_private(checkpoint, json_bytes(state))
    return state


def share_capture_storage(root: Path, output: Path, *, source: str = "all") -> dict[str, Any]:
    """Defer active source collectors and replay completed maintenance without inference."""
    if source != "all" and source not in SOURCE_ROOTS:
        raise ValueError("unknown capture storage source")
    if any(path.is_symlink() for path in (root, output, *root.parents, *output.parents)):
        raise ValueError("unsafe capture storage maintenance path")
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    results = []
    for name in SOURCE_ROOTS if source == "all" else [source]:
        lock_path = root / (name + ".collector.lock")
        if lock_path.is_symlink():
            raise ValueError("unsafe capture collector lock")
        with lock_path.open("a+b") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                results.append({"source": name, "state": "deferred_active_collector"})
                continue
            results.append(_share_source(root, output, name))
    return {
        "schema_version": 1,
        "sources": results,
        "model_inference_run": False,
        "captures_journals_and_receipts_preserved": True,
    }
