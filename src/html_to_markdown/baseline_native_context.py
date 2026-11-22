"""Replay exact request-bound OCR against every unchanged original native frame."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from .baseline_ledger_custody import _safe, _verify_source
from .enrichment.analysis import sha
from .enrichment.contracts import EDITOR_MODEL, ResponseEvidence
from .enrichment.gates import request_hash
from .enrichment.media import all_frame_inputs
from .media_encoding import verify_native_frame
from .source_boundary_review import parse_source_response, source_logical_request
from .source_review import RelevanceDecision
from .source_review_jobs import _media_context


def _original_context(saved: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    evidence = ResponseEvidence.model_validate(saved["phases"]["relevance"])
    logical = source_logical_request(evidence)
    decision = parse_source_response(
        evidence, RelevanceDecision, EDITOR_MODEL, request_hash(logical)
    )
    content = logical["input"][0]["content"]
    context = json.loads(content[0]["text"])
    if (
        context["binding"] != saved["binding"]
        or context["binding_sha256"] != saved["binding_sha256"]
        or decision.identity != saved["identity"]
        or decision.binding_sha256 != saved["binding_sha256"]
    ):
        raise ValueError("original native OCR request addresses a different source")
    images: dict[str, str] = {}
    for item in content:
        if item["type"] == "input_image":
            url = item["image_url"]
            digest = sha(base64.b64decode(url.split(",", 1)[1], validate=True))
            if digest in images and images[digest] != url:
                raise ValueError("original native image representation is ambiguous")
            images[digest] = url
    return context, images


# Complete source and frame guards are checked together; OCR grants no approval.
# pylint: disable-next=too-many-locals
def native_context(
    capture: Path, output: Path, saved: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, Any]]:
    """OCR is bound document data; re-running OCR cannot change a saved request."""
    prior = saved.get("phases", {}).get("relevance")
    if not prior:
        return _media_context(capture, output, saved["item"])
    _verify_source(output, saved)
    context, images = _original_context(saved)
    expected_digests = {item["sha256"] for item in saved["item"]["media"]}
    if context["media"].keys() != expected_digests:
        raise ValueError("original native OCR request has incomplete source media")
    visual = []
    fields = {}
    receipts = {}
    for item in saved["item"]["media"]:
        member = Path(item["path"])
        if (
            member.is_absolute()
            or ".." in member.parts
            or saved["input_file_hashes"].get(member.as_posix()) != item["sha256"]
        ):
            raise ValueError("unsafe original native OCR source media member")
        path = capture / member
        _safe(path)
        if sha(path.read_bytes()) != item["sha256"]:
            raise ValueError("original native OCR source media changed")
        expected = all_frame_inputs(path, perform_ocr=False)
        old = context["media"][item["sha256"]]
        if old["frame_count"] != len(expected) or len(old["frames"]) != len(expected):
            raise ValueError("original native OCR request has incomplete frame coverage")
        for info, (expected_url, expected_info) in zip(old["frames"], expected, strict=True):
            url = images.get(info["visual_input_sha256"])
            if url is None:
                raise ValueError("original native OCR frame bytes are unavailable")
            verify_native_frame(url, info, expected_url, expected_info)
            address = "media/" + item["sha256"] + "/" + str(info.get("frame", 0))
            fields[address + "/ocr"] = info["text"]
            fields[address + "/metadata"] = json.dumps(info.get("metadata", {}), sort_keys=True)
            visual.extend(
                [
                    {"type": "input_text", "text": address},
                    {"type": "input_image", "image_url": url, "detail": "high"},
                ]
            )
        receipts[item["sha256"]] = old
    for address, value in fields.items():
        if context["fields"].get(address) != value:
            raise ValueError("original request OCR fields differ from their complete native frames")
    return visual, fields, receipts
