"""Receipt-pinned source refresh with whole-article publication fallbacks."""

# Deferred policy callbacks run after module initialization.
# pylint: disable=cyclic-import
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from .content_policy import load_content_policy, migrate_content, validate_content_policy
from .curation import corpus_digest, json_bytes, load_curation_policy
from .metadata import enrich_snapshot
from .models import DiscoveredPage, PageStatus
from .package import (
    RELEASE_ASSET_NAMES,
    _read_archive_files,
    build_manifest,
    sha256_file,
    verify_publication_receipt,
    write_release,
)
from .quality import write_quality_reports
from .render import split_document
from .state import StateStore
from .urls import stable_path
from .validation import validate_snapshot

MARKER = "refresh-state.json"
SOURCE = "docs-cloud-f5-com"
_BASELINES: dict[tuple[str, ...], dict[str, bytes]] = {}
_CANDIDATES: set[tuple[str, ...]] = set()


def _copy_article(source: Path, target: Path, relative: str) -> None:
    destination = target / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source / relative, destination)
    assets = (source / relative).parent / "assets"
    if assets.exists():
        shutil.copytree(assets, destination.parent / "assets")


def _verify_candidate(current: Path, candidate: Path, relative: str) -> None:
    key = (
        corpus_digest(candidate),
        sha256_file(current / relative),
        load_curation_policy().sha256,
        load_content_policy().policy_digest,
    )
    if key in _CANDIDATES:
        return
    with tempfile.TemporaryDirectory(prefix="refresh-check-") as temporary:
        replay = Path(temporary)
        _copy_article(current, replay, relative)
        migrate_content(replay)
        validate_content_policy(replay)
        if corpus_digest(replay) != corpus_digest(candidate):
            raise ValueError("fresh candidate differs from deterministic source curation")
    _CANDIDATES.add(key)


def _verified_baseline(directory: Path, receipt_sha256: str) -> dict[str, bytes]:
    key = (
        receipt_sha256,
        *(sha256_file(directory / name) for name in (*RELEASE_ASSET_NAMES, "publication.json")),
    )
    if key in _BASELINES:
        return _BASELINES[key]
    receipt = json.loads((directory / "publication.json").read_text())
    verify_publication_receipt(
        directory,
        receipt,
        expected_tag=receipt["release_tag"],
        expected_receipt_sha256=receipt_sha256,
        actual_asset_names=[*RELEASE_ASSET_NAMES, "publication.json"],
    )
    files = _read_archive_files(directory / "html-to-markdown-content.tar.gz")
    _BASELINES[key] = files
    return files


# Keep both upstreams and the complete selected graph in one preservation audit.
# pylint: disable-next=too-many-locals,too-many-branches,too-many-statements
def verify_refresh(output: Path) -> dict[str, Any]:
    """Check both upstream inputs and every selected body/asset on each replay."""
    state: dict[str, Any] = json.loads((output / MARKER).read_text())
    root = output / ".refresh"
    published = _verified_baseline(root / "baseline", state["baseline_receipt_sha256"])
    current = root / "current"
    if sha256_file(current / "inventory.json") != state["inventory_sha256"]:
        raise ValueError("fresh inventory evidence changed")
    inventory = json.loads((current / "inventory.json").read_text())
    confirmed = {
        item.get("path"): item
        for item in inventory["documents"]
        if item.get("status") == "success" and item.get("path")
    }
    if corpus_digest(current) != state["input_corpus_sha256"]:
        raise ValueError("fresh captured inputs changed")
    if (
        load_curation_policy().sha256 != state["curation_policy_sha256"]
        or load_content_policy().policy_digest != state["api_policy_sha256"]
    ):
        raise ValueError("refresh policy changed; rebuild from captured inputs")
    actual = {p.relative_to(output).as_posix() for p in output.glob("content/*/**/index.md")}
    if actual != set(state["documents"]):
        raise ValueError("refresh document inventory changed")
    prior = json.loads(published["manifest.json"])
    represented = actual | {alias["path"] for alias in state["aliases"]}
    if not {d["path"] for d in prior["documents"]} <= represented:
        raise ValueError("source refresh dropped a previously verified article")
    prior_aliases = prior.get("aliases", prior.get("enrichment", {}).get("aliases", []))
    for alias in state["aliases"]:
        if alias in prior_aliases:
            continue
        evidence = state.get("alias_sources", {}).get(alias["path"])
        if evidence is None or evidence["kind"] != "current":
            raise ValueError("source refresh invented an alias identity")
        candidate = root / "candidates" / evidence["candidate"]
        _verify_candidate(current, candidate, alias["path"])
        metadata, body = split_document((candidate / alias["path"]).read_text())
        target_metadata, target_body = split_document((output / alias["target"]).read_text())
        if (
            body != target_body
            or metadata.get("canonical_url") != target_metadata.get("canonical_url")
            or metadata.get("url") != alias["url"]
        ):
            raise ValueError("current canonical alias lacks equal complete source content")
    if any(
        alias["path"] not in actual and alias not in state["aliases"] for alias in prior_aliases
    ):
        raise ValueError("source refresh dropped a historical alias")
    for relative, choice in state["documents"].items():
        target = output / relative
        if choice["kind"] == "published":
            original = published[relative]
        elif choice["kind"] == "current" and relative.startswith("content/" + SOURCE + "/"):
            if (
                relative not in confirmed
                or confirmed[relative]["file_sha256"] != choice["input_file_sha256"]
            ):
                raise ValueError("fresh article lacks a successful current capture")
            candidate = root / "candidates" / choice["candidate"]
            _verify_candidate(current, candidate, relative)
            original = (candidate / relative).read_bytes()
            if sha256_file(current / relative) != choice["input_file_sha256"]:
                raise ValueError("fresh document source changed")
        else:
            raise ValueError("source refresh cannot change an unselected source")
        original_meta, original_body = split_document(original.decode())
        metadata, body = split_document(target.read_text())
        if body != original_body or metadata.get("description") != original_meta.get("description"):
            raise ValueError("refresh body or description changed: " + relative)
        # Classification and relationships may be regenerated over the combined
        # inventory. Authored identity, dates and retained payload stay upstream-bound.
        for key in (
            "url",
            "canonical_url",
            "title",
            "slug",
            "publication_date",
            "modification_date",
        ):
            if metadata.get(key) != original_meta.get(key):
                raise ValueError("refresh authored metadata changed: " + relative)
        if sha256_file(target) != choice["file_sha256"]:
            raise ValueError("refresh metadata changed: " + relative)
    assets = {
        p.relative_to(output).as_posix(): sha256_file(p)
        for p in output.glob("content/*/**/assets/*")
    }
    if assets != state["assets"]:
        raise ValueError("refresh retained assets changed")
    for relative, digest in assets.items():
        parent = str(Path(relative).parent.parent / "index.md")
        choice = state["documents"][parent]
        source = (
            published[relative]
            if choice["kind"] == "published"
            else (root / "candidates" / choice["candidate"] / relative).read_bytes()
        )
        if hashlib.sha256(source).hexdigest() != digest:
            raise ValueError("refresh asset differs from selected upstream")
    return state


def inherited_documents(output: Path) -> set[str]:
    state = verify_refresh(output)
    return {path for path, choice in state["documents"].items() if choice["kind"] == "published"}


# Selection, fallback, alias closure and sealing are one publication transaction.
# pylint: disable-next=too-many-locals,too-many-branches,too-many-statements
def combine_docs(
    baseline: Path, current: Path, output: Path, receipt_sha256: str
) -> dict[str, Any]:
    """Curate each fresh article independently and retain verified other sources."""
    if (output / "content").exists() or (output / MARKER).exists():
        raise ValueError("combine-content requires a fresh output directory")
    published = _verified_baseline(baseline, receipt_sha256)
    prior = json.loads(published["manifest.json"])
    inventory = json.loads((current / "inventory.json").read_text())
    if not inventory["completed"] or corpus_digest(current) != inventory["input_corpus_sha256"]:
        raise ValueError("current official documentation capture is incomplete or changed")
    root = output / ".refresh"
    root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(current, root / "current")
    (root / "baseline").mkdir()
    for name in (*RELEASE_ASSET_NAMES, "publication.json"):
        shutil.copy2(baseline / name, root / "baseline" / name)
    choices: dict[str, dict[str, Any]] = {}
    for document in prior["documents"]:
        relative = document["path"]
        choices[relative] = {"kind": "published", "reason": "verified prior source retained"}
    findings = []
    confirmed = {
        item.get("path"): item
        for item in inventory["documents"]
        if item.get("status") == "success" and item.get("path")
    }
    for path in sorted((root / "current").glob(f"content/{SOURCE}/**/index.md")):
        relative = path.relative_to(root / "current").as_posix()
        if relative not in confirmed or confirmed[relative]["file_sha256"] != sha256_file(path):
            findings.append(
                {
                    "document": relative,
                    "disposition": "fallback" if relative in choices else "unresolved",
                    "reason": "current capture did not confirm this complete article",
                }
            )
            continue
        identifier = hashlib.sha256(relative.encode()).hexdigest()
        candidate = root / "candidates" / identifier
        destination = candidate / relative
        destination.parent.mkdir(parents=True)
        shutil.copy2(path, destination)
        if (path.parent / "assets").exists():
            shutil.copytree(path.parent / "assets", destination.parent / "assets")
        try:
            migrate_content(candidate)
            if not destination.exists():
                raise ValueError("fresh article requires an updated curation decision")
            validate_content_policy(candidate)
        except (ValueError, OSError) as failure:
            findings.append(
                {
                    "document": relative,
                    "disposition": "fallback" if relative in choices else "unresolved",
                    "reason": str(failure).replace(str(root), "private-source"),
                }
            )
            continue
        choices[relative] = {
            "kind": "current",
            "candidate": identifier,
            "input_file_sha256": sha256_file(path),
            "reason": "fresh article passed current policy",
        }
        findings.append(
            {"document": relative, "disposition": "accepted", "reason": "current policy passed"}
        )
    # Only historical alias identities are reused. If a route has independently
    # changed into a different article, its fresh article stays a separate leaf.
    aliases = []
    alias_sources = {}

    def selected_bytes(relative: str) -> bytes:
        choice = choices[relative]
        if choice["kind"] == "published":
            return published[relative]
        return (root / "candidates" / str(choice["candidate"]) / relative).read_bytes()

    for alias in prior.get("aliases", prior.get("enrichment", {}).get("aliases", [])):
        if alias["target"] not in choices:
            continue
        if alias["path"] in choices:
            original_meta, original_body = split_document(selected_bytes(alias["path"]).decode())
            target_meta, target_body = split_document(selected_bytes(alias["target"]).decode())
            source_a = root / "current" / alias["path"]
            source_b = root / "current" / alias["target"]
            same_capture = (
                source_a.is_file()
                and source_b.is_file()
                and (
                    split_document(source_a.read_text())[1]
                    == split_document(source_b.read_text())[1]
                )
            )
            if (original_body == target_body or same_capture) and original_meta.get(
                "canonical_url"
            ) == target_meta.get("canonical_url"):
                del choices[alias["path"]]
                for finding in findings:
                    if finding["document"] == alias["path"]:
                        finding["disposition"] = "alias"
        if alias["path"] not in choices:
            aliases.append(alias)
    canonical_groups: dict[str, list[str]] = {}
    for relative, choice in choices.items():
        if choice["kind"] == "current":
            metadata, _ = split_document(selected_bytes(relative).decode())
            canonical_groups.setdefault(str(metadata["canonical_url"]), []).append(relative)
    for canonical, paths in canonical_groups.items():
        if len(paths) < 2:
            continue
        preferred = (
            "content/" + SOURCE + "/" + stable_path(SOURCE, canonical).as_posix() + "/index.md"
        )
        target = preferred if preferred in paths else sorted(paths)[0]
        _, target_body = split_document(selected_bytes(target).decode())
        for relative in paths:
            if relative == target:
                continue
            metadata, body = split_document(selected_bytes(relative).decode())
            if body != target_body:
                continue
            alias_sources[relative] = choices.pop(relative)
            aliases.append({"path": relative, "target": target, "url": str(metadata["url"])})
            for finding in findings:
                if finding["document"] == relative:
                    finding["disposition"] = "alias"
    with_sources = {}
    for relative, choice in choices.items():
        if choice["kind"] == "published":
            data = published[relative]
            asset_source = {
                name: value
                for name, value in published.items()
                if name.startswith(str(Path(relative).parent) + "/assets/")
            }
        else:
            source = root / "candidates" / choice["candidate"]
            data = (source / relative).read_bytes()
            asset_source = {
                p.relative_to(source).as_posix(): p.read_bytes()
                for p in (source / Path(relative).parent / "assets").glob("*")
                if p.is_file()
            }
        target_file = output / relative
        target_file.parent.mkdir(parents=True, exist_ok=True)
        target_file.write_bytes(data)
        for name, value in asset_source.items():
            asset = output / name
            asset.parent.mkdir(parents=True, exist_ok=True)
            asset.write_bytes(value)
        with_sources[relative] = split_document(data.decode())[0]
    store = StateStore(output / "state.sqlite")
    try:
        prior_by_path = {d["path"]: d for d in prior["documents"]}
        capture_by_url = {item["url"]: item for item in inventory.get("documents", [])}
        for relative, metadata in with_sources.items():
            url = str(metadata["url"])
            store.discover([DiscoveredPage(source_id=str(metadata["sourceId"]), url=url)])
            store.mark(
                url, PageStatus.SUCCESS, output_path=relative, digest=str(metadata["content_hash"])
            )
            if choices[relative]["kind"] == "published":
                store.seed_provenance(url, prior_by_path[relative].get("provenance", {}))
                with store.connection:
                    store.connection.execute(
                        "UPDATE pages SET status=?,freshness=? WHERE canonical_url=?",
                        (PageStatus.CARRIED_FORWARD, "retained_verified", url),
                    )
            else:
                store.seed_provenance(
                    url,
                    {
                        "last_success_at": capture_by_url.get(url, {}).get(
                            "captured_at", inventory.get("ended_at")
                        )
                    },
                )
        enrich_snapshot(output, store)
        for relative, choice in choices.items():
            choice["file_sha256"] = sha256_file(output / relative)
        state = {
            "schema_version": 1,
            "baseline_receipt_sha256": receipt_sha256,
            "baseline_tag": json.loads((baseline / "publication.json").read_text())["release_tag"],
            "input_corpus_sha256": inventory["input_corpus_sha256"],
            "inventory_sha256": sha256_file(current / "inventory.json"),
            "curation_policy_sha256": load_curation_policy().sha256,
            "api_policy_sha256": load_content_policy().policy_digest,
            "documents": choices,
            "aliases": aliases,
            "alias_sources": alias_sources,
            "findings": findings,
            "capture_counts": inventory.get("counts", {}),
            "review_required": inventory.get("review_required", []),
            "unavailable": [
                item
                for item in inventory.get("documents", [])
                if item.get("change") == "unavailable"
            ],
            "assets": {
                p.relative_to(output).as_posix(): sha256_file(p)
                for p in output.glob("content/*/**/assets/*")
            },
        }
        (output / MARKER).write_bytes(json_bytes(state))
        (output / "curation-audit.json").write_bytes(
            json_bytes(
                {
                    "schema_version": 1,
                    "policy_sha256": state["curation_policy_sha256"],
                    "api_policy_sha256": state["api_policy_sha256"],
                    "source_refresh": {
                        "baseline_receipt_sha256": receipt_sha256,
                        "input_corpus_sha256": state["input_corpus_sha256"],
                        "findings": findings,
                    },
                }
            )
        )
        verify_refresh(output)
        validate_snapshot(output, store)
        write_quality_reports(output, reference=baseline)
        manifest = build_manifest(
            output,
            store,
            inventory.get("started_at", prior["started_at"]),
            inventory.get("ended_at", prior["ended_at"]),
        )
        write_release(output, manifest)
    finally:
        store.close()
    return state
