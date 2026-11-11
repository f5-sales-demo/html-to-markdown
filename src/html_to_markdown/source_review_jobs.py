"""Private, resumable admission phases with complete offline replay."""

from __future__ import annotations

import json
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from pydantic import BaseModel

from .content_inventory import atomic_private, verify_capture
from .curation import blocks, json_bytes
from .enrichment.analysis import sha
from .enrichment.codex_client import CodexClient
from .enrichment.contracts import EDITOR_MODEL, VALIDATOR_MODEL, ResponseEvidence
from .enrichment.gates import parse_response, request_hash
from .enrichment.media import all_frame_inputs
from .models import PageMetadata
from .render import serialize_document, split_document
from .source_review import (
    VERSION,
    AdmissionValidation,
    FindingDecision,
    PrivacyDecision,
    RelevanceDecision,
    addressed_spans,
    admission_request,
    apply_text,
    binding_for,
    enforce,
    preview_media,
    scan,
    text_fields,
    validate_admission,
)


def _retire_output(output: Path, saved: dict[str, Any]) -> None:
    """Preserve previously emitted bytes privately when admission becomes stale."""
    for relative in saved.get("file_hashes", {}):
        path = output / "reviewed" / Path(relative).relative_to("articles")
        if path.is_file():
            data = path.read_bytes()
            atomic_private(output / "history" / "published" / sha(data), data)
            path.unlink()


def prepare_reviews(capture: Path, output: Path) -> dict[str, Any]:
    inventory = json.loads((capture / "inventory.json").read_bytes())
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    records = []
    for item in inventory["documents"]:
        destination = output / "documents" / (sha(item["identity"]) + ".json")
        saved = json.loads(destination.read_bytes()) if destination.exists() else {}
        if not item.get("capture_complete") or item["disposition"] == "policy_excluded":
            if saved:
                atomic_private(
                    output / "history" / (sha(destination.read_bytes()) + ".json"),
                    destination.read_bytes(),
                )
                _retire_output(output, saved)
            records.append(
                {
                    "identity": item["identity"],
                    "url": item["url"],
                    "sourceId": item["sourceId"],
                    "disposition": item["disposition"],
                    "reason": item["reason"],
                }
            )
            continue
        verify_capture(capture, item)
        binding = binding_for(item)
        digest = sha(json_bytes(binding))
        if saved.get("binding_sha256") != digest:
            if saved:
                history = output / "history" / (sha(destination.read_bytes()) + ".json")
                atomic_private(history, destination.read_bytes())
                _retire_output(output, saved)
            saved = {
                "schema_version": 1,
                "version": VERSION,
                "item": item,
                "binding": binding,
                "binding_sha256": digest,
                "phases": {},
                "disposition": "queued",
                "reason": "admission_review_required",
            }
            atomic_private(destination, json_bytes(saved))
        records.append(
            {
                "identity": item["identity"],
                "binding_sha256": digest,
                "path": destination.relative_to(output).as_posix(),
            }
        )
    active = {record["path"] for record in records if "path" in record}
    for path in (output / "documents").glob("*.json"):
        if path.relative_to(output).as_posix() not in active:
            _retire_output(output, json.loads(path.read_bytes()))
    result = {
        "schema_version": 1,
        "version": VERSION,
        "inventory_sha256": sha((capture / "inventory.json").read_bytes()),
        "capture_binding": inventory["binding"],
        "documents": records,
        "enumeration_complete": inventory["completeness"]["enumeration"],
    }
    atomic_private(output / "review-inventory.json", json_bytes(result))
    return result


def _media_context(
    capture: Path, output: Path, item: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, Any]]:
    visual = []
    fields: dict[str, str] = {}
    receipts = {}
    for media in item["media"]:
        path = capture / media["path"]
        if sha(path.read_bytes()) != media["sha256"]:
            raise ValueError("source media digest changed")
        cache = output / "media" / (media["sha256"] + "-native-2.json")
        if cache.exists():
            frames = json.loads(cache.read_bytes())
        else:
            frames = all_frame_inputs(path)
            atomic_private(cache, json_bytes(frames))
        expected = all_frame_inputs(path, perform_ocr=False)
        if [frame[0] for frame in frames] != [frame[0] for frame in expected]:
            raise ValueError("cached media does not cover complete native frames")
        receipts[media["sha256"]] = {
            "frame_count": len(frames),
            "frames": [frame[1] for frame in frames],
        }
        for data_url, info in frames:
            # Decode and verify the visual derivative on every replay as well.
            import base64  # pylint: disable=import-outside-toplevel

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


def _context(
    capture: Path, output: Path, saved: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    item = saved["item"]
    verify_capture(capture, item)
    if (
        binding_for(item) != saved["binding"]
        or sha(json_bytes(saved["binding"])) != saved["binding_sha256"]
    ):
        raise ValueError("stale source policy or admission binding")
    metadata, body = split_document((capture / item["article_path"]).read_text())
    source_html = (capture / item["source_html_path"]).read_text()
    if item["sourceId"] == "community-f5-com":
        source_html = json.loads(source_html)["post_stream"]["posts"][0]["cooked"]
    source_soup = BeautifulSoup(source_html, "html.parser")
    source_article = (
        source_soup.select_one("c-site-article-detail-container, article, main") or source_soup
    )
    policy = json.loads(Path(__file__).with_name("curation_policy.json").read_bytes())
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
    visual, media_fields, media_receipts = _media_context(capture, output, item)
    fields = {**text_fields(metadata, body), **media_fields}
    context = {
        "identity": item["identity"],
        "binding_sha256": saved["binding_sha256"],
        "binding": saved["binding"],
        "metadata": metadata,
        "body": body,
        "source_article_html": str(source_article),
        "retirement_rules": rules,
        "structural_addresses": [
            {
                "address": f"b:{index}",
                "source_address": block.location,
                "sha256": block.sha256,
                "text": block.text,
            }
            for index, block in enumerate(blocks(body))
        ],
        "fields": fields,
        "findings": scan(fields),
        "media": media_receipts,
    }
    return context, visual


def _request(
    model: str, phase: str, context: dict[str, Any], visual: list[dict[str, Any]], schema: Any
) -> dict[str, Any]:
    payload = (
        {**context, "addressed_spans": addressed_spans(context["fields"])}
        if phase == "privacy"
        else context
    )
    body = admission_request(model, phase, payload, schema)
    body["input"][0]["content"].extend(visual)
    properties = body["text"]["format"]["schema"]["properties"]
    properties["identity"]["enum"] = [context["identity"]]
    properties["binding_sha256"]["enum"] = [context["binding_sha256"]]
    if schema is RelevanceDecision:
        addresses = [block["address"] for block in context["structural_addresses"]]
        properties["evidence_addresses"]["items"]["enum"] = addresses
    return body


def _compile(
    capture: Path,
    output: Path,
    saved: dict[str, Any],
    context: dict[str, Any],
    proposal: PrivacyDecision,
) -> tuple[dict[str, Any], list[dict[str, Any]], PrivacyDecision, dict[str, bytes]]:
    if (
        proposal.identity != context["identity"]
        or proposal.binding_sha256 != context["binding_sha256"]
        or proposal.unresolved
    ):
        raise ValueError("privacy proposal is stale or unresolved")
    addressed = {choice.finding_id for choice in proposal.findings}
    if len(addressed) != len(proposal.findings) or addressed != {
        finding["id"] for finding in context["findings"]
    }:
        raise ValueError("privacy proposal must classify every finding exactly once")
    if any(choice.disposition == "unresolved" for choice in proposal.findings):
        raise ValueError("privacy finding is unresolved")
    metadata, body = apply_text(context["metadata"], context["body"], proposal)
    changes = {change.source_sha256: change for change in proposal.media_changes}
    if len(changes) != len(proposal.media_changes) or changes.keys() - context["media"].keys():
        raise ValueError("duplicate or unknown media transformation")
    values = {value.source_sha256: value.replacement for value in proposal.values}
    files: dict[str, bytes] = {}
    transforms = {}
    media = []
    media_locations = {}
    item = saved["item"]
    for original in item["media"]:
        raw = (capture / original["path"]).read_bytes()
        transformed = original["sha256"] in changes
        if transformed:
            raw, transform = preview_media(raw, changes[original["sha256"]], values)
            transforms[original["sha256"]] = transform
        digest = sha(raw)
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
        body = body.replace("assets/" + Path(original["path"]).name, "assets/" + digest + extension)
        media_locations[digest] = original["sha256"]
        media.append({**original, "path": relative, "sha256": digest})
    article = serialize_document(PageMetadata.model_validate(metadata), body).encode()
    files[item.get("published_path", item["article_path"])] = article
    # Build the exact candidate derivative/OCR privately; keep original media too.
    with tempfile.TemporaryDirectory(prefix="admission-candidate-") as directory:
        root = Path(directory)
        for relative, data in files.items():
            atomic_private(root / relative, data)
        visual, media_fields, receipts = _media_context(root, output, {"media": media})
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


def review_one(
    capture: Path, output: Path, record: Path, phase: str, client: CodexClient | None
) -> dict[str, Any]:
    saved = json.loads(record.read_bytes())
    context, visual = _context(capture, output, saved)
    if phase == "relevance":
        body = _request(EDITOR_MODEL, phase, context, visual, RelevanceDecision)
    else:
        relevance_body = _request(EDITOR_MODEL, "relevance", context, visual, RelevanceDecision)
        relevance = parse_response(
            ResponseEvidence.model_validate(saved["phases"]["relevance"]),
            RelevanceDecision,
            EDITOR_MODEL,
            request_hash(relevance_body),
        )
        addresses = {block["address"] for block in context["structural_addresses"]}
        if (
            relevance.identity != context["identity"]
            or relevance.binding_sha256 != context["binding_sha256"]
            or not relevance.reason.strip()
            or not relevance.evidence_addresses
            or set(relevance.evidence_addresses) - addresses
        ):
            raise ValueError("relevance decision lacks grounded complete-source evidence")
        if (
            relevance.disposition == "review"
            or relevance.extraction != "complete"
            or relevance.retirement == "uncertain"
        ):
            raise ValueError("source relevance or extraction requires exception review")
        context["relevance_decision"] = relevance.model_dump()
        if phase == "privacy":
            body = _request(EDITOR_MODEL, phase, context, visual, PrivacyDecision)
        else:
            privacy_body = _request(EDITOR_MODEL, "privacy", context, visual, PrivacyDecision)
            proposal = parse_response(
                ResponseEvidence.model_validate(saved["phases"]["privacy"]),
                PrivacyDecision,
                EDITOR_MODEL,
                request_hash(privacy_body),
            )
            candidate, candidate_visual, projected, files = _compile(
                capture, output, saved, context, proposal
            )
            context["privacy_decision"] = proposal.model_dump()
            context["candidate"] = candidate
            body = _request(
                VALIDATOR_MODEL,
                "validation",
                context,
                visual + candidate_visual,
                AdmissionValidation,
            )
            if phase == "replay":
                validation = parse_response(
                    ResponseEvidence.model_validate(saved["phases"]["validation"]),
                    AdmissionValidation,
                    VALIDATOR_MODEL,
                    request_hash(body),
                )
                validate_admission(
                    context["identity"],
                    context["binding_sha256"],
                    candidate["output_sha256"],
                    validation,
                )
                enforce(candidate["findings"], projected, validation)
                saved.update(
                    disposition="accepted"
                    if relevance.disposition == "include" and relevance.retirement != "excluded"
                    else "policy_excluded",
                    reason="independently_validated_source_admission",
                    output_sha256=candidate["output_sha256"],
                    file_hashes=candidate["file_hashes"],
                    transforms=candidate["media_transforms"],
                    enforcement_findings=0,
                )
                if saved["disposition"] == "accepted":
                    for path, data in files.items():
                        published = Path(path).relative_to("articles")
                        atomic_private(output / "reviewed" / published, data)
                atomic_private(record, json_bytes(saved))
                return dict(saved)
    existing = saved["phases"].get(phase)
    if (
        existing
        and existing.get("error") is None
        and existing["request_sha256"] == request_hash(body)
    ):
        try:
            schemas: dict[str, type[BaseModel]] = {
                "relevance": RelevanceDecision,
                "privacy": PrivacyDecision,
                "validation": AdmissionValidation,
            }
            parse_response(
                ResponseEvidence.model_validate(existing),
                schemas[phase],
                body["model"],
                request_hash(body),
            )
            return dict(saved)
        except ValueError:
            atomic_private(
                output / "history" / (sha(json_bytes(existing)) + ".json"), json_bytes(existing)
            )
    if client is None:
        raise ValueError("inference phase requires the MacBook Codex client")
    saved["phases"][phase] = client.execute(body).model_dump()
    atomic_private(record, json_bytes(saved))
    return dict(saved)


def run_reviews(capture: Path, output: Path, *, phase: str, journal: Path) -> dict[str, Any]:
    if phase == "prepare":
        return prepare_reviews(capture, output)
    if phase not in {"relevance", "privacy", "validation", "replay", "all"}:
        raise ValueError(
            "review phase must be prepare, relevance, privacy, validation, replay or all"
        )
    inventory = json.loads((output / "review-inventory.json").read_bytes())
    records = [output / record["path"] for record in inventory["documents"] if "path" in record]
    # OCR and shared visual inputs are prepared once before concurrent inference.
    ready = []
    failures: dict[str, str] = {}
    for record in records:
        saved = json.loads(record.read_bytes())
        try:
            _media_context(capture, output, saved["item"])
            ready.append(record)
        except (ValueError, OSError) as failure:
            failures[record.name] = "MediaPreparationError"
            atomic_private(
                output / "errors" / (record.name + ".json"),
                json_bytes({"phase": "media", "error": str(failure)}),
            )
    client = None if phase == "replay" else CodexClient(journal, concurrency=4)
    phases = ["relevance", "privacy", "validation", "replay"] if phase == "all" else [phase]
    try:
        for current in phases:
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = {
                    pool.submit(review_one, capture, output, record, current, client): record
                    for record in ready
                }
                for future in as_completed(futures):
                    record = futures[future]
                    try:
                        future.result()
                        failures.pop(record.name, None)
                    except (ValueError, KeyError, OSError) as failure:
                        failures[record.name] = type(failure).__name__
                        atomic_private(
                            output / "errors" / (record.name + ".json"),
                            json_bytes({"phase": current, "error": str(failure)}),
                        )
    finally:
        if client is not None:
            client.close()
    result: dict[str, Any] = {
        "schema_version": 1,
        "phase": phase,
        "documents": [],
        "exceptions": failures,
    }
    for record in records:
        saved = json.loads(record.read_bytes())
        if record.name in failures:
            _retire_output(output, saved)
            saved.update(disposition="queued", reason="admission_phase_failed")
            atomic_private(record, json_bytes(saved))
        result["documents"].append(
            {
                "identity": saved["item"]["identity"],
                "url": saved["item"]["url"],
                "disposition": saved["disposition"],
                "reason": saved["reason"],
                "binding_sha256": saved["binding_sha256"],
            }
        )
    atomic_private(output / "review-report.json", json_bytes(result))
    return result


def verify_reviews(capture: Path, output: Path) -> dict[str, Any]:
    """Replay complete source admission without inference; inspect retained bytes."""
    inventory = json.loads((output / "review-inventory.json").read_bytes())
    expected = {}
    for record in inventory["documents"]:
        if "path" not in record:
            continue
        path = output / record["path"]
        saved = json.loads(path.read_bytes())
        if saved["disposition"] not in {"accepted", "policy_excluded"}:
            continue
        # Replay into an isolated copy so verification never repairs its target.
        with tempfile.TemporaryDirectory(prefix="source-review-replay-") as directory:
            scratch = Path(directory)
            shutil.copytree(output / "media", scratch / "media") if (
                output / "media"
            ).exists() else None
            copy = scratch / "document.json"
            atomic_private(copy, path.read_bytes())
            actual = review_one(capture, scratch, copy, "replay", None)
            if actual != saved:
                raise ValueError("source admission replay differs from receipt")
        if saved["disposition"] == "accepted":
            for relative, digest in saved["file_hashes"].items():
                relative = Path(relative).relative_to("articles").as_posix()
                if sha((output / "reviewed" / relative).read_bytes()) != digest:
                    raise ValueError("reviewed article or media bytes changed")
                expected[relative] = digest
    actual = {
        path.relative_to(output / "reviewed").as_posix(): sha(path.read_bytes())
        for path in (output / "reviewed").rglob("*")
        if path.is_file()
    }
    if expected != actual:
        raise ValueError("source reviewed inventory differs from accepted admissions")
    return {
        "accepted_files": expected,
        "inventory_sha256": sha((output / "review-inventory.json").read_bytes()),
    }
