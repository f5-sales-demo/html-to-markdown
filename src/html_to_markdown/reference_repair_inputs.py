"""Preserve and prove exact media-reference extraction repairs before model review."""

from __future__ import annotations

import copy
import json
import re
import tempfile
from pathlib import Path
from typing import Any

from .content_policy import load_content_policy
from .curation import json_bytes, load_curation_policy
from .curation_existing_validation import _original, _write_once
from .curation_renewal import Proposal
from .curation_renewal_review import review_request
from .enrichment.analysis import sha
from .enrichment.contracts import EDITOR_MODEL, ResponseEvidence
from .enrichment.gates import parse_response, request_hash
from .render import split_document
from .source_media_references import media_reference_spans
from .source_review import PrivacyDecision, RelevanceDecision, review_context_digest
from .source_review_jobs import _article_html, _compile

VERSION = "source-reference-repair/1"


def controller_digest() -> str:
    """Every input repair and validation dependency contributes to the request binding."""
    root = Path(__file__).parent
    return sha(
        json_bytes(
            {
                name: sha((root / name).read_bytes())
                for name in (
                    "reference_repair_inputs.py",
                    "reference_repair_review.py",
                    "source_media_references.py",
                    "curation_existing_validation.py",
                    "curation_renewal.py",
                    "curation_section_validation.py",
                    "curation_renewal_review.py",
                    "curation_model_evidence.py",
                    "source_phase_bindings.py",
                    "enrichment/gates.py",
                    "enrichment/contracts.py",
                )
            }
            | {"source_review": review_context_digest()}
        )
    )


def safe_file(root: Path, name: str) -> Path:
    """Reject absolute, traversing and symbolic capture paths, including ancestor links."""
    relative = Path(name)
    if not name or relative.is_absolute() or ".." in relative.parts:
        raise ValueError("unsafe source-reference repair input path")
    path = root
    if root.is_symlink():
        raise ValueError("unsafe source-reference repair root")
    for part in relative.parts:
        path /= part
        if path.is_symlink():
            raise ValueError("unsafe source-reference repair input link")
    if not path.is_file():
        raise ValueError("source-reference repair input file is unavailable")
    return path


def original_proposal(
    output: Path, pin: str, attempt: int
) -> tuple[dict[str, Any], dict[str, Any], Proposal, ResponseEvidence]:
    """Verify the exact incomplete original without mutating its state or response."""
    directory, context, state = _original(output, pin)
    if attempt < 0 or attempt > state["attempt"]:
        raise ValueError("invalid original reference-repair attempt")
    editor = ResponseEvidence.model_validate_json(
        (directory / f"attempt-{attempt:03d}/editor.json").read_bytes()
    )
    decision = parse_response(editor, Proposal, EDITOR_MODEL, request_hash(editor.request))
    visual = json.loads((directory / "visual.json").read_bytes())
    payload = json.loads(editor.request["input"][0]["content"][0]["text"])
    if (
        editor.request != review_request(context, visual, feedback=payload["validation_feedback"])
        or decision.binding_sha256 != pin
    ):
        raise ValueError("original source-reference repair proposal addresses different inputs")
    if not decision.unresolved:
        raise ValueError("complete proposals use ordinary existing-curation validation")
    return context, state, decision, editor


# Keep the ordered before/after placement verification together.
# pylint: disable-next=too-many-locals
def reference_delta(
    before: str, after: str, files: dict[str, bytes], article: str
) -> list[dict[str, Any]]:
    """Only broken duplicated image extensions may change, never adjacent source text."""
    old = media_reference_spans(before)
    current = media_reference_spans(after)
    if len(old) != len(current):
        raise ValueError("source-reference repair changed media placement coverage")
    changes: list[dict[str, Any]] = []
    parent = Path(article).parent
    for (start, end, reference), (_, _, replacement) in zip(old, current, strict=True):
        destination = (parent / replacement).as_posix()
        if destination not in files:
            raise ValueError("source-reference repair has unavailable media")
        digest = sha(files[destination])
        if (
            not re.fullmatch(
                r"assets/[a-f0-9]{64}\.(?:png|jpg|jpeg|gif|webp|avif|svg)", replacement
            )
            or Path(replacement).stem != digest
        ):
            raise ValueError("source-reference repair media identity is not content addressed")
        if reference == replacement:
            continue
        if (parent / reference).as_posix() in files or reference != replacement + Path(
            replacement
        ).suffix:
            raise ValueError(
                "source-reference repair is not a duplicated-extension extraction correction"
            )
        changes.append(
            {
                "start": start,
                "end": end,
                "before": reference,
                "after": replacement,
                "source_sha256": sha(reference),
                "media_sha256": digest,
            }
        )
    projected = before
    for change in reversed(changes):
        projected = projected[: change["start"]] + change["after"] + projected[change["end"] :]
    if not changes or projected != after:
        raise ValueError("source-reference repair changes unrelated source content")
    return changes


def media_structure(value: dict[str, Any]) -> dict[str, Any]:
    """Ignore only native derivative identities; every observed pixel fact remains exact."""
    result = copy.deepcopy(value)
    for media in result.values():
        for frame in media["frames"]:
            frame.pop("visual_input_sha256", None)
    return result


def verify_lineage(old: dict[str, Any], record: dict[str, Any], source: dict[str, Any]) -> None:
    """An unrelated privacy response or HTML capture cannot become a repair prerequisite."""
    evidence = old["evidence"]
    privacy = ResponseEvidence.model_validate(record["phases"]["privacy"])
    decision = parse_response(privacy, PrivacyDecision, EDITOR_MODEL, request_hash(privacy.request))
    if (
        evidence.get("source_article_html") != source["source_article_html"]
        or evidence.get("source_admission_binding_sha256") != record["binding_sha256"]
        or evidence.get("privacy_decision") != decision.model_dump()
        or evidence.get("original")
        != {key: source[key] for key in ("metadata", "body", "media", "findings")}
    ):
        raise ValueError("source-reference repair is unrelated to the original admission evidence")


def verify_variant(
    old: dict[str, Any],
    old_files: dict[str, bytes],
    candidate: dict[str, Any],
    files: dict[str, bytes],
    article: str,
) -> list[dict[str, Any]]:
    """Replay all metadata, file and media guards; a preparation assertion is insufficient."""
    if {k: v for k, v in candidate["metadata"].items() if k != "content_hash"} != {
        k: v for k, v in old["metadata"].items() if k != "content_hash"
    }:
        raise ValueError("source-reference repair changed metadata or source dates")
    if media_structure(candidate["media"]) != media_structure(old["media"]):
        raise ValueError(
            "source-reference repair changed media pixels, timing, metadata or observations"
        )
    if old_files.keys() != files.keys() or any(
        data != files[name] for name, data in old_files.items() if name != article
    ):
        raise ValueError("source-reference repair changed source-file inventory or asset bytes")
    old_meta, old_body = split_document(old_files[article].decode())
    if old_meta != old["metadata"] or old_body != old["body"]:
        raise ValueError("original source-reference repair article does not match its context")
    return reference_delta(old["body"], candidate["body"], files, article)


# Verify source, model and native-media evidence in the same compilation boundary.
# pylint: disable-next=too-many-locals
def compile_source(
    capture: Path, record: dict[str, Any], output: Path
) -> tuple[dict[str, Any], list[dict[str, Any]], PrivacyDecision, dict[str, bytes], dict[str, Any]]:
    """Original responses remain unchanged; the current compiler reads exact raw captures."""
    item = record["item"]
    if not item.get("capture_complete") or item.get("http_status") != 200:
        raise ValueError("source-reference repair requires a complete public capture")
    source_file = safe_file(capture, item["source_html_path"])
    if sha(source_file.read_bytes()) != item["source_html_sha256"]:
        raise ValueError("source-reference repair captured HTML changed")
    if sha(safe_file(capture, item["article_path"]).read_bytes()) != item["article_sha256"]:
        raise ValueError("source-reference repair captured article changed")
    for media in item["media"]:
        if sha(safe_file(capture, media["path"]).read_bytes()) != media["sha256"]:
            raise ValueError("source-reference repair captured media changed")
    privacy = ResponseEvidence.model_validate(record["phases"]["privacy"])
    proposal = parse_response(privacy, PrivacyDecision, EDITOR_MODEL, request_hash(privacy.request))
    source = json.loads(privacy.request["input"][0]["content"][0]["text"])
    relevance = ResponseEvidence.model_validate(record["phases"]["relevance"])
    classified = parse_response(
        relevance, RelevanceDecision, EDITOR_MODEL, request_hash(relevance.request)
    )
    relevance_source = json.loads(relevance.request["input"][0]["content"][0]["text"])
    if source["identity"] != item["identity"] or classified.identity != item["identity"]:
        raise ValueError("source-reference repair source identity changed")
    if (
        classified.disposition != "include"
        or classified.extraction != "complete"
        or relevance_source["source_article_html"] != source["source_article_html"]
        or _article_html(capture, item) != source["source_article_html"]
    ):
        raise ValueError("source-reference repair lacks exact complete source/relevance evidence")
    candidate, visual, projected, files = _compile(capture, output, record, source, proposal)
    return (
        candidate,
        list(privacy.request["input"][0]["content"][1:]) + visual,
        projected,
        files,
        source,
    )


# Preserve and bind every original/captured/repaired input in one transaction.
# pylint: disable-next=too-many-locals
def prepare_reference_repair(
    output: Path, original_pin: str, attempt: int, capture: Path, source_record: Path
) -> dict[str, Any]:
    """Preparation proves byte changes only; independent source and curation reviews remain."""
    context, state, _, editor = original_proposal(output, original_pin, attempt)
    if source_record.is_symlink():
        raise ValueError("unsafe source-reference repair record")
    record_bytes = source_record.read_bytes()
    record = json.loads(record_bytes)
    with tempfile.TemporaryDirectory(prefix="reference-repair-") as temporary:
        candidate, _, _, files, source = compile_source(capture, record, Path(temporary))
    verify_lineage(context, record, source)
    directory = output / original_pin
    old_files = {
        p.relative_to(directory / "inputs").as_posix(): p.read_bytes()
        for p in (directory / "inputs").rglob("*")
        if p.is_file()
    }
    article = record["item"].get("published_path", record["item"]["article_path"])
    changes = verify_variant(context, old_files, candidate, files, article)
    capture_names = {
        record["item"]["source_html_path"],
        record["item"]["article_path"],
        *(m["path"] for m in record["item"]["media"]),
    }
    captured = {name: safe_file(capture, name).read_bytes() for name in sorted(capture_names)}
    data = {
        "version": VERSION,
        "original_binding_sha256": original_pin,
        "original_attempt": attempt,
        "original_state_sha256": sha((directory / "state.json").read_bytes()),
        "original_editor_request_sha256": editor.request_sha256,
        "source_record_sha256": sha(record_bytes),
        "capture_files": {name: sha(raw) for name, raw in captured.items()},
        "current_candidate": candidate,
        "changes": changes,
        "source_article_html_sha256": sha(source["source_article_html"]),
        "controller_sha256": controller_digest(),
        "curation_policy_sha256": load_curation_policy().sha256,
        "api_policy_sha256": load_content_policy().policy_digest,
        "original_editor_budget": state["attempt"],
        "new_editor_proposals": 0,
        "fresh_capture": False,
        "publication_approved": False,
    }
    pin = sha(json_bytes(data))
    destination = output / "reference-repairs" / pin
    _write_once(destination / "context.json", json_bytes({**data, "binding_sha256": pin}))
    _write_once(destination / "source-record.json", record_bytes)
    for name, raw in captured.items():
        _write_once(destination / "capture" / name, raw)
    for name, raw in files.items():
        _write_once(destination / "repaired" / name, raw)
    return {**data, "binding_sha256": pin}


# Every preserved input is independently checked before returning its projection.
# pylint: disable-next=too-many-locals
def replay_reference_inputs(
    output: Path, pin: str
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    Proposal,
    ResponseEvidence,
    list[dict[str, Any]],
    PrivacyDecision,
]:
    """Rebuild the full source repair and reject any stale or changed prerequisite."""
    if not re.fullmatch(r"[a-f0-9]{64}", pin):
        raise ValueError("unsafe source-reference repair identity")
    directory = output / "reference-repairs" / pin
    data = json.loads(safe_file(directory, "context.json").read_bytes())
    if (
        data["binding_sha256"] != pin
        or sha(json_bytes({k: v for k, v in data.items() if k != "binding_sha256"})) != pin
    ):
        raise ValueError("source-reference repair binding changed")
    if (
        data["controller_sha256"] != controller_digest()
        or data["curation_policy_sha256"] != load_curation_policy().sha256
        or data["api_policy_sha256"] != load_content_policy().policy_digest
    ):
        raise ValueError("stale source-reference repair execution or policy")
    old, state, proposal, editor = original_proposal(
        output, data["original_binding_sha256"], data["original_attempt"]
    )
    if (
        sha((output / data["original_binding_sha256"] / "state.json").read_bytes())
        != data["original_state_sha256"]
        or editor.request_sha256 != data["original_editor_request_sha256"]
    ):
        raise ValueError("original source-reference repair ledger changed")
    record_path = safe_file(directory, "source-record.json")
    if sha(record_path.read_bytes()) != data["source_record_sha256"]:
        raise ValueError("source-reference repair record changed")
    for name, digest in data["capture_files"].items():
        if sha(safe_file(directory / "capture", name).read_bytes()) != digest:
            raise ValueError("source-reference repair capture changed")
    with tempfile.TemporaryDirectory(prefix="reference-repair-replay-") as temporary:
        record = json.loads(record_path.read_bytes())
        candidate, visual, projected, files, source = compile_source(
            directory / "capture", record, Path(temporary)
        )
    verify_lineage(old, record, source)
    if (
        candidate != data["current_candidate"]
        or sha(source["source_article_html"]) != data["source_article_html_sha256"]
    ):
        raise ValueError("source-reference repair compiler replay changed")
    for name, raw in files.items():
        if safe_file(directory / "repaired", name).read_bytes() != raw:
            raise ValueError("source-reference repair output bytes changed")
    original_inputs = output / data["original_binding_sha256"] / "inputs"
    old_files = {
        p.relative_to(original_inputs).as_posix(): p.read_bytes()
        for p in original_inputs.rglob("*")
        if p.is_file()
    }
    if (
        verify_variant(
            old,
            old_files,
            candidate,
            files,
            record["item"].get("published_path", record["item"]["article_path"]),
        )
        != data["changes"]
    ):
        raise ValueError("source-reference repair change inventory changed")
    return data, old, state, proposal, editor, visual, projected
