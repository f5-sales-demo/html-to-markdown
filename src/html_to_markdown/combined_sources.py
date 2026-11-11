"""Governed all-source selection with preserved admission and curation inputs."""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from .content_policy import load_content_policy, migrate_content, validate_content_policy
from .curation import corpus_digest, json_bytes, load_curation_policy
from .enrichment.analysis import sha
from .enrichment.engine import import_state
from .metadata import enrich_snapshot
from .package import RELEASE_ASSET_NAMES, build_manifest, write_release
from .quality import write_quality_reports
from .refresh import _copy_article, _verified_baseline
from .render import split_document
from .source_inputs import verify_source
from .state import StateStore
from .urls import SOURCE_ROOTS
from .validation import validate_snapshot


def _content_files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in (root / "content").rglob("*")
        if path.is_file()
    }


def _write_files(root: Path, files: dict[str, bytes]) -> None:
    for relative, data in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


# Preserve all stage evidence and checks in one transaction.
# pylint: disable-next=too-many-locals,too-many-branches,too-many-statements
def combine_sources(
    baseline: Path, inputs: dict[str, tuple[Path, str]], output: Path, receipt_sha256: str
) -> dict[str, Any]:
    if output.exists() and any(output.iterdir()):
        raise ValueError("combined sources require a fresh output directory")
    if not inputs or inputs.keys() - SOURCE_ROOTS.keys():
        raise ValueError("source inputs must select known sources")
    published = _verified_baseline(baseline, receipt_sha256)
    prior = json.loads(published["manifest.json"])
    output.mkdir(parents=True, exist_ok=True)
    root = output / ".refresh"
    (root / "baseline").mkdir(parents=True)
    for name in (*RELEASE_ASSET_NAMES, "publication.json"):
        shutil.copy2(baseline / name, root / "baseline" / name)
    baseline_current = root / "baseline-curated"
    _write_files(
        baseline_current,
        {path: data for path, data in published.items() if path.startswith("content/")},
    )
    migrate_content(baseline_current)
    validate_content_policy(baseline_current)
    selected = root / "selected"
    _write_files(selected, _content_files(baseline_current))
    prior_by_path = {doc["path"]: doc for doc in prior["documents"]}
    choices = {
        path: {
            "kind": "published",
            "source": split_document(data.decode())[0]["sourceId"],
            "reason": "verified_prior_source",
            "last_success_at": prior_by_path.get(path, {})
            .get("provenance", {})
            .get("last_success_at"),
        }
        for path, data in _content_files(baseline_current).items()
        if path.endswith("/index.md")
    }
    sources = {}
    findings = []
    ended_at = prior["ended_at"]
    for source, (directory, digest) in sorted(inputs.items()):
        admission = verify_source(directory, digest)
        if admission["source"] not in {source, "all"}:
            raise ValueError("source admission receipt selects another source")
        saved = root / "sources" / source
        shutil.copytree(directory, saved)
        sources[source] = {
            "admission_sha256": digest,
            "content_sha256": admission["content_sha256"],
            "inventory_sha256": admission["inventory_sha256"],
            "enumeration_complete": admission["enumeration_complete"],
            "capture_complete": admission["capture_complete"],
            "review_complete": admission["review_complete"],
        }
        ended_at = max(ended_at, admission["ended_at"])
        for item in admission["documents"]:
            if item.get("sourceId") != source:
                continue
            relative = item.get("published_path")
            if not relative:
                findings.append(
                    {
                        "identity": item["identity"],
                        "source": source,
                        "disposition": item["disposition"],
                        "reason": item["reason"],
                    }
                )
                continue
            prior_success = choices.get(relative, {}).get("last_success_at")
            captured_at = item.get("captured_at")
            if prior_success and captured_at and captured_at < prior_success:
                findings.append(
                    {
                        "document": relative,
                        "source": source,
                        "disposition": "superseded",
                        "reason": "capture_older_than_latest_verified_source",
                    }
                )
                continue
            if item["disposition"] == "policy_excluded":
                if relative in choices:
                    shutil.rmtree(selected / Path(relative).parent)
                    choices.pop(relative)
                findings.append(
                    {
                        "document": relative,
                        "source": source,
                        "disposition": "policy_excluded",
                        "reason": "independently_validated_exclusion",
                    }
                )
                continue
            if item["disposition"] != "accepted":
                findings.append(
                    {
                        "document": relative,
                        "source": source,
                        "disposition": "source_fallback" if relative in choices else "queued",
                        "reason": item["reason"],
                    }
                )
                continue
            candidate = root / "candidates" / sha(source + ":" + relative)
            _copy_article(saved, candidate, relative)
            try:
                migrate_content(candidate)
                if not (candidate / relative).is_file():
                    raise ValueError("source_requires_updated_curation")
                validate_content_policy(candidate)
            except (ValueError, OSError):
                findings.append(
                    {
                        "document": relative,
                        "source": source,
                        "disposition": "source_fallback" if relative in choices else "queued",
                        "reason": "source_curation_requires_review",
                    }
                )
                continue
            if (selected / Path(relative).parent).exists():
                shutil.rmtree(selected / Path(relative).parent)
            _copy_article(saved, selected, relative)
            choices[relative] = {
                "kind": "admitted",
                "source": source,
                "admission_sha256": digest,
                "record_sha256": item["record_sha256"],
                "last_success_at": captured_at,
                "reason": "independently_reviewed_current_source",
            }
            findings.append(
                {
                    "document": relative,
                    "source": source,
                    "disposition": "accepted",
                    "reason": "admission_and_curation_passed",
                }
            )
    # Preserve selection before the document-wide retirement/API transformation.
    _write_files(output, _content_files(selected))
    migrate_content(output)
    validate_content_policy(output)
    aliases = [
        alias
        for alias in prior.get("aliases", prior.get("enrichment", {}).get("aliases", []))
        if (output / alias["target"]).is_file() and not (output / alias["path"]).is_file()
    ]
    store = StateStore(output / "state.sqlite")
    try:
        # Keep source capture clocks distinct from metadata regeneration time.
        (output / "manifest.json").write_bytes(published["manifest.json"])
        import_state(output, store)
        for path, choice in choices.items():
            if not (output / path).is_file():
                continue
            meta, _ = split_document((output / path).read_text())
            with store.connection:
                store.connection.execute(
                    "UPDATE pages SET last_success_at=?, freshness=? WHERE canonical_url=?",
                    (
                        choice["last_success_at"],
                        "fresh" if choice["kind"] == "admitted" else "retained_verified",
                        meta["url"],
                    ),
                )
        enrich_snapshot(output, store)
        current = _content_files(output)
        choices = {path: choice for path, choice in choices.items() if path in current}
        state = {
            "schema_version": 2,
            "baseline_receipt_sha256": receipt_sha256,
            "baseline_tag": json.loads((baseline / "publication.json").read_bytes())["release_tag"],
            "baseline_curated_sha256": corpus_digest(baseline_current),
            "selected_sha256": corpus_digest(selected),
            "curation_policy_sha256": load_curation_policy().sha256,
            "api_policy_sha256": load_content_policy().policy_digest,
            "sources": sources,
            "documents": choices,
            "aliases": aliases,
            "findings": findings,
            "files": {path: sha(data) for path, data in sorted(current.items())},
            "output_sha256": corpus_digest(output),
            "ended_at": ended_at,
        }
        (output / "refresh-state.json").write_bytes(json_bytes(state))
        write_quality_reports(output)
        validate_snapshot(output, store)
        write_release(output, build_manifest(output, store, prior["started_at"], ended_at))
    finally:
        store.close()
    verify_combined(output)
    return state


def verify_combined(output: Path) -> dict[str, Any]:
    state = json.loads((output / "refresh-state.json").read_bytes())
    root = output / ".refresh"
    published = _verified_baseline(root / "baseline", state["baseline_receipt_sha256"])
    if (
        load_curation_policy().sha256 != state["curation_policy_sha256"]
        or load_content_policy().policy_digest != state["api_policy_sha256"]
    ):
        raise ValueError("combined source policy changed")
    for source, evidence in state["sources"].items():
        verify_source(root / "sources" / source, evidence["admission_sha256"])
    with tempfile.TemporaryDirectory(prefix="combined-replay-") as directory:
        replay = Path(directory)
        _write_files(
            replay, {path: data for path, data in published.items() if path.startswith("content/")}
        )
        migrate_content(replay)
        if (
            corpus_digest(replay) != state["baseline_curated_sha256"]
            or corpus_digest(root / "baseline-curated") != state["baseline_curated_sha256"]
        ):
            raise ValueError("preserved baseline curation changed")
    selected = root / "selected"
    if corpus_digest(selected) != state["selected_sha256"]:
        raise ValueError("selected admitted or fallback source changed")
    baseline = _content_files(root / "baseline-curated")
    for path, choice in state["documents"].items():
        expected = (
            baseline[path]
            if choice["kind"] == "published"
            else (root / "sources" / choice["source"] / path).read_bytes()
        )
        if (selected / path).read_bytes() != expected:
            raise ValueError("selected bytes differ from verified upstream")
    with tempfile.TemporaryDirectory(prefix="combined-policy-replay-") as directory:
        replay = Path(directory)
        _write_files(replay, _content_files(selected))
        migrate_content(replay)
        validate_content_policy(replay)
        expected = _content_files(replay)
        actual = _content_files(output)
        if expected.keys() != actual.keys():
            raise ValueError("combined curation inventory changed")
        for path, data in expected.items():
            if path.endswith("/index.md"):
                a_meta, a_body = split_document(data.decode())
                b_meta, b_body = split_document(actual[path].decode())
                if a_body != b_body or a_meta.get("description") != b_meta.get("description"):
                    raise ValueError("combined body or description changed")
            elif data != actual[path]:
                raise ValueError("combined source media changed")
    if {path: sha(data) for path, data in _content_files(output).items()} != state["files"]:
        raise ValueError("combined final metadata or assets changed")
    return dict(state)
