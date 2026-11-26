"""Shared complete source context and unapproved privacy rendering."""

from __future__ import annotations

import base64
import json
import tempfile
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from .content_inventory import atomic_private
from .curation import json_bytes, load_curation_policy
from .enrichment.analysis import sha
from .enrichment.media import all_frame_inputs
from .media_encoding import current_review_encoding, verify_native_frame
from .models import PageMetadata
from .render import serialize_document, split_document
from .source_media_references import media_destination_map, rewrite_media_references
from .source_review import (
    FindingDecision,
    PrivacyDecision,
    apply_text,
    preview_media,
    scan,
    text_fields,
)
from .source_review_reconciliation import guard_label_quotes


def article_html(capture: Path, item: dict[str, Any]) -> str:
    """Scope complete authored source exactly as the admission request does."""
    source_html = (capture / item["source_html_path"]).read_text()
    if item["sourceId"] == "community-f5-com":
        source_html = json.loads(source_html)["post_stream"]["posts"][0]["cooked"]
        # Discourse cooked is already the complete first post. Nested article
        # elements are resource cards, not the enclosing authored source.
        return str(BeautifulSoup(source_html, "html.parser"))
    soup = BeautifulSoup(source_html, "html.parser")
    return str(soup.select_one("c-site-article-detail-container, article, main") or soup)


def media_context(
    capture: Path,
    output: Path,
    item: dict[str, Any],
    *,
    require_cached: bool = False,
    frame_inputs: Any = all_frame_inputs,
) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, Any]]:
    visual = []
    fields: dict[str, str] = {}
    receipts = {}
    for media in item["media"]:
        path = capture / media["path"]
        if sha(path.read_bytes()) != media["sha256"]:
            raise ValueError("source media digest changed")
        # Keep old observations and their exact encoded bytes as immutable
        # history. The complete SVG geometry and compressed views use a new
        # cache namespace; a representation change grants no editor allowance.
        cache_generation = 2 if current_review_encoding() == 1 else 3
        cache = output / "media" / f"{media['sha256']}-native-{cache_generation}.json"
        if cache.exists():
            frames = json.loads(cache.read_bytes())
        else:
            if require_cached:
                raise ValueError("complete saved candidate media/OCR evidence is unavailable")
            frames = frame_inputs(path)
            atomic_private(cache, json_bytes(frames))
        expected = frame_inputs(path, perform_ocr=False)
        if len(frames) != len(expected):
            raise ValueError("cached media does not cover complete native frames")
        for captured_frame, expected_frame in zip(frames, expected, strict=True):
            verify_native_frame(*captured_frame, *expected_frame)
        receipts[media["sha256"]] = {
            "frame_count": len(frames),
            "frames": [frame[1] for frame in frames],
        }
        for data_url, info in frames:
            # Decode and verify the visual derivative on every replay as well.
            if (
                sha(base64.b64decode(data_url.split(",", 1)[1], validate=True))
                != info["visual_input_sha256"]
            ):
                raise ValueError("cached media visual digest changed")
            address = "media/" + media["sha256"] + "/" + str(info.get("frame", 0))
            fields[address + "/ocr"] = info["text"]
            fields[address + "/metadata"] = json.dumps(info.get("metadata", {}), sort_keys=True)
            visual.extend(
                [
                    {"type": "input_text", "text": address},
                    {"type": "input_image", "image_url": data_url, "detail": "high"},
                ]
            )
    return visual, fields, receipts


def retirement_rules() -> list[dict[str, Any]]:
    policy = load_curation_policy().raw
    policy_keys = {
        "id",
        "version",
        "mode",
        "retired_identities",
        "retired_patterns",
        "retired_urls",
        "scope_patterns",
        "dependency_rules",
        "provider_identities",
        "provider_aliases",
        "omit_urls",
    }
    rules = [
        {key: value for key, value in topic.items() if key in policy_keys}
        for topic in policy["topics"]
    ]
    return rules


# Rendering supplies an unapproved candidate; callers enforce their own exact envelope.
# pylint: disable-next=too-many-locals,too-many-statements
def render_source_proposal(
    capture: Path,
    output: Path,
    saved: dict[str, Any],
    context: dict[str, Any],
    proposal: PrivacyDecision,
    *,
    require_cached_media: bool = False,
    media_context: Any = media_context,
) -> tuple[dict[str, Any], list[dict[str, Any]], PrivacyDecision, dict[str, bytes]]:
    if proposal.unresolved:
        raise ValueError("privacy proposal is unresolved: " + "; ".join(proposal.unresolved))
    addressed = {choice.finding_id for choice in proposal.findings}
    if len(addressed) != len(proposal.findings) or addressed != {
        finding["id"] for finding in context["findings"]
    }:
        raise ValueError("privacy proposal must classify every finding exactly once")
    if any(choice.disposition == "unresolved" for choice in proposal.findings):
        raise ValueError("privacy finding is unresolved")
    guard_label_quotes(context, proposal)
    metadata, body = apply_text(context["metadata"], context["body"], proposal)
    changes = {change.source_sha256: change for change in proposal.media_changes}
    if len(changes) != len(proposal.media_changes) or changes.keys() - context["media"].keys():
        raise ValueError("duplicate or unknown media transformation")
    values = {value.source_sha256: value.replacement for value in proposal.values}
    files: dict[str, bytes] = {}
    transforms = {}
    media = []
    media_locations = {}
    media_references: list[tuple[str, str]] = []
    item = saved["item"]
    for original in item["media"]:
        raw = (capture / original["path"]).read_bytes()
        if original["sha256"] in changes:
            raw, transform = preview_media(raw, changes[original["sha256"]], values)
            transforms[original["sha256"]] = transform
        digest = sha(raw)
        transformed = digest != original["sha256"]
        # Consumer assets need a media extension for the Pages MIME contract.
        from .pipeline import Pipeline  # pylint: disable=import-outside-toplevel

        media_type = (
            ("image/gif" if raw.startswith(b"GIF") else "image/png")
            if transformed
            else original["media_type"]
        )
        extension = Pipeline._asset_extension(original["url"], media_type)  # pylint: disable=protected-access
        relative = str(
            Path(item.get("published_path", item["article_path"])).parent
            / "assets"
            / (digest + extension)
        )
        files[relative] = raw
        before = "assets/" + Path(original["path"]).name
        after = "assets/" + digest + extension
        media_references.append((before, after))
        media_locations[digest] = original["sha256"]
        media.append({**original, "path": relative, "sha256": digest})
    body = rewrite_media_references(
        body,
        media_destination_map(media_references),
        {"assets/" + Path(entry["path"]).name for entry in media},
    )
    article = serialize_document(PageMetadata.model_validate(metadata), body).encode()
    files[item.get("published_path", item["article_path"])] = article
    # Build the exact candidate derivative/OCR privately; keep original media too.
    with tempfile.TemporaryDirectory(prefix="admission-candidate-") as directory:
        root = Path(directory)
        for relative, data in files.items():
            atomic_private(root / relative, data)
        visual, media_fields, receipts = media_context(
            root, output, {"media": media}, require_cached=require_cached_media
        )
    final_meta, final_body = split_document(article.decode())
    fields = {**text_fields(final_meta, final_body), **media_fields}
    final_findings = scan(fields)
    # Carry contextual safe findings across deterministic offset changes only.
    # Identical values in different placements still receive separate final IDs.
    choices = {choice.finding_id: choice for choice in proposal.findings}
    safe: dict[tuple[str, str, str], list[FindingDecision]] = {}
    for finding in context["findings"]:
        choice = choices[finding["id"]]
        if choice.disposition in {"documentation_example", "authoritative_attribution"}:
            key = (finding["location"], finding["kind"], finding["value_sha256"])
            safe.setdefault(key, []).append(choice)
    projected = []
    for finding in final_findings:
        location = finding["location"]
        if location.startswith("media/"):
            parts = location.split("/")
            parts[1] = media_locations.get(parts[1], parts[1])
            location = "/".join(parts)
        key = (location, finding["kind"], finding["value_sha256"])
        candidates = safe.get(key, [])
        if not candidates:
            # Synthetic reserved examples also require independent validation.
            original = next(
                (
                    value
                    for value in proposal.values
                    if sha(value.replacement) == finding["value_sha256"]
                ),
                None,
            )
            if original is not None:
                projected.append(
                    FindingDecision(
                        finding_id=finding["id"],
                        disposition="documentation_example",
                        reason="synthetic replacement requires independent contextual validation",
                    )
                )
            else:
                projected.append(
                    FindingDecision(
                        finding_id=finding["id"],
                        disposition="unresolved",
                        reason="new or unmatched final finding",
                    )
                )
        else:
            projected.append(candidates.pop(0).model_copy(update={"finding_id": finding["id"]}))
    payload = {
        "metadata": final_meta,
        "body": final_body,
        "media": receipts,
        "media_transforms": transforms,
        "findings": final_findings,
        "finding_decisions": [choice.model_dump() for choice in projected],
        "file_hashes": {path: sha(data) for path, data in sorted(files.items())},
    }
    payload["output_sha256"] = sha(json_bytes(payload["file_hashes"]))
    return payload, visual, proposal.model_copy(update={"findings": projected}), files


# Keep exact proposal data with the complete source and candidate evidence.
# pylint: disable-next=too-many-arguments
