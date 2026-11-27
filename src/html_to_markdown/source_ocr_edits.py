"""Exact legacy OCR instructions assert native labels, never edit observations."""

from __future__ import annotations

import re
from typing import Any

from .curation import json_bytes
from .enrichment.analysis import sha
from .source_review import Label, PrivacyDecision, TextChange, apply_text

VERSION = "native-ocr-edit-guards/1"
ADDRESS = re.compile(r"media/([0-9a-f]{64})/(0|[1-9][0-9]*)/ocr")


def _native_labels(
    context: dict[str, Any], proposal: PrivacyDecision, edit: TextChange
) -> tuple[str, int, list[Label]]:
    matched = ADDRESS.fullmatch(edit.field)
    if matched is None:
        raise ValueError("noncanonical OCR transformation address")
    digest, frame_string = matched.groups()
    frame = int(frame_string)
    receipt = context.get("media", {}).get(digest, {})
    frames = receipt.get("frames", [])
    if (
        not 0 <= frame < len(frames)
        or receipt.get("frame_count") != len(frames)
        or frames[frame].get("frame", frame) != frame
        or frames[frame].get("text") != context.get("fields", {}).get(edit.field)
    ):
        raise ValueError("OCR address does not match the complete native frame receipt")
    changes = [change for change in proposal.media_changes if change.source_sha256 == digest]
    if len(changes) != 1:
        raise ValueError("OCR instruction requires exactly one source media transformation")
    change = changes[0]
    frame_ids = [item.frame for item in change.frames]
    if len(set(frame_ids)) != len(frame_ids) or (
        len(frames) == 1 and change.frames and (change.labels or change.rectangles)
    ):
        raise ValueError("duplicate or mixed OCR native frame transformation")
    labels = change.labels if frame == 0 or len(frames) > 1 else []
    labels = [
        *labels,
        *(label for item in change.frames if item.frame == frame for label in item.labels),
    ]
    matching = [
        label
        for label in labels
        if label.source_value_sha256 == edit.source_sha256 and label.text == edit.replacement
    ]
    if not matching:
        raise ValueError("OCR instruction has no matching mapped native image label")
    return digest, frame, matching


def _guard(context: dict[str, Any], proposal: PrivacyDecision, edit: TextChange) -> dict[str, Any]:
    digest, frame, labels = _native_labels(context, proposal, edit)
    original = context["fields"][edit.field]
    mappings = {value.source_sha256: value.replacement for value in proposal.values}
    if (
        not 0 <= edit.start < edit.end <= len(original)
        or sha(original[edit.start : edit.end]) != edit.source_sha256
        or mappings.get(edit.source_sha256) != edit.replacement
        or not edit.reason.strip()
    ):
        raise ValueError("OCR span digest or synthetic relationship guard changed")
    return {
        "field": edit.field,
        "start": edit.start,
        "end": edit.end,
        "source_sha256": edit.source_sha256,
        "ocr_sha256": sha(original),
        "replacement_sha256": sha(edit.replacement),
        "media_sha256": digest,
        "frame": frame,
        "labels": [
            {
                "rectangle": list(label.rectangle),
                "label_sha256": sha(json_bytes(label.model_dump())),
            }
            for label in labels
        ],
    }


def compile_text(
    context: dict[str, Any], proposal: PrivacyDecision
) -> tuple[dict[str, Any], str, dict[str, Any] | None]:
    """Preserve each OCR instruction as evidence of the actual native transformation."""
    ocr_edits = [edit for edit in proposal.text_changes if edit.field.startswith("media/")]
    if not ocr_edits:
        return (*apply_text(context["metadata"], context["body"], proposal), None)
    ordinary = proposal.model_copy(
        update={
            "text_changes": [
                edit for edit in proposal.text_changes if not edit.field.startswith("media/")
            ]
        }
    )
    metadata, body = apply_text(context["metadata"], context["body"], ordinary)
    guards = [_guard(context, proposal, edit) for edit in ocr_edits]
    ordered = sorted(ocr_edits, key=lambda edit: (edit.field, edit.start))
    if any(
        left.field == right.field and left.end > right.start
        for left, right in zip(ordered, ordered[1:], strict=False)
    ):
        raise ValueError("overlapping OCR transformation assertions")
    ledger = {
        "version": VERSION,
        "proposal_sha256": sha(json_bytes(proposal.model_dump())),
        "changes": guards,
    }
    return metadata, body, ledger
