"""Deterministic combined release bundle creation."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import mimetypes
import re
import tarfile
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from sqlite3 import Row
from typing import Any

from . import __version__
from .content_policy import migrate_content, validate_content_policy
from .curation import corpus_digest, json_bytes, validate_curation
from .models import PageMetadata, PageStatus
from .quality import write_quality_reports
from .render import content_hash, split_document
from .state import StateStore
from .urls import SOURCE_ROOTS
from .validation import KnownDocument, validate_enriched_metadata, validate_related_targets

MIB = 1024 * 1024
MAX_ARCHIVE_BYTES = 1024 * MIB
MAX_EXPANDED_BYTES = 1024 * MIB
MAX_MEMBERS = 20_000
MAX_MARKDOWN_BYTES = 2 * MIB
MAX_ASSET_BYTES = 20 * MIB
MANIFEST_SCHEMA_VERSION = 2
PUBLICATION_SCHEMA_VERSION = 1
RELEASE_ASSET_NAMES = (
    "html-to-markdown-content.tar.gz",
    "html-to-markdown-content.tar.gz.sha256",
    "manifest.json",
    "quality-report.json",
    "quality-report.md",
)
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
TAG_PATTERN = re.compile(r"^content-[0-9]{8}T[0-9]{6}Z$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
TIMESTAMP_PATTERN = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _media_type(path: Path) -> str:
    value, _ = mimetypes.guess_type(path.name)
    return value or "application/octet-stream"


def _safe_member_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    raw_invalid = not value or value.startswith("/") or "\\" in value
    segment_invalid = path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts)
    encoded = value.casefold()
    if raw_invalid or segment_invalid or "%2f" in encoded or "%5c" in encoded:
        raise ValueError(f"archive contains an invalid member path: {value}")
    return path


def _is_payload_name(name: str) -> bool:
    if name in {"manifest.json", "quality-report.json", "quality-report.md", "SHA256SUMS"}:
        return True
    parts = PurePosixPath(name).parts
    if len(parts) < 4 or parts[0] != "content":
        return False
    if parts[1] not in SOURCE_ROOTS:
        return False
    return name.endswith("/index.md") or ("assets" in parts[2:-1] and len(parts[-1]) > 1)


def _validate_payload_size(path: Path, relative: str) -> None:
    size = path.stat().st_size
    if relative.endswith("/index.md") and size > MAX_MARKDOWN_BYTES:
        raise ValueError(f"Markdown member exceeds {MAX_MARKDOWN_BYTES} bytes: {relative}")
    if "/assets/" in relative and size > MAX_ASSET_BYTES:
        raise ValueError(f"asset member exceeds {MAX_ASSET_BYTES} bytes: {relative}")


def _parse_sums(value: bytes) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        lines = value.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise ValueError("SHA256SUMS is not UTF-8") from error
    for line in lines:
        fields = line.split("  ", 1)
        if len(fields) != 2 or not SHA256_PATTERN.fullmatch(fields[0]):
            raise ValueError("SHA256SUMS contains a malformed entry")
        name = fields[1]
        _safe_member_path(name)
        if name in result:
            raise ValueError(f"SHA256SUMS contains a duplicate entry: {name}")
        result[name] = fields[0]
    return result


def _validate_document_entry(
    document: object,
    files: dict[str, bytes],
    seen: set[str],
    known_urls: set[str],
    require_enriched: bool,
) -> None:
    if not isinstance(document, dict):
        raise ValueError("manifest document entry must be an object")
    path = document.get("path")
    if isinstance(path, str) and path in seen:
        raise ValueError(f"duplicate manifest document path: {path}")
    if not isinstance(path, str) or path not in files or not path.endswith("/index.md"):
        raise ValueError(f"manifest document is missing: {path}")
    seen.add(path)
    data = files[path]
    try:
        metadata, body = split_document(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise ValueError(f"manifest document is invalid: {path}") from error
    if require_enriched:
        metadata_errors = validate_enriched_metadata(metadata, path, known_urls)
        if metadata_errors:
            raise ValueError(metadata_errors[0])
    if metadata.get("url") != document.get("url"):
        raise ValueError(f"manifest document URL mismatch: {path}")
    if document.get("body_sha256") != content_hash(body):
        raise ValueError(f"manifest document body hash mismatch: {path}")
    if document.get("file_sha256") != hashlib.sha256(data).hexdigest():
        raise ValueError(f"manifest document file hash mismatch: {path}")
    if document.get("size_bytes") != len(data):
        raise ValueError(f"manifest document size mismatch: {path}")


def _validate_asset_entry(asset: object, files: dict[str, bytes], seen: set[str]) -> None:
    if not isinstance(asset, dict):
        raise ValueError("manifest asset entry must be an object")
    path = asset.get("path")
    if not isinstance(path, str) or path in seen or path not in files or "/assets/" not in path:
        raise ValueError(f"manifest asset is missing or duplicated: {path}")
    seen.add(path)
    data = files[path]
    if asset.get("sha256") != hashlib.sha256(data).hexdigest():
        raise ValueError(f"manifest asset hash mismatch: {path}")
    if asset.get("size_bytes") != len(data):
        raise ValueError(f"manifest asset size mismatch: {path}")
    if asset.get("media_type") != _media_type(Path(path)):
        raise ValueError(f"manifest asset media type mismatch: {path}")


# Manifest validation deliberately checks the complete closed archive graph.
# pylint: disable-next=too-many-branches,too-many-locals
def _validate_manifest(manifest: object, files: dict[str, bytes]) -> None:
    if not isinstance(manifest, dict) or manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ValueError("manifest schema version is invalid")
    from .enrichment.graph import validate_aliases  # pylint: disable=import-outside-toplevel

    validate_aliases(manifest)
    documents = manifest.get("documents")
    assets = manifest.get("assets")
    if not isinstance(documents, list) or not isinstance(assets, list):
        raise ValueError("manifest documents and assets must be lists")
    seen: set[str] = set()
    known_urls: set[str] = set()
    metadata_schema_values: list[object] = []
    parsed_documents: dict[str, PageMetadata] = {}
    known_documents: dict[str, KnownDocument] = {}
    for document in documents:
        if not isinstance(document, dict):
            continue
        path = document.get("path")
        if not isinstance(path, str) or path not in files:
            continue
        try:
            metadata, _ = split_document(files[path].decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue
        metadata_schema_values.append(metadata.get("metadata_schema"))
        canonical_url = metadata.get("canonical_url")
        if isinstance(canonical_url, str):
            known_urls.add(canonical_url)
            try:
                parsed = PageMetadata.model_validate(metadata)
                source = str(parsed.source_id)
                stable = (
                    PurePosixPath(path)
                    .relative_to(PurePosixPath("content") / source)
                    .parent.as_posix()
                )
                parsed_documents[path] = parsed
                if parsed.task_type is not None:
                    known_documents[canonical_url] = (
                        source,
                        stable,
                        parsed.title,
                        parsed.task_type.value,
                    )
            except ValueError:
                pass
        elif isinstance(document.get("url"), str):
            known_urls.add(str(document["url"]))
    require_enriched = any(value is not None for value in metadata_schema_values)
    if require_enriched and any(value != 1 for value in metadata_schema_values):
        raise ValueError("archive mixes enriched and legacy document metadata")
    for document in documents:
        _validate_document_entry(document, files, seen, known_urls, require_enriched)
        if require_enriched and isinstance(document, dict):
            path = document.get("path")
            if isinstance(path, str) and path in parsed_documents:
                relationship_errors = validate_related_targets(
                    parsed_documents[path], path, known_documents
                )
                if relationship_errors:
                    raise ValueError(relationship_errors[0])
    for asset in assets:
        _validate_asset_entry(asset, files, seen)
    if manifest.get("page_count") != len(documents) or manifest.get("asset_count") != len(assets):
        raise ValueError("manifest counts do not match entries")
    content_names = {name for name in files if name.startswith("content/")}
    if seen != content_names:
        raise ValueError("manifest content member set mismatch")


def _document_entries(output: Path, rows: list[Row]) -> list[dict[str, object]]:
    accepted = {PageStatus.SUCCESS, PageStatus.CARRIED_FORWARD, PageStatus.REMOVAL_CANDIDATE}
    documents: list[dict[str, object]] = []
    for row in rows:
        if PageStatus(row["status"]) not in accepted:
            continue
        current_failure = None
        if row["current_failure_classification"]:
            current_failure = {
                "classification": row["current_failure_classification"],
                "error_class": row["error_class"],
                "error_message": row["error_message"],
            }
        path = output / row["output_path"]
        raw = path.read_bytes()
        _, body = split_document(raw.decode("utf-8"))
        documents.append(
            {
                "sourceId": row["source"],
                "url": row["canonical_url"],
                "path": row["output_path"],
                "body_sha256": content_hash(body),
                "file_sha256": hashlib.sha256(raw).hexdigest(),
                "size_bytes": len(raw),
                "provenance": {
                    "freshness": row["freshness"] or "fresh",
                    "current_failure": current_failure,
                    "last_success_at": row["last_success_at"],
                    "consecutive_failure_count": row["consecutive_failure_count"],
                    "terminal_confirmation_count": row["terminal_confirmation_count"],
                },
            }
        )
    return documents


def _asset_entries(output: Path) -> list[dict[str, object]]:
    return [
        {
            "path": path.relative_to(output).as_posix(),
            "sha256": sha256_file(path),
            "media_type": _media_type(path),
            "size_bytes": path.stat().st_size,
        }
        for path in sorted(output.glob("content/*/**/assets/*"))
    ]


def build_manifest(
    output: Path, store: StateStore, started_at: str, ended_at: str
) -> dict[str, object]:
    migrate_content(output, store)
    if not (output / "quality-report.json").is_file():
        write_quality_reports(output)
    rows = store.rows()
    documents = _document_entries(output, rows)
    asset_entries = _asset_entries(output)
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
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "tool_version": __version__,
        "source_roots": SOURCE_ROOTS,
        "started_at": started_at,
        "ended_at": ended_at,
        "page_count": len(documents),
        "asset_count": len(asset_entries),
        "assets": asset_entries,
        "counts": counts,
        "quality_status_counts": quality_status_counts,
        "removals": [],
        "failures": [item for item in failures if item["url"] in {doc["url"] for doc in documents}],
        "documents": documents,
    }
    if (output / "enrichment-state.json").is_file():
        from .enrichment.engine import verify_enrichment  # pylint: disable=import-outside-toplevel

        state = verify_enrichment(output)
        manifest["enrichment"] = {
            "artifact_sha256": state["artifact_sha256"],
            "aliases": state["report"]["aliases"],
        }
    return manifest


# Packaging and its final integrity audit intentionally share one transaction.
# pylint: disable-next=too-many-locals,too-many-branches,too-many-statements
def write_release(output: Path, manifest: dict[str, object]) -> Path:
    validate_content_policy(output)
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
    files = content_files + report_files + [manifest_path]
    if len(files) + 1 > MAX_MEMBERS:
        raise ValueError(f"archive exceeds {MAX_MEMBERS} members")
    for path in files:
        _validate_payload_size(path, path.relative_to(output).as_posix())
    expanded_size = sum(path.stat().st_size for path in files)
    if expanded_size > MAX_EXPANDED_BYTES:
        raise ValueError(f"expanded payload exceeds {MAX_EXPANDED_BYTES} bytes")
    sums = [f"{sha256_file(path)}  {path.relative_to(output).as_posix()}" for path in files]
    sums_path = output / "SHA256SUMS"
    sums_path.write_text("\n".join(sums) + ("\n" if sums else ""), encoding="utf-8")
    archive = output / "html-to-markdown-content.tar.gz"
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for path in sorted(
            [*files, sums_path], key=lambda item: item.relative_to(output).as_posix()
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
    if archive.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError(f"archive exceeds {MAX_ARCHIVE_BYTES} bytes")
    verify_archive(archive)
    validate_curation(output, artifacts=True)
    audit_path = output / "curation-audit.json"
    if audit_path.is_file():
        audit = json.loads(audit_path.read_text())
        audit["output_sha256"] = corpus_digest(output)
        audit["artifact_digests"] = {
            name: sha256_file(output / name) for name in (*RELEASE_ASSET_NAMES, "SHA256SUMS")
        }
        audit_path.write_bytes(json_bytes(audit))
    return archive


def _read_archive_files(archive: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    seen_names: set[str] = set()
    expanded = 0
    with tarfile.open(archive, mode="r|gz") as packaged:
        member_count = 0
        for member in packaged:
            member_count += 1
            if member_count > MAX_MEMBERS:
                raise ValueError("archive member count is invalid")
            path = _safe_member_path(member.name)
            if member.name in seen_names:
                raise ValueError(f"archive contains a duplicate member: {member.name}")
            seen_names.add(member.name)
            if not member.isfile():
                if member.isdir():
                    continue
                raise ValueError(f"archive member type is not allowed: {member.name}")
            if not _is_payload_name(member.name):
                raise ValueError(f"archive contains an unknown member: {member.name}")
            if member.name.endswith("/index.md") and member.size > MAX_MARKDOWN_BYTES:
                raise ValueError(
                    f"Markdown member exceeds {MAX_MARKDOWN_BYTES} bytes: {member.name}"
                )
            if "/assets/" in member.name and member.size > MAX_ASSET_BYTES:
                raise ValueError(f"asset member exceeds {MAX_ASSET_BYTES} bytes: {member.name}")
            expanded += member.size
            if expanded > MAX_EXPANDED_BYTES:
                raise ValueError(f"expanded payload exceeds {MAX_EXPANDED_BYTES} bytes")
            handle = packaged.extractfile(member)
            if handle is None:
                raise ValueError(f"archive member cannot be read: {path}")
            files[member.name] = handle.read()
        if member_count == 0:
            raise ValueError("archive member count is invalid")
    return files


def verify_archive(archive: Path) -> None:
    if archive.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError(f"archive exceeds {MAX_ARCHIVE_BYTES} bytes")
    files = _read_archive_files(archive)
    if "SHA256SUMS" not in files or "manifest.json" not in files:
        raise ValueError("archive is missing required metadata")
    sums = _parse_sums(files["SHA256SUMS"])
    expected = set(files) - {"SHA256SUMS"}
    if set(sums) != expected:
        raise ValueError("SHA256SUMS member set mismatch")
    for name, expected_digest in sums.items():
        if hashlib.sha256(files[name]).hexdigest() != expected_digest:
            raise ValueError(f"SHA256SUMS digest mismatch: {name}")
    try:
        manifest = json.loads(files["manifest.json"])
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("manifest is not valid JSON") from error
    _validate_manifest(manifest, files)


def write_publication_receipt(
    output: Path,
    *,
    tag: str,
    source_commit: str,
    created_at: str,
    published_at: str,
) -> Path:
    if not TAG_PATTERN.fullmatch(tag):
        raise ValueError("release tag is invalid")
    if not COMMIT_PATTERN.fullmatch(source_commit):
        raise ValueError("source commit is invalid")
    if not TIMESTAMP_PATTERN.fullmatch(created_at) or not TIMESTAMP_PATTERN.fullmatch(published_at):
        raise ValueError("publication timestamps must be UTC")
    assets: list[dict[str, object]] = []
    for name in RELEASE_ASSET_NAMES:
        path = output / name
        if not path.is_file():
            raise ValueError(f"publication asset is missing: {name}")
        assets.append(
            {"name": name, "size_bytes": path.stat().st_size, "sha256": sha256_file(path)}
        )
    receipt = {
        "schema_version": PUBLICATION_SCHEMA_VERSION,
        "release_tag": tag,
        "source_commit": source_commit,
        "created_at": created_at,
        "published_at": published_at,
        "assets": assets,
    }
    path = output / "publication.json"
    path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    verify_publication_receipt(
        output,
        receipt,
        expected_tag=tag,
        expected_source_commit=source_commit,
        actual_asset_names={*RELEASE_ASSET_NAMES, "publication.json"},
    )
    return path


def verify_publication_receipt(
    output: Path,
    receipt: object,
    *,
    expected_tag: str,
    expected_source_commit: str | None = None,
    expected_receipt_sha256: str | None = None,
    actual_asset_names: Iterable[str],
) -> None:
    if not isinstance(receipt, dict) or receipt.get("schema_version") != PUBLICATION_SCHEMA_VERSION:
        raise ValueError("publication receipt schema is invalid")
    if receipt.get("release_tag") != expected_tag:
        raise ValueError("publication receipt tag mismatch")
    source_commit = receipt.get("source_commit")
    if not isinstance(source_commit, str) or not COMMIT_PATTERN.fullmatch(source_commit):
        raise ValueError("publication receipt source commit is invalid")
    if expected_source_commit is not None and source_commit != expected_source_commit:
        raise ValueError("publication receipt source commit mismatch")
    receipt_path = output / "publication.json"
    if expected_receipt_sha256 is not None and (
        not receipt_path.is_file() or sha256_file(receipt_path) != expected_receipt_sha256
    ):
        raise ValueError("publication receipt digest mismatch")
    expected_names = set(RELEASE_ASSET_NAMES) | {"publication.json"}
    if set(actual_asset_names) != expected_names:
        raise ValueError("publication asset set mismatch")
    assets = receipt.get("assets")
    if not isinstance(assets, list):
        raise ValueError("publication assets must be a list")
    entries: dict[str, dict[str, Any]] = {}
    for asset in assets:
        if not isinstance(asset, dict) or not isinstance(asset.get("name"), str):
            raise ValueError("publication asset entry is invalid")
        name = asset["name"]
        if name in entries:
            raise ValueError(f"publication asset is duplicated: {name}")
        entries[name] = asset
    if set(entries) != set(RELEASE_ASSET_NAMES):
        raise ValueError("publication receipt asset set mismatch")
    for name in RELEASE_ASSET_NAMES:
        path = output / name
        if not path.is_file():
            raise ValueError(f"publication asset is missing: {name}")
        if entries[name].get("size_bytes") != path.stat().st_size:
            raise ValueError(f"publication asset size mismatch: {name}")
        if entries[name].get("sha256") != sha256_file(path):
            raise ValueError(f"publication asset digest mismatch: {name}")
