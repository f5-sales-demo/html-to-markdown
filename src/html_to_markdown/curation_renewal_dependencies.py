"""Independent baseline dependency reviews bound to a verified immutable receipt."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .baseline_source_context import baseline_context
from .content_inventory import atomic_private
from .curation import json_bytes, load_curation_policy, media_inventory
from .curation_renewal import proposal_context
from .curation_renewal_inputs import _topic_rules
from .curation_renewal_jobs import review_batch
from .curation_renewal_policy import DependencyReviewRequired, qualify_policy
from .enrichment.analysis import sha
from .source_review_jobs import _media_context


# Published input never claims a fresh capture; every dependency needs current privacy proof.
# Complete source, private privacy proof and renewal inputs are bound together.
# pylint: disable-next=too-many-locals
def prepare_dependencies(
    baseline: Path,
    publication_sha256: str,
    documents: list[str],
    output: Path,
    source_inputs: dict[str, tuple[Path, str]] | None = None,
) -> dict[str, Any]:
    from .baseline_source_review import (  # pylint: disable=import-outside-toplevel
        baseline_context,
        prepare_baseline_reviews,
        review_baseline_privacy,
    )

    privacy = output / "dependency-privacy"
    prepared = prepare_baseline_reviews(baseline, publication_sha256, documents, privacy)
    records = []
    for item in prepared["documents"]:
        record = privacy / item["path"]
        saved = json.loads(record.read_bytes())
        entry = {
            "identity": item["identity"],
            "source_privacy_path": record.relative_to(output).as_posix(),
            "reason": "baseline_source_privacy_review_required",
        }
        if source_inputs:
            # pylint: disable-next=import-outside-toplevel
            from .admitted_baseline_sources import (
                prepare_replacement,  # pylint: disable=import-outside-toplevel
            )

            replacement = prepare_replacement(
                baseline,
                publication_sha256,
                saved["binding"]["baseline_article"],
                output,
                source_inputs,
            )
            if replacement is not None:
                pin = replacement["binding_sha256"]
                entry.update(
                    binding_sha256=pin,
                    context_path=pin + "/context.json",
                    visual_path=pin + "/visual.json",
                    reason="independently_admitted_current_source_dependency",
                )
                records.append(entry)
                continue
        if saved["status"] == "source_exclusion_validated":
            entry.update(
                _prepare_source_exclusion(output, privacy, record, saved, publication_sha256)
            )
            records.append(entry)
            continue
        if saved["status"] != "privacy_only_validated":
            if saved["status"] == "source_review_required":
                disposition = saved["source_disposition"]
                entry.update(
                    reason="baseline_source_extraction_review_required"
                    if disposition["extraction"] != "complete"
                    else "baseline_source_relevance_review_required",
                    source_disposition={
                        key: disposition[key] for key in ("disposition", "extraction")
                    },
                )
            records.append(entry)
            continue
        saved = review_baseline_privacy(privacy, record)
        original, original_visual = baseline_context(privacy, privacy, saved)
        candidate = saved["candidate"]
        directory = privacy / "reviewed" / record.stem
        inputs = {}
        for name, digest in candidate["file_hashes"].items():
            path = directory / name
            if path.is_symlink() or sha(path.read_bytes()) != digest:
                raise ValueError("baseline privacy output bytes changed")
            inputs[name] = path.read_bytes()
        if (
            sha(json_bytes({name: sha(data) for name, data in sorted(inputs.items())}))
            != candidate["output_sha256"]
        ):
            raise ValueError("baseline privacy output inventory changed")
        article = saved["binding"]["baseline_article"]
        media = media_inventory(directory / "articles" / article, candidate["body"])
        visual, _, _ = _media_context(
            directory,
            output / "dependency-media",
            {
                "media": [
                    {
                        "path": (
                            Path("articles") / Path(article).parent / m["reference"]
                        ).as_posix(),
                        "sha256": m["sha256"],
                    }
                    for m in media
                ]
            },
        )
        evidence = {
            "input_kind": "retained_verified_baseline",
            "baseline_receipt_sha256": publication_sha256,
            "baseline_article": article,
            "baseline_article_sha256": saved["binding"]["baseline_article_sha256"],
            "source_limitation": original["source_limitation"],
            "original": {key: original[key] for key in ("metadata", "body", "media", "findings")},
            "privacy_candidate": candidate,
            "privacy_decision": saved["privacy_decision"],
            "baseline_source_privacy_record_sha256": sha(record.read_bytes()),
            "source_privacy_prerequisite": {
                "binding_sha256": saved["binding_sha256"],
                "enforcement_findings": 0,
                "publication_approved": False,
            },
            "policy_topics": _topic_rules(original, candidate),
        }
        context = proposal_context(
            candidate["metadata"],
            candidate["body"],
            candidate["media"],
            candidate["output_sha256"],
            load_curation_policy().sha256,
            evidence,
        )
        destination = output / context["binding_sha256"]
        for name, data in inputs.items():
            atomic_private(destination / "inputs" / name, data)
        atomic_private(destination / "context.json", json_bytes(context))
        atomic_private(destination / "visual.json", json_bytes(original_visual + visual))
        entry.update(
            binding_sha256=context["binding_sha256"],
            context_path=context["binding_sha256"] + "/context.json",
            visual_path=context["binding_sha256"] + "/visual.json",
            reason="independent_baseline_dependency_review_required",
        )
        records.append(entry)
    inventory = {
        "schema_version": 1,
        "baseline_receipt_sha256": publication_sha256,
        "documents": records,
        "source_privacy_required": True,
    }
    atomic_private(output / "dependency-inventory.json", json_bytes(inventory))
    return inventory


class DependencyReviewPending(ValueError):
    """Complete privacy prerequisites remain queued after a bounded cohort."""

    def __init__(self, remaining: int, reviewed: int) -> None:
        super().__init__("baseline source privacy dependencies remain pending")
        self.remaining = remaining
        self.reviewed = reviewed


def review_dependencies(
    output: Path,
    contexts: list[dict[str, Any]],
    baseline: Path,
    publication_sha256: str,
    journal: Path,
    source_inputs: dict[str, tuple[Path, str]] | None = None,
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Close complete planner dependencies; any unresolved review stops promotion."""
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
        raise ValueError("dependency review limit must be a positive integer")
    remaining = 0 if limit is None else limit
    selected = list(contexts)
    for _ in range(3):
        try:
            qualification = qualify_policy(output, selected, baseline, publication_sha256)
            atomic_private(
                output / "dependency-resolution.json",
                json_bytes(
                    {
                        "state": "closed",
                        "baseline_receipt_sha256": publication_sha256,
                        "qualification_sha256": qualification["qualification_sha256"],
                        "contexts": [c["binding_sha256"] for c in selected],
                        "post_planner_validation_required": True,
                    }
                ),
            )
            return selected
        except DependencyReviewRequired as failure:
            already = {
                path.relative_to(output / c["binding_sha256"] / "inputs" / "articles").as_posix()
                for c in selected
                for path in (output / c["binding_sha256"] / "inputs" / "articles").rglob("index.md")
            }
            if already & set(failure.documents):
                raise ValueError(
                    "independently reviewed dependency still fails actual planner"
                ) from failure
            # pylint: disable-next=import-outside-toplevel
            from .baseline_source_review import (
                review_baseline_batch,
            )

            inventory = prepare_dependencies(
                baseline,
                publication_sha256,
                failure.documents,
                output,
                **({"source_inputs": source_inputs} if source_inputs else {}),
            )
            if any("context_path" not in item for item in inventory["documents"]):
                privacy = output / "dependency-privacy"
                inventory = json.loads((privacy / "inventory.json").read_bytes())
                quantum: dict[str, Any] = {}
                if limit is None:
                    review_baseline_batch(privacy, inventory, journal)
                else:
                    # pylint: disable-next=import-outside-toplevel
                    from .baseline_review_schedule import review_quantum

                    if remaining:
                        quantum = review_quantum(privacy, inventory, journal, remaining)
                        remaining -= quantum["reviewed"]
                    else:
                        quantum = {
                            "reviewed": 0,
                            "deferred": len(inventory["documents"]),
                            "publication_approved": False,
                        }
                inventory = prepare_dependencies(
                    baseline,
                    publication_sha256,
                    failure.documents,
                    output,
                    **({"source_inputs": source_inputs} if source_inputs else {}),
                )
                missing = sum("context_path" not in item for item in inventory["documents"])
                if limit is not None and missing:
                    atomic_private(
                        output / "dependency-resolution.json",
                        json_bytes(
                            {
                                "state": "pending",
                                "baseline_receipt_sha256": publication_sha256,
                                "pending": missing,
                                "quantum": quantum,
                                "publication_approved": False,
                            }
                        ),
                    )
                    raise DependencyReviewPending(missing, limit - remaining) from failure
            report = review_batch(output, inventory, "all", journal)
            approved = {
                d["identity"]
                for d in report.get("documents", [])
                if d["disposition"] == "independently_validated"
            }
            selected.extend(
                json.loads((output / item["context_path"]).read_bytes())
                for item in inventory["documents"]
                if item["identity"] in approved
            )
            atomic_private(
                output / "dependency-selection.json",
                json_bytes(
                    {
                        "baseline_receipt_sha256": publication_sha256,
                        "contexts": [c["binding_sha256"] for c in selected],
                        "exceptions": report["exceptions"],
                    }
                ),
            )
            if report["exceptions"]:
                raise ValueError("independent baseline dependencies remain queued") from failure
    raise ValueError("whole-corpus dependency reconciliation remains incomplete")


def _prepare_source_exclusion(
    output: Path, privacy: Path, record: Path, saved: dict[str, Any], publication_sha256: str
) -> dict[str, Any]:
    # pylint: disable-next=import-outside-toplevel
    from .baseline_dispositions import (
        replay_exclusion,  # pylint: disable=import-outside-toplevel
    )

    original, visual = baseline_context(privacy, privacy, saved)
    evidence = {
        "input_kind": "retained_verified_baseline",
        "baseline_receipt_sha256": publication_sha256,
        "baseline_article": saved["binding"]["baseline_article"],
        "baseline_source_disposition_record_sha256": sha(record.read_bytes()),
        "source_disposition_kind": "source_relevance_exclusion",
    }
    context = proposal_context(
        original["metadata"],
        original["body"],
        original["media"],
        saved["binding"]["source_files_sha256"],
        load_curation_policy().sha256,
        evidence,
    )
    destination = output / context["binding_sha256"]
    for name, digest in saved["input_file_hashes"].items():
        source = privacy / saved["source"] / name
        if source.is_symlink() or sha(source.read_bytes()) != digest:
            raise ValueError("retained exclusion original bytes changed")
        atomic_private(destination / "inputs" / name, source.read_bytes())
    atomic_private(destination / "context.json", json_bytes(context))
    atomic_private(destination / "visual.json", json_bytes(visual))
    replay_exclusion(context, output)
    return {
        "binding_sha256": context["binding_sha256"],
        "context_path": context["binding_sha256"] + "/context.json",
        "visual_path": context["binding_sha256"] + "/visual.json",
        "reason": "independently_validated_source_exclusion",
    }
