"""Exact retirement masks retain necessary media and every unaffected pixel."""

from __future__ import annotations

import hashlib
import io
import re
from pathlib import Path
from typing import Any

from markdown_it import MarkdownIt
from PIL import Image, ImageChops, ImageDraw

from .media_encoding import png_bytes

VERSION = "retirement-mask/1"


def sha(value: bytes | str) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def _image(raw: bytes) -> Image.Image:
    with Image.open(io.BytesIO(raw)) as image:
        if getattr(image, "n_frames", 1) != 1:
            raise ValueError("retirement masks require a qualified frame-preserving animated codec")
        if image.mode not in {"RGB", "RGBA"}:
            raise ValueError("retirement masks require lossless RGB or RGBA media")
        image.load()
        return image.copy()


def _masks(image: Image.Image, masks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not masks:
        raise ValueError("retirement media transformation has no reviewed masks")
    prepared: list[dict[str, Any]] = []
    for item in masks:
        box, color = item["rectangle"], item["color"]
        valid_box = len(box) == 4 and all(
            isinstance(v, int) and not isinstance(v, bool) for v in box
        )
        valid_box = (
            valid_box
            and 0 <= box[0] < box[2] <= image.width
            and 0 <= box[1] < box[3] <= image.height
        )
        valid_color = len(color) in {3, 4} and all(
            isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 255 for v in color
        )
        if (
            item.get("frame", 0) != 0
            or not valid_box
            or not valid_color
            or not str(item.get("reason", "")).strip()
        ):
            raise ValueError("invalid retirement frame, rectangle, fill or rationale")
        if any(
            box[0] < other["rectangle"][2]
            and other["rectangle"][0] < box[2]
            and box[1] < other["rectangle"][3]
            and other["rectangle"][1] < box[3]
            for other in prepared
        ):
            raise ValueError("overlapping retirement media masks")
        prepared.append({**item, "source_pixels_sha256": sha(image.crop(tuple(box)).tobytes())})
    return prepared


def prepare_mask(raw: bytes, masks: list[dict[str, Any]]) -> tuple[bytes, dict[str, Any]]:
    image = _image(raw)
    prepared = _masks(image, masks)
    output = image.copy()
    draw = ImageDraw.Draw(output)
    for item in prepared:
        x0, y0, x1, y1 = item["rectangle"]
        color = tuple(item["color"][:3]) + (
            (item["color"][3] if len(item["color"]) == 4 else 255,) if image.mode == "RGBA" else ()
        )
        draw.rectangle((x0, y0, x1 - 1, y1 - 1), fill=color)
    result = png_bytes(output)
    specification = {
        "version": VERSION,
        "source_sha256": sha(raw),
        "output_sha256": sha(result),
        "width": image.width,
        "height": image.height,
        "mode": image.mode,
        "masks": prepared,
    }
    verify_mask(raw, result, specification)
    return result, specification


def verify_mask(raw: bytes, result: bytes, specification: dict[str, Any]) -> None:
    if (
        specification.get("version") != VERSION
        or sha(raw) != specification.get("source_sha256")
        or sha(result) != specification.get("output_sha256")
    ):
        raise ValueError("retirement media source, output or codec guard changed")
    original, output = _image(raw), _image(result)
    if (
        original.size != output.size
        or original.mode != output.mode
        or (original.width, original.height, original.mode)
        != (specification["width"], specification["height"], specification["mode"])
    ):
        raise ValueError("retirement media dimensions or alpha changed")
    prepared = _masks(original, specification["masks"])
    if prepared != specification["masks"]:
        raise ValueError("retirement media source pixel guard changed")
    difference = ImageChops.difference(original, output)
    for item in prepared:
        box = tuple(item["rectangle"])
        expected = tuple(item["color"][:3]) + (
            (item["color"][3] if len(item["color"]) == 4 else 255,) if output.mode == "RGBA" else ()
        )
        if (
            output.crop(box).tobytes()
            != Image.new(output.mode, (box[2] - box[0], box[3] - box[1]), expected).tobytes()
        ):
            raise ValueError("retirement mask fill differs from its reviewed value")
        ImageDraw.Draw(difference).rectangle((box[0], box[1], box[2] - 1, box[3] - 1), fill=0)
    if any(channel.getbbox() is not None for channel in difference.split()):
        raise ValueError("retirement media changed pixels outside its reviewed masks")


def replay_mask(raw: bytes, specification: dict[str, Any]) -> bytes:
    output, prepared = prepare_mask(raw, specification["masks"])
    if prepared != specification:
        raise ValueError("retirement media transformation replay changed")
    return output


def _image_urls(body: str) -> list[tuple[int, int, str]]:
    protected = []
    lines = body.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    for token in MarkdownIt("commonmark", {"html": True}).parse(body):
        if token.type in {"fence", "code_block"} and token.map:
            protected.append((offsets[token.map[0]], offsets[token.map[1]]))
    pattern = re.compile(
        r'!\[[^\]\n]*\]\(\s*<?(?P<markdown>assets/[^\s)>]+)|<(?:img|source)\b[^>]*\bsrc=["\'](?P<html>assets/[^"\']+)',
        re.I,
    )
    result = []
    for match in pattern.finditer(body):
        field = "markdown" if match["markdown"] else "html"
        start, end = match.span(field)
        if not any(left <= start < right for left, right in protected):
            result.append((start, end, match[field]))
    return result


def reference_updates(body: str, source: str, output: str) -> list[dict[str, Any]]:
    urls = {url for _, _, url in _image_urls(body) if Path(url).stem == source}
    return [
        {
            "source": url,
            "destination": "assets/" + output + ".png",
            "count": sum(value == url for _, _, value in _image_urls(body)),
        }
        for url in sorted(urls)
    ]


def relocate_references(
    body: str, replacements: list[dict[str, Any]], *, guarded: bool = True
) -> str:
    indexed = {rule["source"]: rule for rule in replacements}
    if len(indexed) != len(replacements):
        raise ValueError("duplicate retirement media reference")
    selected = [
        (start, end, indexed[url]["destination"])
        for start, end, url in _image_urls(body)
        if url in indexed
    ]
    if guarded and any(
        sum(url == rule["source"] for _, _, url in _image_urls(body)) != rule["count"]
        for rule in replacements
    ):
        raise ValueError("retirement media reference guard changed")
    for start, end, destination in reversed(selected):
        body = body[:start] + destination + body[end:]
    return body


def plan_masks(topics: list[dict[str, Any]], originals: list[Any]) -> dict[Path, bytes]:
    """Prepare only governed, independently bound document-specific output assets."""
    planned: dict[Path, bytes] = {}
    for path, metadata, body in originals:
        for topic in topics:
            for review in topic["media"]:
                specification = review.get("transformation")
                if specification is None or metadata["url"] not in review.get("documents", []):
                    continue
                if (
                    topic["id"] != "appstack-retired"
                    or review["disposition"] != "keep"
                    or any(
                        not re.fullmatch(r"[0-9a-f]{64}", str(review.get(field, "")))
                        for field in ("renewal_binding_sha256", "renewal_validation_sha256")
                    )
                ):
                    raise ValueError("retirement media lacks independent placement approval")
                references = [
                    url for _, _, url in _image_urls(body) if Path(url).stem == review["sha256"]
                ]
                for reference in references:
                    source = path.parent / reference
                    if source.is_symlink() or not source.is_file():
                        raise ValueError("retirement media source is unavailable or unsafe")
                    raw = source.read_bytes()
                    if (
                        sha(raw) != review["sha256"]
                        or specification["source_sha256"] != review["sha256"]
                        or specification["output_sha256"] != review.get("output_sha256")
                    ):
                        raise ValueError(
                            "retirement media source or output placement guard changed"
                        )
                    output = replay_mask(raw, specification)
                    destination = (path.parent / "assets" / (sha(output) + ".png")).resolve()
                    if destination in planned and planned[destination] != output:
                        raise ValueError("conflicting retirement media output")
                    planned[destination] = output
    return planned
