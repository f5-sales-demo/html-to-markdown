"""Independently confirmed visible media values; all evidence remains private."""

from __future__ import annotations

import base64
import io
import math
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from .curation import json_bytes
from .enrichment.analysis import sha
from .enrichment.contracts import StrictModel
from .media_encoding import png_bytes


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


def _native_crop(
    image_url: str, region: dict[str, Any], context_margin: int
) -> tuple[dict[str, Any], Any]:
    raw = base64.b64decode(image_url.split(",", 1)[1], validate=True)
    with Image.open(io.BytesIO(raw)) as original:
        left, top, right, bottom = region["rectangle"]
        if not (0 <= left < right <= original.width and 0 <= top < bottom <= original.height):
            raise ValueError("observation crop is outside its native frame")
        inspection = [
            max(0, left - context_margin),
            max(0, top - context_margin),
            min(original.width, right + context_margin),
            min(original.height, bottom + context_margin),
        ]
        crop = original.crop(tuple(inspection)).resize(
            ((inspection[2] - inspection[0]) * 4, (inspection[3] - inspection[1]) * 4),
            Image.Resampling.NEAREST,
        )
    return ({**region, "inspection_rectangle": inspection} if context_margin else region), crop


def observation_views(
    regions: list[dict[str, Any]], visual: list[dict[str, Any]], *, context_margin: int = 0
) -> list[dict[str, Any]]:
    """Supplement complete frames with enlarged exact pixels of confirmed regions."""
    sources = {
        item["text"]: visual[index + 1]["image_url"]
        for index, item in enumerate(visual[:-1])
        if item["type"] == "input_text"
        and item["text"].startswith("media/")
        and visual[index + 1]["type"] == "input_image"
    }
    crops = []
    if context_margin < 0:
        raise ValueError("observation context margin cannot be negative")
    for region in regions:
        address = f"media/{region['media_sha256']}/{region['frame']}"
        if address not in sources:
            raise ValueError("observation crop addresses an absent native frame")
        crops.append(_native_crop(sources[address], region, context_margin))
    available = 50 - sum(item["type"] == "input_image" for item in visual)
    if crops and available < 1:
        raise ValueError("complete native frames leave no room for source-region review views")
    result = []
    size = max(1, math.ceil(len(crops) / available)) if crops else 1
    for start in range(0, len(crops), size):
        pixels, description = _region_sheet(crops[start : start + size])
        data = io.BytesIO()
        data.write(png_bytes(pixels))
        result.extend(
            [
                {
                    "type": "input_text",
                    "text": description
                    + " Report rectangles in original full-frame coordinates, never contact-sheet coordinates.",
                },
                {
                    "type": "input_image",
                    "image_url": "data:image/png;base64,"
                    + base64.b64encode(data.getvalue()).decode(),
                    "detail": "high",
                },
            ]
        )
    return result


def boundary_views(context: dict[str, Any], visual: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Expose native edge pixels for feedback-named frames before regions exist."""
    if sum(item["type"] == "input_image" for item in visual) >= 50:
        return []
    hints = " ".join(context.get("observation_feedback", []))
    regions = []
    for digest, media in sorted(context["media"].items()):
        if digest not in hints:
            continue
        for index, frame in enumerate(media["frames"]):
            width, height = frame["width"], frame["height"]
            if any(
                not isinstance(value, int) or isinstance(value, bool) or value < 1
                for value in (width, height)
            ):
                raise ValueError("native boundary view requires valid frame dimensions")
            for top in sorted({0, max(0, height - 128)}):
                for left in range(0, width, 448):
                    regions.append(
                        {
                            "media_sha256": digest,
                            "frame": index,
                            "rectangle": [
                                left,
                                top,
                                min(width, left + 512),
                                min(height, top + 128),
                            ],
                        }
                    )
    views = observation_views(regions, visual)
    for view in views:
        if view["type"] == "input_text":
            view["text"] = (
                "Geometry-only native boundary inspection tiles: rectangles describe tiles, "
                "not identifying fields or redaction authorization. Feedback selects candidate "
                "frames only; derive any field rectangle from source pixels and require "
                "independent confirmation. " + view["text"]
            )
    return views


def _region_sheet(group: list[tuple[dict[str, Any], Any]]) -> tuple[Any, str]:
    if len(group) == 1:
        region, pixels = group[0]
        description = (
            "Source context around the candidate rectangle, enlarged four times without added detail. The inspection rectangle is not redaction authorization. "
            if "inspection_rectangle" in region
            else "Exact native-region pixels enlarged four times without added detail. "
        ) + json_bytes(region).decode()
    else:
        pixels = Image.new(
            "RGB",
            (max(crop.width for _, crop in group), sum(crop.height + 28 for _, crop in group)),
            "white",
        )
        draw = ImageDraw.Draw(pixels)
        position = 0
        panels = []
        for number, (region, crop) in enumerate(group, 1):
            draw.text(
                (2, position + 2),
                f"Panel {number}: frame {region['frame']}, native {region['rectangle']}",
                fill="black",
                font=ImageFont.load_default(size=12),
            )
            pixels.paste(crop, (0, position + 28))
            panels.append(
                {
                    **region,
                    "panel": number,
                    "sheet_rectangle": [
                        0,
                        position + 28,
                        crop.width,
                        position + 28 + crop.height,
                    ],
                    "scale": 4,
                }
            )
            position += crop.height + 28
        description = (
            "Labeled source-region contact sheet. Every panel preserves exact source pixels enlarged four times; labels are inspection annotations. "
            + (
                "Inspection context does not authorize wider redactions. "
                if any("inspection_rectangle" in region for region, _ in group)
                else ""
            )
            + json_bytes(panels).decode()
        )
    return pixels, description
