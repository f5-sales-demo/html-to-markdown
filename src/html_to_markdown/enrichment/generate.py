"""Phase-based whole-corpus model orchestration; inference is separate from replay."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from ..curation import corpus_digest, json_bytes
from .analysis import analyze, sha
from .codex_client import CodexClient
from .contracts import EDITOR_MODEL, Artifact, Decision, ImageCollection, ResponseEvidence
from .engine import context_for
from .gates import candidate, parse_response, request_hash
from .media import image_input
from .requests import editor_request, image_collection_request, validator_request


# Phase orchestration keeps identity, visual evidence and receipts in one transaction.
# pylint: disable-next=too-many-locals,too-many-branches,too-many-statements
def generate(
    curated: Path, destination: Path, *, phase: str, journal: Path, wait: bool = False
) -> dict[str, Any]:
    artifact = Artifact.model_validate_json(destination.read_bytes())
    analysis = analyze(curated)
    if (
        artifact.curated_sha256 != corpus_digest(curated)
        or sha(json_bytes(analysis)) != artifact.analysis_sha256
    ):
        raise ValueError("input changed since preparation")
    documents = {d["document"]: d for d in analysis["documents"]}
    records = {d.document: d for d in artifact.documents}
    requests: dict[str, dict[str, Any]] = {}
    identities = {}
    if phase == "media":
        unique: dict[str, Path] = {}
        for path, digest in artifact.assets.items():
            unique.setdefault(digest, curated / path)
        client = CodexClient(journal)
        for cached in sorted(journal.glob("*.codex.json")):
            evidence = ResponseEvidence.model_validate_json(cached.read_bytes())
            if evidence.error is None:
                texts = [
                    c.get("text", "")
                    for item in evidence.request.get("input", [])
                    for c in item.get("content", [])
                    if c.get("type") == "input_text"
                ]
                if texts:
                    try:
                        metadata = __import__("json").loads(texts[0])
                        digest = metadata.get("sha256")
                        if digest in unique:
                            artifact.images[digest] = evidence
                    except ValueError:
                        pass
        remaining = [
            (digest, path)
            for digest, path in sorted(unique.items())
            if digest not in artifact.images and digest not in artifact.image_groups
        ]
        groups = [remaining[i : i + 8] for i in range(0, len(remaining), 8)]

        def inspect_group(group: list[tuple[str, Path]]) -> dict[str, ResponseEvidence]:
            inputs = []
            for digest, path in group:
                try:
                    data_url, ocr = image_input(path)
                    inputs.append((digest, data_url, ocr))
                except (ValueError, OSError):
                    continue
            if not inputs:
                return {}
            body = image_collection_request(inputs)
            evidence = client.execute(body)
            try:
                collection = parse_response(
                    evidence, ImageCollection, EDITOR_MODEL, request_hash(body)
                )
                if {image.sha256 for image in collection.images} != {
                    digest for digest, _, _ in inputs
                }:
                    raise ValueError("incomplete grouped media response")
                return {
                    image.sha256: evidence.model_copy(update={"output_selector": image.sha256})
                    for image in collection.images
                }
            except ValueError:
                return {}

        with ThreadPoolExecutor(max_workers=12) as pool:
            futures = [pool.submit(inspect_group, group) for group in groups]
            for index, future in enumerate(as_completed(futures), 1):
                completed = future.result()
                for digest, evidence in completed.items():
                    artifact.image_groups[digest] = evidence.request_sha256
                    artifact.images[evidence.request_sha256] = evidence.model_copy(
                        update={"output_selector": None}
                    )
                print(
                    f"Media groups {index}/{len(groups)}; reviewed {len(set(unique) & (set(artifact.images) | set(artifact.image_groups)))}/{len(unique)}",
                    flush=True,
                )
                if index % 5 == 0 or index == len(groups):
                    temporary = destination.with_suffix(".tmp")
                    temporary.write_bytes(json_bytes(artifact.model_dump()))
                    temporary.replace(destination)
        return {"phase": phase, "requested": len(unique), "returned": len(artifact.images)}
    if phase in {"editor", "validator"}:
        for path, doc in documents.items():
            context = context_for(doc, analysis, artifact)
            identity = sha(path)
            identities[identity] = path
            if phase == "editor":
                requests[identity] = editor_request(doc, context)
            else:
                editor_evidence = records[path].editor
                if editor_evidence is None:
                    continue
                try:
                    decision = parse_response(
                        editor_evidence,
                        Decision,
                        EDITOR_MODEL,
                        request_hash(editor_request(doc, context)),
                    )
                    body = candidate(doc, decision, documents)
                    requests[identity] = validator_request(
                        doc, body, decision.model_dump(), context
                    )
                except (ValueError, TypeError, KeyError):
                    # Rejected edits still get a whole-document independent review
                    # as retained originals; the failed editor response remains
                    # in the artifact and can never bypass deterministic checks.
                    requests[identity] = validator_request(
                        doc, doc["body"], {"rejected_editor": True}, context
                    )
    else:
        raise ValueError("phase must be media, editor, or validator")
    client = CodexClient(journal)
    try:
        results = client.run(requests, wait=wait)
    finally:
        client.close()
    if phase == "media":
        artifact.images.update(results)
    else:
        for identity, result in results.items():
            record = records[identities[identity]]
            if phase == "editor":
                record.editor = result
                record.validator = None
            else:
                record.validator = result
    destination.write_bytes(json_bytes(artifact.model_dump()))
    return {
        "phase": phase,
        "requested": len(requests),
        "returned": len(results),
        "pending": len(requests) - len(results),
        "artifact_sha256": sha(destination.read_bytes()),
    }
