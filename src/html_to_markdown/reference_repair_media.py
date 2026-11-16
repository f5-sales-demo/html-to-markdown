"""Reconstruct complete native inputs while preserving digest-bound captured OCR."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .content_inventory import atomic_private
from .curation import json_bytes
from .enrichment.analysis import sha
from .enrichment.media import all_frame_inputs

OCR_FIELDS = {"text", "engine", "uncertain", "error"}


def seed_candidate_media(candidate: dict[str, Any], blobs: dict[str, Path], output: Path) -> None:
    """OCR is captured evidence; replay rechecks pixels, metadata and every frame guard."""
    if blobs.keys() != candidate["media"].keys():
        raise ValueError("source-reference replay media inventory changed")
    for digest, receipt in candidate["media"].items():
        path = blobs[digest]
        if sha(path.read_bytes()) != digest:
            raise ValueError("source-reference replay image bytes changed")
        native = all_frame_inputs(path, perform_ocr=False)
        if receipt["frame_count"] != len(native) or len(receipt["frames"]) != len(native):
            raise ValueError("source-reference replay frame coverage changed")
        frames = []
        for (url, expected), captured in zip(native, receipt["frames"], strict=True):
            if (
                captured.keys() - expected.keys() - OCR_FIELDS
                or any(
                    captured.get(key) != value
                    for key, value in expected.items()
                    if key not in OCR_FIELDS
                )
                or not isinstance(captured.get("text"), str)
            ):
                raise ValueError("source-reference replay native frame or metadata guard changed")
            frames.append((url, captured))
        atomic_private(output / "media" / (digest + "-native-2.json"), json_bytes(frames))
