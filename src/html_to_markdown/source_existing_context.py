"""Verify an original source proposal as immutable data, without granting admission."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

from . import source_candidate
from .content_inventory import verify_capture
from .curation import blocks, json_bytes
from .enrichment.analysis import sha
from .enrichment.contracts import EDITOR_MODEL, ResponseEvidence
from .enrichment.gates import request_hash
from .enrichment.media import all_frame_inputs
from .media_encoding import review_media_encoding, source_review_encoding, verify_native_frame
from .render import split_document
from .source_binding_context import privacy_context
from .source_boundary_review import parse_source_response, source_logical_request
from .source_review import PrivacyDecision, binding_for, scan, text_fields

VERSION = "existing-source-proposal-inputs/1"


def _unchanged_record(path: Path, digest: str) -> None:
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise ValueError("unsafe original source record path")
    with path.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != digest:
        raise ValueError("original source record digest differs from its pin")


def _record(path: Path, digest: str) -> dict[str, Any]:
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise ValueError("unsafe original source record path")
    raw = path.read_bytes()
    if sha(raw) != digest:
        raise ValueError("original source record digest differs from its pin")
    value: dict[str, Any] = json.loads(raw)
    if sha(json_bytes(value["binding"])) != value["binding_sha256"]:
        raise ValueError("original source record binding digest changed")
    return value


def _original(
    saved: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], PrivacyDecision]:
    evidence = ResponseEvidence.model_validate(saved["phases"]["privacy"])
    if evidence.prompt_version not in {"source-admission/1", "source-privacy/2"}:
        raise ValueError("unsupported original source proposal contract")
    logical = source_logical_request(evidence)
    proposal = parse_source_response(evidence, PrivacyDecision, EDITOR_MODEL, request_hash(logical))
    content = logical["input"][0]["content"]
    context = json.loads(content[0]["text"])
    binding = saved["binding"]
    if evidence.prompt_version == "source-privacy/2":
        binding = privacy_context({"binding": binding})["binding"]
    digest = sha(json_bytes(binding))
    if any(
        (
            context["binding"] != binding,
            context["binding_sha256"] != digest,
            proposal.binding_sha256 != digest,
            proposal.identity != context["identity"],
            proposal.identity != saved["item"]["identity"],
            proposal.unresolved,
        )
    ):
        raise ValueError("original source proposal linkage or completion changed")
    return context, content[1:], proposal


def _native(
    capture: Path, saved: dict[str, Any], context: dict[str, Any], visual: list[dict[str, Any]]
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    media = saved["item"]["media"]
    if set(context["media"]) != {item["sha256"] for item in media}:
        raise ValueError("original native frame coverage is incomplete")
    images = {}
    for entry in visual:
        if entry["type"] == "input_image":
            url = entry["image_url"]
            if not url.startswith("data:image/") or ";base64," not in url:
                raise ValueError("unsafe original native image representation")
            images[sha(base64.b64decode(url.split(",", 1)[1], validate=True))] = url
    fields = {}
    verified = []
    with review_media_encoding(source_review_encoding(saved)):
        for item in media:
            frames = all_frame_inputs(capture / item["path"], perform_ocr=False)
            old = context["media"][item["sha256"]]
            if old["frame_count"] != len(frames) or len(old["frames"]) != len(frames):
                raise ValueError("original native frame coverage is incomplete")
            for index, (info, expected) in enumerate(zip(old["frames"], frames, strict=True)):
                url = images.get(info["visual_input_sha256"])
                if url is None or info.get("frame", 0) != index:
                    raise ValueError("original native frame coverage is incomplete")
                verify_native_frame(url, info, *expected)
                address = "media/" + item["sha256"] + "/" + str(index)
                fields[address + "/ocr"] = info["text"]
                fields[address + "/metadata"] = json.dumps(info.get("metadata", {}), sort_keys=True)
                verified.extend(
                    [
                        {"type": "input_text", "text": address},
                        {"type": "input_image", "image_url": url, "detail": "high"},
                    ]
                )
    return fields, verified


# This verifies source data only. Selection, independent validation and replay are separate gates.
# pylint: disable-next=too-many-locals
def qualify_source_proposal(
    capture: Path,
    current_record: Path,
    current_sha256: str,
    original_record: Path,
    original_sha256: str,
) -> dict[str, Any]:
    current = _record(current_record, current_sha256)
    original = _record(original_record, original_sha256)
    item = current["item"]
    inventory_path = capture / "inventory.json"
    inventory_raw = inventory_path.read_bytes()
    inventory = json.loads(inventory_raw)
    matches = [row for row in inventory["documents"] if row["identity"] == item["identity"]]
    if matches != [item]:
        raise ValueError("current captured inventory differs from the source record")
    verify_capture(capture, item)
    context, visual, proposal = _original(original)
    metadata, body = split_document((capture / item["article_path"]).read_text())
    source_html = source_candidate.article_html(capture, item)
    if any(
        (
            original["item"]["identity"] != item["identity"],
            original["item"]["sourceId"] != item["sourceId"],
            original["item"]["url"] != item["url"],
            original["item"]["article_sha256"] != item["article_sha256"],
            original["item"]["media"] != item["media"],
            context["metadata"] != metadata,
            context["body"] != body,
            context["source_article_html"] != source_html,
            original["binding"]["article_sha256"] != item["article_sha256"],
            original["binding"]["source_article_html_sha256"] != sha(source_html),
        )
    ):
        raise ValueError("original source differs from the complete current capture")
    media_fields, native_visual = _native(capture, original, context, visual)
    fields = {**text_fields(metadata, body), **media_fields}
    structural = [
        {
            "address": f"b:{index}",
            "source_address": block.location,
            "sha256": block.sha256,
            "text": block.text,
        }
        for index, block in enumerate(blocks(body))
    ]
    if context["fields"] != fields or context["structural_addresses"] != structural:
        raise ValueError("original source fields or structural coverage changed")
    if context["findings"] != scan(fields):
        raise ValueError("original source finding coverage requires current review")
    binding = binding_for(item, source_article_sha256=sha(source_html))
    if binding["editor_model"] != EDITOR_MODEL or binding["validator_model"] != EDITOR_MODEL:
        raise ValueError("current source validation requires Sol-only models")
    # The current execution envelope is separate; neither original record is rebound.
    result = {
        "version": VERSION,
        "input_controller_sha256": sha(Path(__file__).read_bytes()),
        "identity": item["identity"],
        "capture_inventory_sha256": sha(inventory_raw),
        "current_record_sha256": current_sha256,
        "original_record_sha256": original_sha256,
        "original_context": context,
        "original_native_visual": native_visual,
        "original_proposal": proposal.model_dump(),
        "source_binding": binding,
        "source_binding_sha256": sha(json_bytes(binding)),
        "original_feedback": original.get("privacy_feedback", []),
        "current_feedback": current.get("privacy_feedback", []),
        "editor_corrections_consumed": max(
            len(original.get("privacy_feedback", [])), len(current.get("privacy_feedback", []))
        ),
        "new_editor_allowances": 0,
        "admission_approved": False,
    }
    if inventory_path.read_bytes() != inventory_raw:
        raise ValueError("current captured inventory changed during qualification")
    verify_capture(capture, item)
    _unchanged_record(current_record, current_sha256)
    _unchanged_record(original_record, original_sha256)
    return result


def compile_source_proposal(
    capture: Path,
    output: Path,
    current_record: Path,
    original_record: Path,
    qualified: dict[str, Any],
    *,
    require_cached_media: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]], PrivacyDecision, dict[str, bytes]]:
    """Compile exact original data after checking both envelopes; confer no approval."""
    current_pin = qualified["current_record_sha256"]
    original_pin = qualified["original_record_sha256"]
    actual = qualify_source_proposal(
        capture, current_record, current_pin, original_record, original_pin
    )
    if actual != qualified:
        raise ValueError("original source qualification or current controller changed")
    current = _record(current_record, current_pin)
    original = _record(original_record, original_pin)
    context = qualified["original_context"]
    proposal = PrivacyDecision.model_validate(qualified["original_proposal"])
    # Do not rebind the original proposal to the current execution envelope.
    # The ordinary compiler retains its binding guard; this path verifies the
    # original complete request and source before using the shared rendering stage.
    with review_media_encoding(source_review_encoding(original)):
        result = source_candidate.render_source_proposal(
            capture, output, current, context, proposal, require_cached_media=require_cached_media
        )
    final = qualify_source_proposal(
        capture, current_record, current_pin, original_record, original_pin
    )
    if final != qualified:
        raise ValueError("original source qualification changed during compilation")
    return result
