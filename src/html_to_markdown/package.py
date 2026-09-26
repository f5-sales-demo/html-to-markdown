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
from .quality import write_quality_reports
from .render import split_document
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
    document_statuses = {
        PageStatus.SUCCESS,
        PageStatus.CARRIED_FORWARD,
        PageStatus.REMOVAL_CANDIDATE,
    }
    documents = []
    for row in rows:
        status = PageStatus(row["status"])
        if status not in document_statuses:
            continue
        current_failure = None
        if row["current_failure_classification"]:
            current_failure = {
                "classification": row["current_failure_classification"],
                "error_class": row["error_class"],
                "error_message": row["error_message"],
            }
        documents.append(
            {
                "sourceId": row["source"],
                "url": row["canonical_url"],
                "path": row["output_path"],
                "sha256": row["content_hash"],
                "provenance": {
                    "freshness": row["freshness"] or "fresh",
                    "current_failure": current_failure,
                    "last_success_at": row["last_success_at"],
                    "consecutive_failure_count": row["consecutive_failure_count"],
                    "terminal_confirmation_count": row["terminal_confirmation_count"],
                },
            }
        )
    assets = list(output.glob("content/*/**/assets/*"))
    asset_entries = [
        {"path": path.relative_to(output).as_posix(), "sha256": sha256_file(path)}
        for path in sorted(assets)
    ]
    removals = [
        {"url": row["canonical_url"], "classification": row["status"]}
        for row in rows
        if PageStatus(row["status"]) == PageStatus.CONFIRMED_REMOVAL
    ]
    failures = [
        {"url": row["canonical_url"], "classification": row["status"], "error": row["error_class"]}
        for row in rows
        if PageStatus(row["status"])
        in {PageStatus.UNAVAILABLE, PageStatus.CARRIED_FORWARD, PageStatus.REMOVAL_CANDIDATE}
    ]
    count_names = (
        PageStatus.SUCCESS,
        PageStatus.CARRIED_FORWARD,
        PageStatus.UNAVAILABLE,
        PageStatus.REMOVAL_CANDIDATE,
        PageStatus.CONFIRMED_REMOVAL,
    )
    counts = {
        ("fresh" if status == PageStatus.SUCCESS else status.value): sum(
            row["status"] == status for row in rows
        )
        for status in count_names
    }
    quality_path = output / "quality-report.json"
    quality = json.loads(quality_path.read_text(encoding="utf-8")) if quality_path.is_file() else {}
    quality_summary = quality.get("summary", {})
    quality_status_counts = (
        quality_summary.get("status_counts", {"not_compared": 0})
        if isinstance(quality_summary, dict)
        else {"not_compared": 0}
    )
    return {
        "tool_version": __version__,
        "source_roots": SOURCE_ROOTS,
        "started_at": started_at,
        "ended_at": ended_at,
        "page_count": len(documents),
        "asset_count": len(assets),
        "assets": asset_entries,
        "counts": counts,
        "quality_status_counts": quality_status_counts,
        "removals": removals,
        "failures": failures,
        "documents": documents,
    }


# Packaging and its final integrity audit intentionally share one transaction.
# pylint: disable-next=too-many-locals,too-many-branches,too-many-statements
def write_release(output: Path, manifest: dict[str, object]) -> Path:
    if (
        not (output / "quality-report.json").is_file()
        or not (output / "quality-report.md").is_file()
    ):
        write_quality_reports(output)
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
    content_files = sorted(output.glob("content/*/**/index.md")) + sorted(
        output.glob("content/*/**/assets/*")
    )
    report_files = [
        path
        for path in (output / "quality-report.json", output / "quality-report.md")
        if path.is_file()
    ]
    files = content_files + report_files
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
    with tarfile.open(archive, mode="r:gz") as packaged:
        members = packaged.getmembers()
        if not members or any(
            Path(member.name).is_absolute() or ".." in Path(member.name).parts for member in members
        ):
            raise ValueError("archive contains an invalid member")
    document_entries = manifest.get("documents", [])
    if not isinstance(document_entries, list):
        raise ValueError("manifest documents must be a list")
    document_paths: set[str] = set()
    for document in document_entries:
        if not isinstance(document, dict):
            raise ValueError("manifest document entry must be an object")
        path_value = document.get("path")
        if not isinstance(path_value, str) or not (output / path_value).is_file():
            raise ValueError(f"manifest document is missing: {path_value}")
        if path_value in document_paths:
            raise ValueError(f"duplicate manifest document path: {path_value}")
        document_paths.add(path_value)
        metadata, _ = split_document((output / path_value).read_text(encoding="utf-8"))
        if metadata.get("url") != document.get("url"):
            raise ValueError(f"manifest document URL mismatch: {path_value}")
        if metadata.get("content_hash") != document.get("sha256"):
            raise ValueError(f"manifest document hash mismatch: {path_value}")
    asset_values = manifest.get("assets", [])
    if not isinstance(asset_values, list):
        raise ValueError("manifest assets must be a list")
    for asset in asset_values:
        if not isinstance(asset, dict):
            raise ValueError("manifest asset entry must be an object")
        path_value = asset.get("path")
        if not isinstance(path_value, str) or not (output / path_value).is_file():
            raise ValueError(f"manifest asset is missing: {path_value}")
        if sha256_file(output / path_value) != asset.get("sha256"):
            raise ValueError(f"manifest asset hash mismatch: {path_value}")
    return archive
