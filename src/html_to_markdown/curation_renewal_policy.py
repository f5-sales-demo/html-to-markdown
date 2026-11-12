"""Compile reviewed catalog guards and qualify the real whole-corpus planner."""

from __future__ import annotations

import copy
import json
import tempfile
from pathlib import Path
from typing import Any

from .content_inventory import atomic_private
from .content_policy import load_content_policy, migrate_content, validate_content_policy
from .curation import (
    CurationPolicy,
    canonical,
    corpus_digest,
    curation_validation_errors,
    json_bytes,
    load_curation_policy,
)
from .curation_renewal_jobs import replay_renewal
from .enrichment.analysis import sha
from .refresh import _verified_baseline
from .render import split_document
from .source_review import scan, text_fields
from .source_review_jobs import _media_context


class DependencyReviewRequired(ValueError):
    """All unreviewed baseline dependencies must independently qualify before promotion."""

    def __init__(self, documents: list[str]) -> None:
        super().__init__("independent whole-corpus dependency review required")
        self.documents = documents


def policy_files(raw: dict[str, Any]) -> dict[str, bytes]:
    """Keep external catalogs and evidence pins; only render reviewed decisions."""
    source = Path(__file__).parent
    original = json.loads((source / "curation_policy.json").read_bytes())
    files = {}
    for topic in raw["topics"]:
        destination = next(t for t in original["topics"] if t["id"] == topic["id"])
        if destination.get("review_catalog"):
            name = destination["review_catalog"]["path"]
            catalog = json.loads((source / name).read_bytes())
            catalog.update(documents=topic["documents"], media=topic["media"])
            files[name] = json_bytes(catalog)
            destination["review_catalog"]["sha256"] = sha(files[name])
        else:
            destination.update(documents=topic["documents"], media=topic["media"])
        if (
            topic["documents"]
            != load_curation_policy().raw["topics"][raw["topics"].index(topic)]["documents"]
            or topic["media"]
            != load_curation_policy().raw["topics"][raw["topics"].index(topic)]["media"]
        ):
            major, minor, patch = destination["version"].split(".")
            destination["version"] = f"{major}.{minor}.{int(patch) + 1}"
    files["curation_policy.json"] = json_bytes(original)
    return files


def _materialized_policy(raw: dict[str, Any]) -> CurationPolicy:
    files = policy_files(raw)
    policy = json.loads(files["curation_policy.json"])
    linked = bytearray(files["curation_policy.json"])
    for topic in policy["topics"]:
        if catalog := topic.get("review_catalog"):
            data = files[catalog["path"]]
            reviewed = json.loads(data)
            topic.update(documents=reviewed["documents"], media=reviewed["media"])
            linked.extend(data)
    return CurationPolicy(policy, sha(bytes(linked)))


def reviewed_policy(output: Path, contexts: list[dict[str, Any]]) -> CurationPolicy:
    if not contexts:
        raise ValueError("curation policy promotion requires independently reviewed inputs")
    current = load_curation_policy()
    raw = copy.deepcopy(current.raw)
    for context in contexts:
        if context["policy_sha256"] != current.sha256:
            raise ValueError("superseded curation renewal policy input")
        directory = output / context["binding_sha256"]
        visual = json.loads((directory / "visual.json").read_bytes())
        result = replay_renewal(context, visual, output)
        for stage in result["candidate"]["stages"]:
            topic = next(t for t in raw["topics"] if t["id"] == stage["topic"])
            decision = {
                **stage["document"],
                "reason": "independently_validated_current_source",
                "renewal_binding_sha256": context["binding_sha256"],
                "renewal_validation_sha256": sha(json_bytes(result)),
            }
            topic["documents"] = [
                old
                for old in topic["documents"]
                if canonical(old["url"]) != canonical(decision["url"])
            ]
            topic["documents"].append(decision)
            topic["documents"].sort(key=lambda item: item["url"])
            for choice in stage["media"]:
                # Replace this document's placement only, preserving other reviews.
                media = []
                for old in topic["media"]:
                    if old["sha256"] != choice["sha256"] or "documents" not in old:
                        media.append(old)
                        continue
                    remaining = [
                        url
                        for url in old["documents"]
                        if canonical(url) != canonical(context["identity"])
                    ]
                    if remaining:
                        media.append({**old, "documents": remaining})
                promoted = {**choice, "reason": "independently_validated_document_placement"}
                if "transformation" in choice:
                    promoted.update(
                        renewal_binding_sha256=context["binding_sha256"],
                        renewal_validation_sha256=sha(json_bytes(result)),
                    )
                    promoted["transformation"] = copy.deepcopy(choice["transformation"])
                    for mask in promoted["transformation"]["masks"]:
                        mask["reason"] = "independently_validated_retirement_region"
                    media.append(
                        {
                            "sha256": choice["output_sha256"],
                            "disposition": "keep",
                            "documents": choice["documents"],
                            "reason": "independently_validated_retirement_output",
                            "renewal_binding_sha256": context["binding_sha256"],
                            "renewal_validation_sha256": sha(json_bytes(result)),
                        }
                    )
                media.append(promoted)
                topic["media"] = media
    return _materialized_policy(raw)


def _record_dependencies(
    output: Path, unknown: list[str], validation_errors: dict[str, str]
) -> None:
    unresolved = sorted(set(unknown) | set(validation_errors))
    report = json_bytes(
        {
            "documents": unresolved,
            "reason": "independent_dependency_review_required",
            "validation_errors": validation_errors,
            "changed_dependencies": unknown,
        }
    )
    atomic_private(output / "dependency-history" / (sha(report) + ".json"), report)
    atomic_private(output / "dependency-exceptions.json", report)


# Qualification compares the complete baseline with all selected source inputs.
# pylint: disable-next=too-many-locals
def qualify_policy(
    output: Path, contexts: list[dict[str, Any]], baseline: Path, publication_sha256: str
) -> dict[str, Any]:
    """Unknown changed dependencies remain queued rather than implicitly approved."""
    policy = reviewed_policy(output, contexts)
    baseline_files = _verified_baseline(baseline, publication_sha256)
    with tempfile.TemporaryDirectory(prefix="private-catalog-qualification-") as temporary:
        root = Path(temporary)
        for relative, data in baseline_files.items():
            atomic_private(root / relative, data)
        approved = {}
        for context in contexts:
            directory = output / context["binding_sha256"]
            inputs = directory / "inputs"
            if inputs.is_symlink():
                raise ValueError("unsafe curation qualification source directory")
            captured = {
                path.relative_to(inputs).as_posix(): sha(path.read_bytes())
                for path in sorted(inputs.rglob("*"))
                if path.is_file()
            }
            if sha(json_bytes(captured)) != context["source_files_sha256"]:
                raise ValueError("curation qualification privacy-source bytes changed")
            for path in sorted(inputs.rglob("*")):
                if not path.is_file():
                    continue
                relative = path.relative_to(directory / "inputs")
                if relative.parts[:2] != ("articles", "content") or path.is_symlink():
                    raise ValueError("unsafe curation policy qualification input")
                destination = root / relative.relative_to("articles")
                atomic_private(destination, path.read_bytes())
                if destination.name == "index.md":
                    relative_article = destination.relative_to(root).as_posix()
                    if relative_article in approved:
                        raise ValueError("duplicate curation qualification article identity")
                    approved[relative_article] = context
        before = {
            path.relative_to(root).as_posix(): sha(path.read_bytes())
            for path in sorted((root / "content").rglob("index.md"))
        }
        first = migrate_content(root, curation_policy=policy)
        validation_errors = curation_validation_errors(root, policy)
        after = {
            path.relative_to(root).as_posix(): sha(path.read_bytes())
            for path in sorted((root / "content").rglob("index.md"))
        }
        unknown = sorted(
            path
            for path in before.keys() | after.keys()
            if before.get(path) != after.get(path) and path not in approved
        )
        unresolved = sorted(set(unknown) | set(validation_errors))
        if unresolved:
            _record_dependencies(output, unknown, validation_errors)
            raise DependencyReviewRequired(unresolved)
        validate_content_policy(root, curation_policy=policy)
        first_digest = corpus_digest(root)
        second = migrate_content(root, curation_policy=policy)
        if corpus_digest(root) != first_digest or second != first:
            raise ValueError("repeated proposed-policy curation is not deterministic")
        # The complete actual output remains private for post-planner validation.
        candidates = {}
        for relative in sorted(approved):
            path = root / relative
            context = approved[relative]
            expected = replay_renewal(
                context,
                json.loads((output / context["binding_sha256"] / "visual.json").read_bytes()),
                output,
            )["candidate"]
            if path.exists() != (expected["disposition"] == "keep"):
                raise ValueError("actual planner contradicts independently reviewed disposition")
            candidate: dict[str, Any] = (
                {"disposition": "remove"}
                if not path.exists()
                else {
                    "disposition": "keep",
                    "metadata": split_document(path.read_text())[0],
                    "body": split_document(path.read_text())[1],
                }
            )
            if path.exists():
                media = [
                    {"path": p.relative_to(root).as_posix(), "sha256": sha(p.read_bytes())}
                    for p in sorted(path.parent.glob("assets/*"))
                    if p.is_file()
                ]
                visual, fields, receipts = _media_context(
                    root, output / "planner-media", {"media": media}
                )
                atomic_private(
                    output / "planner-visual" / (context["binding_sha256"] + ".json"),
                    json_bytes(visual),
                )
                candidate.update(
                    media=receipts,
                    findings=scan(
                        {**text_fields(candidate["metadata"], candidate["body"]), **fields}
                    ),
                )
            else:
                candidate.update(body="", metadata=context["metadata"], media={}, findings=[])
            candidate.update(
                identity=context["identity"],
                binding_sha256=context["binding_sha256"],
                article=relative,
            )
            candidates[relative] = candidate
        result = {
            "policy_sha256": policy.sha256,
            "baseline_receipt_sha256": publication_sha256,
            "api_policy_sha256": load_content_policy().policy_digest,
            "corpus_sha256": first_digest,
            "candidates": candidates,
            "post_planner_validation_required": True,
        }
        result["qualification_sha256"] = sha(json_bytes(result))
        atomic_private(output / "planner-qualification.json", json_bytes(result))
        return result
