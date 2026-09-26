"""Deterministic combined release bundle creation."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile
from pathlib import Path

from . import __version__
from .models import PageStatus
from .state import StateStore
from .urls import SOURCE_ROOTS


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest(
    output: Path, store: StateStore, started_at: str, ended_at: str
) -> dict[str, object]:
    rows = store.rows()
    documents = [
        {
            "sourceId": row["source"],
            "url": row["canonical_url"],
            "path": row["output_path"],
            "sha256": row["content_hash"],
        }
        for row in rows
        if row["status"] == PageStatus.SUCCESS
    ]
    assets = list(output.glob("content/*/**/assets/*"))
    removals = [
        {"url": row["canonical_url"], "classification": row["status"]}
        for row in rows
        if PageStatus(row["status"]).terminal_removal
    ]
    failures = [
        {"url": row["canonical_url"], "classification": row["status"], "error": row["error_class"]}
        for row in rows
        if row["status"]
        not in {PageStatus.SUCCESS, PageStatus.REMOVED_NOT_FOUND, PageStatus.REMOVED_NAVIGATION}
    ]
    return {
        "tool_version": __version__,
        "source_roots": SOURCE_ROOTS,
        "started_at": started_at,
        "ended_at": ended_at,
        "page_count": len(documents),
        "asset_count": len(assets),
        "removals": removals,
        "failures": failures,
        "documents": documents,
    }


def write_release(output: Path, manifest: dict[str, object]) -> Path:
    manifest_path = output / "manifest.json"
    if manifest_path.exists():
        try:
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
            ignored = {"started_at", "ended_at"}
            previous_content = {key: value for key, value in previous.items() if key not in ignored}
            current_content = {key: value for key, value in manifest.items() if key not in ignored}
            if previous_content == current_content:
                manifest = previous
        except (OSError, json.JSONDecodeError):
            pass
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    files = sorted(output.glob("content/*/**/index.md")) + sorted(
        output.glob("content/*/**/assets/*")
    )
    sums = [f"{sha256_file(path)}  {path.relative_to(output).as_posix()}" for path in files]
    sums_path = output / "SHA256SUMS"
    sums_path.write_text("\n".join(sums) + ("\n" if sums else ""), encoding="utf-8")
    archive = output / "html-to-markdown-content.tar.gz"
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for path in sorted(
            [*files, manifest_path, sums_path], key=lambda item: item.relative_to(output).as_posix()
        ):
            info = tar.gettarinfo(str(path), arcname=path.relative_to(output).as_posix())
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            with path.open("rb") as source_handle:
                tar.addfile(info, source_handle)
    with (
        archive.open("wb") as output_handle,
        gzip.GzipFile(filename="", mode="wb", fileobj=output_handle, mtime=0) as zipped,
    ):
        zipped.write(payload.getvalue())
    archive.with_name(f"{archive.name}.sha256").write_text(
        f"{sha256_file(archive)}  {archive.name}\n", encoding="utf-8"
    )
    return archive
