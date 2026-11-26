"""Verified fresh admission can replace a retained dependency, never its old evidence."""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .content_inventory import atomic_private
from .curation import json_bytes, load_curation_policy, media_inventory
from .curation_renewal import proposal_context
from .curation_renewal_inputs import _topic_rules
from .enrichment.analysis import sha
from .enrichment.contracts import EDITOR_MODEL, ResponseEvidence
from .enrichment.gates import request_hash
from .refresh import _verified_baseline
from .render import split_document
from .source_boundary_review import parse_source_response, source_logical_request
from .source_inputs import MARKER, verify_source
from .source_review import PrivacyDecision, scan, text_fields
from .source_review_jobs import _context, _media_context

SourceReplay = Callable[[Path, str], dict[str, Any]]


def _candidate(
    directory: Path, article: str, output: Path
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, bytes]]:
    path = directory / article
    metadata, body = split_document(path.read_text())
    media = media_inventory(path, body)
    if any(m["kind"] != "local" or not m["sha256"] for m in media):
        raise ValueError("admitted dependency has incomplete media")
    item = {
        "media": [
            {"path": (Path(article).parent / m["reference"]).as_posix(), "sha256": m["sha256"]}
            for m in media
        ]
    }
    visual, fields, receipts = _media_context(directory, output / "dependency-media", item)
    files = {"articles/" + article: path.read_bytes()}
    for m in item["media"]:
        files["articles/" + m["path"]] = (directory / m["path"]).read_bytes()
    candidate = {
        "metadata": metadata,
        "body": body,
        "media": receipts,
        "findings": scan({**text_fields(metadata, body), **fields}),
        "file_hashes": {name: sha(data) for name, data in sorted(files.items())},
    }
    candidate["output_sha256"] = sha(json_bytes(candidate["file_hashes"]))
    return candidate, visual, files


def _admitted_choice(
    verified: dict[str, bytes],
    article: str,
    source_inputs: dict[str, tuple[Path, str]],
    *,
    replay: SourceReplay | None = None,
) -> tuple[dict[str, Any], Path, str, dict[str, Any]] | None:
    metadata, _ = split_document(verified[article].decode())
    selected = source_inputs.get(str(metadata["sourceId"]))
    if selected is None:
        return None
    directory, digest = selected
    marker = directory / MARKER
    if directory.is_symlink() or marker.is_symlink():
        raise ValueError("unsafe admitted baseline replacement source")
    receipt_bytes = marker.read_bytes()
    if sha(receipt_bytes) != digest:
        raise ValueError("source admission receipt digest mismatch")
    receipt = json.loads(receipt_bytes)
    if receipt["source"] not in {metadata["sourceId"], "all"}:
        raise ValueError("admitted baseline replacement selects another source")
    entry = next((d for d in receipt["documents"] if d.get("published_path") == article), None)
    # A pinned negative lookup grants no disposition or approval. Only a possible
    # replacement needs complete capture, review and native-media replay here.
    if entry is None or entry["disposition"] != "accepted":
        return None
    if any(p.is_symlink() for p in directory.rglob("*")):
        raise ValueError("unsafe admitted baseline replacement source")
    receipt = (replay or verify_source)(directory, digest)
    entry = next(d for d in receipt["documents"] if d.get("published_path") == article)
    if entry["disposition"] != "accepted":
        raise ValueError("admitted baseline replacement decision changed")
    if entry["url"] != metadata["url"]:
        raise ValueError("admitted baseline replacement addresses a different article")
    prior = json.loads(verified.get("manifest.json", b'{"documents": []}'))
    previous_date = next(
        (
            d.get("provenance", {}).get("last_success_at")
            for d in prior["documents"]
            if d["path"] == article
        ),
        None,
    )
    if not entry.get("captured_at") or previous_date and entry["captured_at"] < previous_date:
        raise ValueError("admitted baseline replacement is superseded")
    return metadata, directory, digest, entry


def prepare_replacement(
    baseline: Path,
    publication_sha256: str,
    article: str,
    output: Path,
    source_inputs: dict[str, tuple[Path, str]],
) -> dict[str, Any] | None:
    """Only independently admitted current bytes may satisfy a source dependency."""
    verified = _verified_baseline(baseline, publication_sha256)
    return _prepare_replacement(
        verified, publication_sha256, article, output, source_inputs, verify_source
    )


def prepare_replacements(
    baseline: Path,
    publication_sha256: str,
    articles: list[str],
    output: Path,
    source_inputs: dict[str, tuple[Path, str]],
) -> dict[str, dict[str, Any]]:
    """Share complete replay only inside one candidate-preparation transaction."""
    verified = _verified_baseline(baseline, publication_sha256)
    proofs: dict[tuple[Path, str], dict[str, Any]] = {}

    def replay(directory: Path, digest: str) -> dict[str, Any]:
        key = directory, digest
        if key not in proofs:
            proofs[key] = verify_source(directory, digest)
        return proofs[key]

    contexts = {}
    for article in dict.fromkeys(articles):
        candidate = _prepare_replacement(
            verified, publication_sha256, article, output, source_inputs, replay
        )
        if candidate is not None:
            contexts[article] = candidate
    # No dependency inventory or inference can use the cohort before every
    # original and copied source passes fresh complete replay at this boundary.
    for (directory, digest), proof in proofs.items():
        if verify_source(directory, digest) != proof:
            raise ValueError("admitted source replay changed during dependency preparation")
    for context in contexts.values():
        _verify_replacement(context, output, replay)
        _verify_prepared_inputs(context, output)
    if _verified_baseline(baseline, publication_sha256) != verified:
        raise ValueError("verified baseline changed during dependency preparation")
    return contexts


# pylint: disable-next=too-many-locals
def _prepare_replacement(
    verified: dict[str, bytes],
    publication_sha256: str,
    article: str,
    output: Path,
    source_inputs: dict[str, tuple[Path, str]],
    replay: SourceReplay,
) -> dict[str, Any] | None:
    selected = _admitted_choice(verified, article, source_inputs, replay=replay)
    if selected is None:
        return None
    metadata, directory, digest, entry = selected
    relative = Path("dependency-sources") / str(metadata["sourceId"]) / digest
    saved = output / relative
    if not saved.exists():
        saved.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copytree(directory, saved, copy_function=os.link)
    receipt = replay(saved, digest)
    candidate, visual, files = _candidate(saved, article, output)
    _verify_candidate_files(files, receipt)
    source_evidence, original_visual = _source_evidence(saved, entry, candidate)
    evidence = {
        "input_kind": "independently_admitted_current_source",
        "baseline_source_record_sha256": sha(
            (output / "dependency-privacy/documents" / (sha(article) + ".json")).read_bytes()
        ),
        "baseline_receipt_sha256": publication_sha256,
        "baseline_article": article,
        "baseline_article_sha256": sha(verified[article]),
        "admitted_source": {"path": relative.as_posix(), "sha256": digest, "document": entry},
        **source_evidence,
    }
    context = proposal_context(
        candidate["metadata"],
        candidate["body"],
        candidate["media"],
        candidate["output_sha256"],
        load_curation_policy().sha256,
        evidence,
    )
    _persist_context(output, context, files, original_visual + visual)
    _verify_replacement(context, output, replay)
    return context


def verify_replacement(context: dict[str, Any], output: Path) -> None:
    """Always independently replay complete admission outside preparation."""
    _verify_replacement(context, output, verify_source)


def _verify_replacement(context: dict[str, Any], output: Path, replay: SourceReplay) -> None:
    evidence = context["evidence"]
    selected = evidence["admitted_source"]
    relative = Path(selected["path"])
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or len(relative.parts) != 3
        or relative.parts[0] != "dependency-sources"
        or relative.name != selected["sha256"]
    ):
        raise ValueError("unsafe admitted baseline dependency path")
    directory = output / relative
    receipt = replay(directory, selected["sha256"])
    if (
        selected["document"] not in receipt["documents"]
        or selected["document"]["disposition"] != "accepted"
    ):
        raise ValueError("admitted baseline source decision changed")
    article = evidence["baseline_article"]
    _verify_baseline_custody(output, article, evidence)
    if selected["document"]["published_path"] != article:
        raise ValueError("admitted baseline source article changed")
    candidate, visual, files = _candidate(directory, article, output)
    _verify_candidate_files(files, receipt)
    checks, source_visual = _source_evidence(directory, selected["document"], candidate)
    supplied_visual = output / context["binding_sha256"] / "visual.json"
    if any(evidence.get(key) != value for key, value in checks.items()) or (
        supplied_visual.is_symlink()
        or json.loads(supplied_visual.read_bytes()) != source_visual + visual
    ):
        raise ValueError("admitted dependency original, privacy or native-media evidence changed")
    if any(context[key] != candidate[key] for key in ("metadata", "body", "media")) or (
        context["source_files_sha256"] != candidate["output_sha256"]
    ):
        raise ValueError("admitted dependency intermediate changed")
    if (
        sha((directory / MARKER).read_bytes()) != selected["sha256"]
        or context["metadata"]["url"] != selected["document"]["url"]
    ):
        raise ValueError("admitted dependency source proof changed")


def _verify_prepared_inputs(context: dict[str, Any], output: Path) -> None:
    prepared = output / context["binding_sha256"]
    record = prepared / "context.json"
    inputs = prepared / "inputs"
    expected = context["evidence"]["privacy_candidate"]["file_hashes"]
    if any(path.is_symlink() for path in (prepared, record, inputs)) or any(
        path.is_symlink() for path in inputs.rglob("*")
    ):
        raise ValueError("prepared admitted dependency custody changed")
    if (
        json.loads(record.read_bytes()) != context
        or {
            path.relative_to(inputs).as_posix(): sha(path.read_bytes())
            for path in inputs.rglob("*")
            if path.is_file()
        }
        != expected
    ):
        raise ValueError("prepared admitted dependency custody changed")


def _verify_candidate_files(files: dict[str, bytes], receipt: dict[str, Any]) -> None:
    for name, raw in files.items():
        if not name.startswith("articles/"):
            raise ValueError("admitted dependency accepted file path changed")
        relative = name.removeprefix("articles/")
        if receipt["accepted_files"].get(relative) != sha(raw):
            raise ValueError("admitted dependency accepted file pin changed")


def _source_evidence(
    directory: Path, entry: dict[str, Any], candidate: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    path = directory / ".admission/review" / entry["record"]
    raw = path.read_bytes()
    if path.is_symlink() or sha(raw) != entry["record_sha256"]:
        raise ValueError("admitted dependency source record pin changed")
    record = json.loads(raw)
    original, visual = _context(
        directory / ".admission/capture", directory / ".admission/review", record
    )
    response = ResponseEvidence.model_validate(record["phases"]["privacy"])
    privacy = parse_source_response(
        response, PrivacyDecision, EDITOR_MODEL, request_hash(source_logical_request(response))
    )
    return {
        "original": {key: original[key] for key in ("metadata", "body", "media", "findings")},
        "source_article_html": original["source_article_html"],
        "privacy_candidate": candidate,
        "privacy_decision": privacy.model_dump(),
        "policy_topics": _topic_rules(original, candidate),
        "source_admission_binding_sha256": original["binding_sha256"],
    }, visual


def _verify_baseline_custody(output: Path, article: str, evidence: dict[str, Any]) -> None:
    record = output / "dependency-privacy/documents" / (sha(article) + ".json")
    if record.is_symlink() or sha(record.read_bytes()) != evidence["baseline_source_record_sha256"]:
        raise ValueError("replacement original baseline source evidence changed")
    original = json.loads(record.read_bytes())
    relative_source = Path(original["source"])
    if relative_source.is_absolute() or ".." in relative_source.parts:
        raise ValueError("unsafe replacement original baseline source path")
    source = output / "dependency-privacy" / relative_source
    hashes = {
        p.relative_to(source).as_posix(): sha(p.read_bytes())
        for p in sorted(source.rglob("*"))
        if p.is_file()
    }
    if not all(
        (
            not source.is_symlink(),
            not any(p.is_symlink() for p in source.rglob("*")),
            hashes == original["input_file_hashes"],
            sha(json_bytes(hashes)) == original["binding"]["source_files_sha256"],
            original["binding"]["baseline_receipt_sha256"] == evidence["baseline_receipt_sha256"],
            original["binding"]["baseline_article_sha256"] == evidence["baseline_article_sha256"],
            original["binding"]["baseline_article"] == article,
        )
    ):
        raise ValueError("replacement original baseline custody changed")


def _persist_context(
    output: Path, context: dict[str, Any], files: dict[str, bytes], visual: list[dict[str, Any]]
) -> None:
    destination = output / context["binding_sha256"]
    for name, data in files.items():
        atomic_private(destination / "inputs" / name, data)
    atomic_private(destination / "context.json", json_bytes(context))
    atomic_private(destination / "visual.json", json_bytes(visual))
