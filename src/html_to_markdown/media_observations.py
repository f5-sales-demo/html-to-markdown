"""Independently confirmed visible media values; all evidence remains private."""

from __future__ import annotations

from typing import Any

from .curation import json_bytes
from .enrichment.analysis import sha
from .enrichment.contracts import StrictModel


class VisibleValue(StrictModel):
    media_sha256: str
    frame: int
    rectangle: list[int]
    value: str | None
    reason: str


class MediaObservations(StrictModel):
    identity: str
    binding_sha256: str
    observations: list[VisibleValue]
    complete_media_reviewed: bool
    unresolved: list[str]


class ObservationValidation(StrictModel):
    identity: str
    binding_sha256: str
    observations_sha256: str
    approved_indices: list[int]
    complete_media_reviewed: bool
    failures: list[str]
    unresolved: list[str]


def observation_payload(
    identity: str, binding: str, observations: MediaObservations, media: dict[str, Any]
) -> dict[str, Any]:
    if (
        observations.identity != identity
        or observations.binding_sha256 != binding
        or not observations.complete_media_reviewed
        or observations.unresolved
    ):
        raise ValueError("native-media observation is incomplete or stale")
    values = []
    placements = set()
    for item in observations.observations:
        frames = media.get(item.media_sha256, {}).get("frames", [])
        if not 0 <= item.frame < len(frames) or len(item.rectangle) != 4:
            raise ValueError("native-media observation addresses an absent frame")
        frame = frames[item.frame]
        left, top, right, bottom = item.rectangle
        if not (0 <= left < right <= frame["width"] and 0 <= top < bottom <= frame["height"]):
            raise ValueError("native-media observation rectangle is outside its frame")
        if (item.value is not None and not item.value.strip()) or not item.reason.strip():
            raise ValueError("native-media observation requires visible text and a reason")
        placement = (item.media_sha256, item.frame, tuple(item.rectangle))
        if placement in placements:
            raise ValueError("duplicate native-media observation placement")
        placements.add(placement)
        guard = (
            sha(item.value)
            if item.value is not None
            else sha(
                json_bytes(
                    {
                        "media_sha256": item.media_sha256,
                        "frame": item.frame,
                        "rectangle": item.rectangle,
                    }
                )
            )
        )
        values.append(
            {
                **item.model_dump(),
                "value_sha256": guard,
                "guard_kind": "visible_text" if item.value is not None else "identifying_region",
            }
        )
    return {"values": values, "observations_sha256": sha(json_bytes(values))}


def verify_observations(
    identity: str, binding: str, payload: dict[str, Any], validation: ObservationValidation
) -> list[dict[str, Any]]:
    checks = [
        validation.identity == identity,
        validation.binding_sha256 == binding,
        validation.observations_sha256 == payload["observations_sha256"],
        validation.complete_media_reviewed,
        not validation.failures,
        not validation.unresolved,
        len(validation.approved_indices) == len(set(validation.approved_indices)),
        set(validation.approved_indices) == set(range(len(payload["values"]))),
    ]
    if not all(checks):
        raise ValueError("native-media values were not completely and independently confirmed")
    return list(payload["values"])
