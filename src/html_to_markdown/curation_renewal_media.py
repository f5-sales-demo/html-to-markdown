"""Prepare unapproved retirement-media candidates from exact preserved inputs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .curation_media import prepare_mask, reference_updates, relocate_references, replay_mask
from .enrichment.analysis import sha


def source_media(context: dict[str, Any], directory: Path) -> dict[str, bytes]:
    files = directory / "inputs"
    if files.is_symlink():
        raise ValueError("unsafe retirement media input directory")
    captured = {
        p.relative_to(files).as_posix(): sha(p.read_bytes())
        for p in sorted(files.rglob("*"))
        if p.is_file()
    }
    from .curation import json_bytes  # pylint: disable=import-outside-toplevel

    if sha(json_bytes(captured)) != context["source_files_sha256"]:
        raise ValueError("retirement media source file inventory changed")
    result = {}
    for path in sorted(files.rglob("*")):
        if not path.is_file() or path.parent.name != "assets":
            continue
        if path.is_symlink():
            raise ValueError("unsafe retirement media source symlink")
        raw = path.read_bytes()
        digest = sha(raw)
        if digest in context["media"]:
            result[digest] = raw
    return result


def stage_media(
    context: dict[str, Any],
    topic: Any,
    body: str,
    originals: dict[str, bytes],
    current: dict[str, str],
    transforms: dict[str, Any],
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    reviews = []
    replacements = []
    for choice in topic.media:
        source = choice.sha256
        digest = current[source]
        review = {
            "sha256": digest,
            "disposition": choice.disposition,
            "documents": [context["identity"]],
            "reason": "independent_curation_validation_required",
        }
        if choice.masks:
            if (
                topic.topic != "appstack-retired"
                or choice.disposition != "keep"
                or source in transforms
            ):
                raise ValueError("retirement masks need one current AppStack keep placement")
            raw = originals.get(source)
            if raw is None or sha(raw) != source:
                raise ValueError("retirement mask requires exact preserved source media bytes")
            output, specification = prepare_mask(raw, [m.model_dump() for m in choice.masks])
            replacement = sha(output)
            rules = reference_updates(body, digest, replacement)
            if not rules:
                raise ValueError("retirement mask source image has no current document placement")
            body = relocate_references(body, rules)
            replacements.extend(rules)
            transforms[source] = {
                "topic": topic.topic,
                "source_sha256": source,
                "output_sha256": replacement,
                "specification": specification,
                "references": rules,
            }
            current[source] = replacement
            review["transformation"] = specification
            review["output_sha256"] = replacement
        reviews.append(review)
    return body, reviews, replacements


def candidate_visuals(
    candidate: dict[str, Any], originals: dict[str, bytes]
) -> list[dict[str, Any]]:
    import tempfile  # pylint: disable=import-outside-toplevel

    from .enrichment.media import all_frame_inputs  # pylint: disable=import-outside-toplevel

    visual = []
    for digest, transformation in candidate.get("media_transforms", {}).items():
        raw = replay_mask(originals[digest], transformation["specification"])
        with tempfile.TemporaryDirectory(prefix="retirement-media-candidate-") as directory:
            path = Path(directory) / "candidate.png"
            path.write_bytes(raw)
            for image, _ in all_frame_inputs(path, perform_ocr=False):
                visual.append({"type": "input_image", "image_url": image})
    return visual


def projected_media_fact(text: str, candidate: dict[str, Any]) -> str:
    replacements = []
    for transformation in candidate.get("media_transforms", {}).values():
        replacements.extend(transformation["references"])
    return relocate_references(text, replacements, guarded=False)


def reviewed_output_media(
    context: dict[str, Any], stages: list[dict[str, Any]], media_transforms: dict[str, Any]
) -> None:
    """Earlier topic reviews also witness the independently reviewed final media."""
    for transformed in media_transforms.values():
        for stage in stages:
            if stage["topic"] == transformed["topic"]:
                break
            original_media = next(
                m for m in stage["media"] if m["sha256"] == transformed["source_sha256"]
            )
            if original_media["disposition"] != "keep":
                raise ValueError("retirement mask conflicts with an earlier media removal")
            stage["media"].append(
                {
                    "sha256": transformed["output_sha256"],
                    "disposition": "keep",
                    "documents": [context["identity"]],
                    "reason": "independent_retirement_output_validation_required",
                    "source_sha256": transformed["source_sha256"],
                }
            )
