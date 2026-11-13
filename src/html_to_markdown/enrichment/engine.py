"""Prepare, independently validate and replay a digest-pinned corpus transaction."""

# Receipt verification imports the package module only at runtime after initialization.
# pylint: disable=cyclic-import

import gzip
import json
import shutil
from pathlib import Path
from typing import Any

from ..adapters.community import privacy_findings
from ..content_policy import load_content_policy, validate_content_policy
from ..curation import corpus_digest, json_bytes, load_curation_policy, prune_assets
from ..metadata import rebuild_metadata
from ..models import DiscoveredPage, PageMetadata, PageStatus
from ..render import serialize_document, split_document
from ..state import StateStore
from ..validation import validate_document
from .analysis import analyze, links, sha
from .canonical import canonical_request
from .contracts import (
    EDITOR_MODEL,
    PROMPT_VERSION,
    Artifact,
    CanonicalDecision,
    Decision,
    DocumentEvidence,
    ImageAnalysis,
    ResponseEvidence,
    Validation,
)
from .contracts_v2 import VERSION as VERSION_TWO
from .gates import candidate, parse_response, request_hash, validate_candidate
from .graph import graph_fallbacks
from .model_policy import compatible_request
from .requests import editor_evidence_hash, validator_request

MARKER = "enrichment-state.json"
_ARTIFACT_CACHE: dict[str, Artifact] = {}
_DECISION_CACHE: dict[tuple[str, str], tuple[dict[str, Any], list[dict[str, Any]]]] = {}
_ARTIFACT_IDENTITIES: dict[int, str] = {}
_UPSTREAM_POLICY_CACHE: set[tuple[str, ...]] = set()


def import_state(output: Path, store: StateStore) -> None:
    """Import original manifest provenance without replacing existing runtime rows."""
    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.is_file() else {}
    provenance = {d["url"]: d.get("provenance", {}) for d in manifest.get("documents", [])}
    existing = {r["canonical_url"] for r in store.rows()}
    for path in sorted(output.glob("content/*/**/index.md")):
        metadata, body = split_document(path.read_text())
        url = str(metadata["url"])
        if url in existing:
            continue
        store.discover([DiscoveredPage(source_id=str(metadata["sourceId"]), url=url)])
        prior = provenance.get(url, {})
        freshness = prior.get("freshness", "fresh")
        status = {
            "carried_forward": PageStatus.CARRIED_FORWARD,
            "removal_candidate": PageStatus.REMOVAL_CANDIDATE,
        }.get(freshness, PageStatus.SUCCESS)
        failure = prior.get("current_failure") or {}
        store.reconcile(
            url,
            status,
            output_path=path.relative_to(output).as_posix(),
            digest=sha(body),
            freshness=freshness,
            last_success_at=prior.get("last_success_at"),
            failure_classification=failure.get("classification"),
            consecutive_failures=prior.get("consecutive_failure_count", 0),
            terminal_confirmations=prior.get("terminal_confirmation_count", 0),
        )
        with store.connection:
            store.connection.execute(
                "UPDATE pages SET error_class=?, error_message=? WHERE canonical_url=?",
                (failure.get("error_class"), failure.get("error_message"), url),
            )
        store.replace_candidate_links(url, links(body))


def context_for(
    doc: dict[str, Any], analysis: dict[str, Any], artifact: Artifact
) -> dict[str, Any]:
    images = {}
    for media in doc["media"]:
        digest = media["sha256"]
        evidence = artifact.images.get(digest) or artifact.images.get(
            artifact.image_groups.get(digest, "")
        )
        if evidence is not None and digest in artifact.image_groups:
            evidence = evidence.model_copy(update={"output_selector": digest})
        if evidence:
            content = [
                c
                for item in evidence.response.get("output", [])
                for c in item.get("content", [])
                if c.get("type") == "output_text"
            ]
            if evidence.error or len(content) != 1:
                images[digest] = {"uncertain": True, "reason": "image_analysis_failed"}
            else:
                try:
                    parsed = parse_response(
                        evidence, ImageAnalysis, EDITOR_MODEL, evidence.request_sha256
                    )
                    if parsed.sha256 != digest:
                        raise ValueError("image digest mismatch")
                    images[digest] = parsed.model_dump()
                except ValueError:
                    images[digest] = {"uncertain": True, "reason": "invalid_image_analysis"}
        else:
            images[digest] = {"uncertain": True, "reason": "visual_evidence_unavailable"}
    canonical_candidates = [
        d
        for d in analysis["documents"]
        if (d["metadata"].get("canonical_url") or d["metadata"]["url"])
        == (doc["metadata"].get("canonical_url") or doc["metadata"]["url"])
    ]
    canonical_target = next(
        (d["document"] for d in canonical_candidates if "/docs/" not in d["document"]),
        sorted(d["document"] for d in canonical_candidates)[0],
    )
    result = {
        "repeated_passages": [
            g
            for g in analysis["exact_repetition"]
            if any(o["document"] == doc["document"] for o in g["occurrences"])
        ],
        "near_duplicates": [
            g
            for g in analysis["near_repetition"]
            if any(o["document"] == doc["document"] for o in g["occurrences"])
        ],
        "canonical_candidates": [
            d
            for d in analysis["documents"]
            if d["document"] != doc["document"]
            and (d["metadata"].get("canonical_url") or d["metadata"]["url"])
            == (doc["metadata"].get("canonical_url") or doc["metadata"]["url"])
        ],
        "image_analysis": images,
        "image_visual_inputs": _visual_inputs(doc, artifact),
        "authoritative_repairs": {
            key: repair
            for key, repair in artifact.repairs.items()
            if repair.get("document") == doc["document"]
        },
    }
    if len(canonical_candidates) > 1:
        result["recommended_canonical_document"] = canonical_target
        result["canonical_instruction"] = (
            "Use this one target consistently. The target document must be retained, and only another exact duplicate may be an alias. Preserve unresolved technical content exactly; do not rewrite aliases."
        )
    return result


def _visual_inputs(doc: dict[str, Any], artifact: Artifact) -> list[dict[str, Any]]:
    inputs: list[dict[str, Any]] = []
    for media in doc["media"]:
        digest = media["sha256"]
        evidence = artifact.images.get(digest)
        if evidence is not None:
            inputs.extend(
                c
                for item in evidence.request.get("input", [])
                for c in item.get("content", [])
                if c.get("type") == "input_image"
            )
            continue
        grouped = artifact.images.get(artifact.image_groups.get(digest, ""))
        if grouped is not None:
            for item in grouped.request.get("input", []):
                selected = False
                for content in item.get("content", []):
                    if content.get("type") == "input_text":
                        selected = content.get("text") == "Image digest: " + digest
                    elif selected and content.get("type") == "input_image":
                        inputs.append(content)
                        selected = False
    return inputs


def prepare(
    curated: Path,
    baseline: Path,
    destination: Path,
    *,
    tag: str,
    receipt_sha256: str,
    version: int = 1,
) -> Artifact:
    """Bind model work to verified captured and policy-curated inputs."""
    # Package imports curation; defer this edge until the verified-input boundary.
    from ..package import (  # pylint: disable=import-outside-toplevel
        verify_archive,
        verify_publication_receipt,
    )

    receipt_path = baseline / "publication.json"
    if sha(receipt_path.read_bytes()) != receipt_sha256:
        raise ValueError("baseline publication digest mismatch")
    receipt = json.loads(receipt_path.read_text())
    verify_publication_receipt(
        baseline,
        receipt,
        expected_tag=tag,
        actual_asset_names={
            p.name for p in baseline.iterdir() if p.is_file() and p.name != "SHA256SUMS"
        },
    )
    verify_archive(baseline / "html-to-markdown-content.tar.gz")
    if (curated / MARKER).exists():
        raise ValueError("prepare requires preserved pre-enrichment inputs")
    validate_content_policy(curated)
    analysis = analyze(curated, address_version=2 if version == 2 else 1)
    assets = {
        p.relative_to(curated).as_posix(): sha(p.read_bytes())
        for p in sorted(curated.glob("content/*/**/assets/*"))
    }
    if version not in {1, 2}:
        raise ValueError("unsupported enrichment schema version")
    artifact = Artifact(
        schema_version=2 if version == 2 else 1,
        structural_address_version=2 if version == 2 else 1,
        prompt_version=VERSION_TWO if version == 2 else PROMPT_VERSION,
        baseline_tag=tag,
        publication_sha256=receipt_sha256,
        baseline_manifest_sha256=sha((baseline / "manifest.json").read_bytes()),
        curated_sha256=corpus_digest(curated),
        analysis_sha256=sha(json_bytes(analysis)),
        policy_sha256=load_curation_policy().sha256,
        api_policy_sha256=load_content_policy().policy_digest,
        curation_audit_sha256=sha((curated / "curation-audit.json").read_bytes())
        if version == 2
        else None,
        documents=[
            DocumentEvidence(
                document=d["document"],
                source=str(d["metadata"]["sourceId"]),
                input_sha256=d["input_sha256"],
                editor=None,
                validator=None,
            )
            for d in analysis["documents"]
        ],
        assets=assets,
        images={},
        repairs={},
        pricing={},
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(json_bytes(artifact.model_dump()))
    return artifact


def load_artifact(path: Path) -> Artifact:
    raw = path.read_bytes()
    identity = sha(raw)
    if identity in _ARTIFACT_CACHE:
        copy = _ARTIFACT_CACHE[identity].model_copy(deep=True)
        _ARTIFACT_IDENTITIES[id(copy)] = identity
        return copy
    if raw.startswith(b"\x1f\x8b"):
        raw = gzip.decompress(raw)
    parsed = json.loads(raw)
    visual_inputs = parsed.pop("shared_visual_inputs", {})
    if visual_inputs:

        def restore(value: Any) -> Any:
            if isinstance(value, str) and value.startswith("enrichment-image://"):
                digest = value.removeprefix("enrichment-image://")
                image = visual_inputs.get(digest)
                if image is None or sha(image) != digest:
                    raise ValueError("shared visual input digest mismatch")
                return image
            if isinstance(value, dict):
                return {key: restore(item) for key, item in value.items()}
            if isinstance(value, list):
                return [restore(item) for item in value]
            return value

        parsed = restore(parsed)
    artifact = Artifact.model_validate(parsed)
    _ARTIFACT_CACHE[identity] = artifact.model_copy(deep=True)
    _ARTIFACT_IDENTITIES[id(artifact)] = identity
    return artifact


def artifact_bytes(artifact: Artifact) -> bytes:
    """Store each visual input once without changing model request identities."""
    visual_inputs: dict[str, str] = {}

    def deduplicate(value: Any) -> Any:
        if isinstance(value, str) and value.startswith("data:image/"):
            digest = sha(value)
            visual_inputs[digest] = value
            return "enrichment-image://" + digest
        if isinstance(value, dict):
            return {key: deduplicate(item) for key, item in value.items()}
        if isinstance(value, list):
            return [deduplicate(item) for item in value]
        return value

    data = deduplicate(artifact.model_dump())
    data["shared_visual_inputs"] = visual_inputs
    return json_bytes(data)


# The complete acceptance inventory and its source/evidence guards are one transaction.
# pylint: disable-next=too-many-locals,too-many-branches,too-many-statements
def _decisions(curated: Path, artifact: Artifact) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    current_digest = corpus_digest(curated)
    if (
        artifact.prompt_version != (VERSION_TWO if artifact.schema_version == 2 else PROMPT_VERSION)
        or artifact.curated_sha256 != current_digest
    ):
        raise ValueError("stale prompt or changed curated inputs")
    if artifact.schema_version == 2 and artifact.curation_audit_sha256 != sha(
        (curated / "curation-audit.json").read_bytes()
    ):
        raise ValueError("upstream curation audit changed")
    if (
        artifact.policy_sha256 != load_curation_policy().sha256
        or artifact.api_policy_sha256 != load_content_policy().policy_digest
    ):
        raise ValueError("stale upstream policy chain")
    identity = _ARTIFACT_IDENTITIES.get(id(artifact))
    cache_key = (identity, current_digest) if identity is not None else None
    if cache_key is not None and cache_key in _DECISION_CACHE:
        return _DECISION_CACHE[cache_key]
    analysis = analyze(curated, address_version=artifact.structural_address_version)
    if sha(json_bytes(analysis)) != artifact.analysis_sha256:
        raise ValueError("structural analysis changed")
    indexed = {d["document"]: d for d in analysis["documents"]}
    records = {d.document: d for d in artifact.documents}
    if len(records) != len(artifact.documents) or set(records) != set(indexed):
        raise ValueError("decision artifact must cover every input document exactly once")
    results = []
    for path, doc in indexed.items():
        evidence = records[path]
        result: dict[str, Any] = {
            "document": path,
            "source": evidence.source,
            "input_sha256": doc["input_sha256"],
            "disposition": "fallback",
            "body": doc["body"],
            "description": doc["metadata"].get("description"),
            "canonical_document": None,
        }
        try:
            if (
                evidence.input_sha256 != doc["input_sha256"]
                or evidence.source != doc["metadata"]["sourceId"]
            ):
                raise ValueError("stale per-document identity")
            if artifact.schema_version == 2:
                from .v2 import replay_document  # pylint: disable=import-outside-toplevel

                progress = evidence.v2
                if progress is None or progress.get("selected_attempt") is None:
                    raise ValueError(
                        (progress or {}).get("failure_reason")
                        or "missing complete version-two model decisions"
                    )
                selected = progress["selected_attempt"]
                if (
                    not isinstance(selected, int)
                    or isinstance(selected, bool)
                    or not 0 <= selected < len(progress["attempts"])
                ):
                    raise ValueError("invalid selected version-two attempt")
                attempt = progress["attempts"][selected]
                decision, body, validation = replay_document(
                    doc, attempt, indexed, artifact.repairs
                )
                validation_digest = attempt["validation"]["response_sha256"]
            else:
                if evidence.editor is None or evidence.validator is None:
                    raise ValueError("missing editor or independent validator response")
                context = context_for(doc, analysis, artifact)
                decision = parse_response(
                    evidence.editor,
                    Decision,
                    EDITOR_MODEL,
                    editor_evidence_hash(doc, context, evidence.editor),
                )
                body = candidate(doc, decision, indexed, artifact.repairs)
                if privacy_findings(body):
                    raise ValueError("candidate contains unresolved privacy findings")
                validator_body = compatible_request(
                    validator_request(doc, body, decision.model_dump(), context),
                    evidence.validator.request,
                )
                validation = parse_response(
                    evidence.validator,
                    Validation,
                    validator_body["model"],
                    request_hash(validator_body),
                )
                validate_candidate(doc, body, validation)
                for media in decision.media:
                    if media.disposition == "remove" or media.alt != "":
                        image = context["image_analysis"].get(media.sha256, {})
                        if image.get("uncertain", True):
                            raise ValueError("media change lacks verified visual evidence")
                validation_digest = evidence.validator.response_sha256
            policy = load_curation_policy()
            if policy.retired(body) or policy.retired(decision.description):
                raise ValueError("candidate reintroduces retired material")
            result.update(
                disposition=decision.disposition,
                body=body,
                description=decision.description,
                canonical_document=decision.canonical_document,
                reason=decision.reason,
                validation_sha256=validation_digest,
            )
        except (ValueError, TypeError, KeyError) as error:
            result["reason"] = str(error)
        results.append(result)
    # Do not alias through another alias or an excluded target.
    resolved = {r["document"]: r for r in results}
    for result in results:
        if result["disposition"] == "alias" and resolved[result["canonical_document"]][
            "disposition"
        ] not in {"retain", "rewrite", "fallback"}:
            result.update(
                disposition="fallback",
                reason="canonical target is not retained",
                canonical_document=None,
            )
    for review in artifact.canonical_reviews:
        try:
            source = indexed[review.document]
            target = indexed[review.canonical_document]

            def canonical_identity(doc: dict[str, Any]) -> Any:
                return doc["metadata"].get("canonical_url") or doc["metadata"]["url"]

            if (
                canonical_identity(source) != canonical_identity(target)
                or source["body"] != target["body"]
            ):
                raise ValueError("canonical source content differs")
            source_media = {m["sha256"] for m in source["media"]}
            target_media = {m["sha256"] for m in target["media"]}
            if None in source_media or source_media != target_media:
                raise ValueError("canonical media differs or is unavailable")
            for canonical_evidence, role in (
                (review.editor, "editor"),
                (review.validator, "validator"),
            ):
                expected = compatible_request(
                    canonical_request(source, target, EDITOR_MODEL, role=role),
                    canonical_evidence.request,
                )
                check = parse_response(
                    canonical_evidence,
                    CanonicalDecision,
                    expected["model"],
                    request_hash(expected),
                )
                identity_matches = (
                    check.document != source["document"]
                    or check.canonical_document != target["document"]
                    or check.input_sha256 != source["input_sha256"]
                    or check.canonical_input_sha256 != target["input_sha256"]
                )
                preservation_passed = all(
                    (
                        check.approved,
                        check.canonical_identity_matches,
                        check.complete_substantive_text_matches,
                        check.media_bytes_match,
                        check.preserve_all_existing_routes,
                        check.no_technical_repair_proposed,
                    )
                )
                if identity_matches or not preservation_passed or check.preservation_failures:
                    raise ValueError("independent canonical preservation failed")
            if resolved[target["document"]]["disposition"] in {"alias", "exclude_shell"}:
                raise ValueError("canonical target unavailable")
            resolved[source["document"]].update(
                disposition="alias",
                canonical_document=target["document"],
                body=source["body"],
                reason="exact canonical content independently verified by both models",
            )
        except (KeyError, ValueError, TypeError):
            continue
    graph_fallbacks(analysis, results)
    if cache_key is not None:
        _DECISION_CACHE[cache_key] = (analysis, results)
    return analysis, results


# Preserve the complete source/evidence/output transaction before publication.
# pylint: disable-next=too-many-locals,too-many-branches,too-many-statements
def replay(output: Path, artifact_path: Path, digest: str) -> dict[str, Any]:
    """Apply only gated decisions. Preserve original bytes and reversible evidence."""
    if sha(artifact_path.read_bytes()) != digest:
        raise ValueError("enrichment artifact digest mismatch")
    if (output / MARKER).exists():
        state = verify_enrichment(output)
        if state["artifact_sha256"] != digest:
            raise ValueError("cannot replace already-pinned decisions")
        return dict(state["report"])
    if not (output / "curation-audit.json").is_file():
        raise ValueError("replay requires an upstream curated snapshot and audit")
    if (output / "refresh-state.json").is_file():
        from ..refresh import verify_refresh  # pylint: disable=import-outside-toplevel

        verify_refresh(output)
    artifact = load_artifact(artifact_path)
    validate_content_policy(output)
    analysis, results = _decisions(output, artifact)
    preserved = output / ".enrichment" / "curated"
    preserved.mkdir(parents=True, exist_ok=True)
    shutil.copytree(output / "content", preserved / "content", dirs_exist_ok=True)
    for name in ("manifest.json", "curation-audit.json"):
        if (output / name).is_file():
            shutil.copy2(output / name, preserved / name)
    for name in ("refresh-state.json", "source-review-receipt.json"):
        if (output / name).is_file():
            shutil.copy2(output / name, preserved / name)
    for name in (".refresh", ".admission"):
        if (output / name).is_dir():
            shutil.copytree(output / name, preserved / name)
    pinned = output / ".enrichment" / "decisions.json"
    pinned.write_bytes(artifact_path.read_bytes())
    docs = {d["document"]: d for d in analysis["documents"]}
    aliases = []
    store = StateStore(output / "state.sqlite")
    try:
        import_state(output, store)
        for result in results:
            path = output / result["document"]
            metadata = PageMetadata.model_validate(docs[result["document"]]["metadata"])
            if result["disposition"] in {"alias", "exclude_shell"}:
                path.unlink()
                with store.connection:
                    store.connection.execute(
                        "DELETE FROM pages WHERE canonical_url=?", (metadata.url,)
                    )
                if result["disposition"] == "alias":
                    aliases.append(
                        {
                            "path": result["document"],
                            "target": result["canonical_document"],
                            "url": metadata.url,
                        }
                    )
                continue
            if result["disposition"] != "fallback":
                metadata.description = result["description"]
                path.write_text(serialize_document(metadata, result["body"]), encoding="utf-8")
            store.replace_candidate_links(metadata.url, links(result["body"]))
        # Relationships are reconstructed from retained text; historic related
        # links may otherwise recreate unrelated relationships.
        for path in sorted(output.glob("content/*/**/index.md")):
            meta, body = split_document(path.read_text())
            metadata = PageMetadata.model_validate(meta)
            metadata.related_documents = []
            path.write_text(serialize_document(metadata, body), encoding="utf-8")
        prune_assets(output)
        rebuild_metadata(output, store)
    finally:
        store.close()
    # A malformed candidate falls back BEFORE any output mutation; public
    # validators remain mandatory and publication fails on invalid graph edits.
    for path in sorted(output.glob("content/*/**/index.md")):
        errors = validate_document(path)
        if errors:
            raise ValueError("enriched document integrity failed: " + errors[0])
    summary = {
        key: sum(r["disposition"] == key for r in results)
        for key in ("rewrite", "retain", "fallback", "exclude_shell", "alias")
    }
    input_tokens = sum(d["estimated_tokens"] for d in analysis["documents"])
    output_tokens = sum(
        (len(split_document(p.read_text())[1]) + 3) // 4
        for p in output.glob("content/*/**/index.md")
    )
    usage: dict[str, dict[str, int | float | None]] = {}
    responses = [
        *artifact.images.values(),
        *(e for d in artifact.documents for e in (d.editor, d.validator) if e is not None),
    ]

    def collect_usage(value: Any) -> None:
        if isinstance(value, dict):
            if {
                "request_sha256",
                "response_sha256",
                "request",
                "response",
                "model",
            } <= value.keys():
                responses.append(ResponseEvidence.model_validate(value))
            else:
                for child in value.values():
                    collect_usage(child)
        elif isinstance(value, list):
            for child in value:
                collect_usage(child)

    for document in artifact.documents:
        collect_usage(document.v2)
    seen_responses = set()
    for response in responses:
        response_identity = (response.request_sha256, response.response_sha256)
        if response_identity in seen_responses:
            continue
        seen_responses.add(response_identity)
        billed = response.response.get("usage") or {}
        totals = usage.setdefault(
            response.model,
            {"input_tokens": 0, "cached_tokens": 0, "output_tokens": 0, "cost_usd": None},
        )
        totals["input_tokens"] = int(totals["input_tokens"] or 0) + int(
            billed.get("input_tokens", 0)
        )
        totals["cached_tokens"] = int(totals["cached_tokens"] or 0) + int(
            (billed.get("input_tokens_details") or {}).get("cached_tokens", 0)
        )
        totals["output_tokens"] = int(totals["output_tokens"] or 0) + int(
            billed.get("output_tokens", 0)
        )
    for model, totals in usage.items():
        rates = artifact.pricing.get(model)
        if rates and set(rates) == {"input", "cached_input", "output"}:
            totals["cost_usd"] = (
                (float(totals["input_tokens"] or 0) - float(totals["cached_tokens"] or 0))
                * rates["input"]
                + float(totals["cached_tokens"] or 0) * rates["cached_input"]
                + float(totals["output_tokens"] or 0) * rates["output"]
            ) / 1_000_000
    report = {
        "schema_version": 1,
        "baseline_tag": artifact.baseline_tag,
        "publication_sha256": artifact.publication_sha256,
        "counts": summary,
        "input_documents": len(results),
        "estimated_input_tokens": input_tokens,
        "estimated_output_tokens": output_tokens,
        "estimated_token_savings": input_tokens - output_tokens,
        "verified_repairs": [],
        "unresolved_media": artifact.unresolved_media,
        "model_usage": usage,
        "cost_status": "priced"
        if usage and all(v["cost_usd"] is not None for v in usage.values())
        else "unpriced",
        "aliases": aliases,
        "documents": [
            {k: v for k, v in r.items() if k not in {"body", "description"}} for r in results
        ],
    }
    state = {
        "schema_version": 1,
        "artifact_sha256": digest,
        "curated_sha256": artifact.curated_sha256,
        "output_sha256": corpus_digest(output),
        "report": report,
    }
    (output / MARKER).write_bytes(json_bytes(state))
    (output / ".enrichment" / "report.json").write_bytes(json_bytes(report))
    (output / ".enrichment" / "report.md").write_text(
        "# Enrichment report\n\n"
        + "\n".join(f"- {key}: {value}" for key, value in summary.items())
        + f"\n\nEstimated token savings: {input_tokens - output_tokens}.\n\n"
        + "\n".join(f"- {r['document']}: {r['disposition']} ({r['reason']})" for r in results)
        + "\n"
    )
    for name in ("quality-report.json", "quality-report.md"):
        (output / name).unlink(missing_ok=True)
    verify_enrichment(output)
    return report


def verify_enrichment(output: Path) -> dict[str, Any]:
    """Verify the original policy chain and independently regenerate all decisions."""
    state = json.loads((output / MARKER).read_text())
    pinned = output / ".enrichment" / "decisions.json"
    if sha(pinned.read_bytes()) != state["artifact_sha256"]:
        raise ValueError("pinned decision artifact changed")
    artifact = load_artifact(pinned)
    curated = output / ".enrichment" / "curated"
    if (curated / "refresh-state.json").is_file():
        from ..refresh import verify_refresh  # pylint: disable=import-outside-toplevel

        verify_refresh(curated)
    upstream_key = (
        corpus_digest(curated),
        load_curation_policy().sha256,
        load_content_policy().policy_digest,
        sha((curated / "curation-audit.json").read_bytes()),
    )
    if upstream_key not in _UPSTREAM_POLICY_CACHE:
        validate_content_policy(curated)
        _UPSTREAM_POLICY_CACHE.add(upstream_key)
    analysis, results = _decisions(curated, artifact)
    expected = {
        r["document"]: r for r in results if r["disposition"] not in {"alias", "exclude_shell"}
    }
    actual = {p.relative_to(output).as_posix(): p for p in output.glob("content/*/**/index.md")}
    if set(actual) != set(expected):
        raise ValueError("enriched inventory does not match gated decisions")
    for path, result in expected.items():
        meta, body = split_document(actual[path].read_text())
        if body != result["body"] or meta.get("description") != result["description"]:
            raise ValueError("enriched body or grounded description changed")
    if corpus_digest(output) != state["output_sha256"]:
        raise ValueError("enriched metadata or image bytes changed")
    actual_assets = {
        p.relative_to(output).as_posix(): sha(p.read_bytes())
        for p in output.glob("content/*/**/assets/*")
    }
    if any(artifact.assets.get(path) != digest for path, digest in actual_assets.items()):
        raise ValueError("retained image bytes changed")
    return dict(state)
