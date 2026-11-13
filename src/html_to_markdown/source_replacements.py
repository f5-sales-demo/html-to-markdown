"""Finding-specific exact replacement evidence, never a privacy approval."""

from __future__ import annotations

import json
from typing import Any

from .enrichment.analysis import sha
from .source_review import CREDENTIALS, PrivacyDecision, text_fields


def literal_variants(value: str) -> set[str]:
    variants = {value}
    try:
        decoded = json.loads(value)
    except ValueError:
        return variants
    if isinstance(decoded, str):
        variants.add(decoded)
    return variants


def candidate_fields(candidate: dict[str, Any]) -> dict[str, str]:
    fields = text_fields(candidate["metadata"], candidate["body"])
    for digest, receipt in candidate["media"].items():
        for index, frame in enumerate(receipt["frames"]):
            address = f"media/{digest}/{index}"
            fields[address + "/ocr"] = frame["text"]
            fields[address + "/metadata"] = json.dumps(frame.get("metadata", {}), sort_keys=True)
    return fields


def guarded_text_placements(
    context: dict[str, Any],
    candidate: dict[str, Any],
    proposal: PrivacyDecision,
    source_sha256: str,
    replacement: str,
    finding: dict[str, Any],
) -> list[dict[str, Any]]:
    originals = text_fields(context["metadata"], context["body"])
    location = finding["location"]
    target = candidate_fields(candidate).get(location, "")
    result = []
    delta = 0
    for edit in sorted(
        (e for e in proposal.text_changes if e.field == location), key=lambda e: e.start
    ):
        source = originals.get(edit.field, "")
        post_start = edit.start + delta
        post_end = post_start + len(edit.replacement)
        delta += len(edit.replacement) - (edit.end - edit.start)
        if (
            edit.replacement == replacement
            and edit.source_sha256 == source_sha256
            and 0 <= edit.start < edit.end <= len(source)
            and post_start <= finding["start"] < finding["end"] <= post_end
            and target[post_start:post_end] == replacement
        ):
            original = source[edit.start : edit.end]
            if sha(original) == source_sha256:
                result.append(
                    {
                        "kind": "guarded_text",
                        "field": edit.field,
                        "original": original,
                        "start": edit.start,
                        "end": edit.end,
                        "candidate_start": post_start,
                        "candidate_end": post_end,
                    }
                )
    return result


def contains_finding(target: str, replacement: str, finding: dict[str, Any]) -> bool:
    for variant in literal_variants(replacement):
        offset = target.find(variant)
        while offset >= 0:
            if offset <= finding["start"] < finding["end"] <= offset + len(variant):
                return True
            offset = target.find(variant, offset + 1)
    return False


def guarded_native_placements(
    context: dict[str, Any],
    candidate: dict[str, Any],
    proposal: PrivacyDecision,
    source_sha256: str,
    replacement: str,
    finding: dict[str, Any],
) -> list[dict[str, Any]]:
    location = finding["location"]
    target = candidate_fields(candidate).get(location, "")
    result = []
    for change in proposal.media_changes:
        output = (
            candidate.get("media_transforms", {}).get(change.source_sha256, {}).get("output_sha256")
        )
        placements = [(0, change.labels), *[(frame.frame, frame.labels) for frame in change.frames]]
        for frame, labels in placements:
            if location != f"media/{output}/{frame}/ocr":
                continue
            if not contains_finding(target, replacement, finding):
                continue
            for label in labels:
                if label.source_value_sha256 != source_sha256 or label.text != replacement:
                    continue
                for observed in context.get("confirmed_media_values", []):
                    original = observed.get("value")
                    if not isinstance(original, str) or sha(original) != source_sha256:
                        continue
                    if (
                        observed["media_sha256"] == change.source_sha256
                        and observed["frame"] == frame
                        and observed["rectangle"] == label.rectangle
                        and observed["value_sha256"] == source_sha256
                    ):
                        result.append(
                            {
                                "kind": "confirmed_native_label",
                                "media_sha256": change.source_sha256,
                                "output_sha256": output,
                                "frame": frame,
                                "rectangle": label.rectangle,
                                "original": original,
                            }
                        )
    return result


def replacement_provenance(
    context: dict[str, Any], candidate: dict[str, Any], proposal: PrivacyDecision
) -> dict[str, list[dict[str, Any]]]:
    if (
        proposal.identity != context["identity"]
        or proposal.binding_sha256 != context["binding_sha256"]
    ):
        raise ValueError("stale final transformation provenance")
    fields = candidate_fields(candidate)
    result = {}
    for finding in candidate["findings"]:
        if finding["kind"] in CREDENTIALS:
            continue
        field = fields.get(finding["location"], "")
        start, end = finding["start"], finding["end"]
        if not 0 <= start < end <= len(field) or sha(field[start:end]) != finding["value_sha256"]:
            raise ValueError("stale final finding placement in replacement provenance")
        value = field[start:end]
        evidence = []
        for mapping in proposal.values:
            if not any(value in variant for variant in literal_variants(mapping.replacement)):
                continue
            parameters = (
                context,
                candidate,
                proposal,
                mapping.source_sha256,
                mapping.replacement,
                finding,
            )
            for placement in [
                *guarded_text_placements(*parameters),
                *guarded_native_placements(*parameters),
            ]:
                if literal_variants(mapping.replacement) & literal_variants(placement["original"]):
                    continue
                evidence.append(
                    {
                        **{k: v for k, v in placement.items() if k != "original"},
                        "source_sha256": mapping.source_sha256,
                        "replacement_sha256": sha(mapping.replacement),
                        "finding_value_sha256": finding["value_sha256"],
                        "replacement": mapping.replacement,
                    }
                )
        if evidence:
            result[finding["id"]] = evidence
    return result
