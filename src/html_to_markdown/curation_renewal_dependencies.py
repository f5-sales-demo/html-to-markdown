"""Independent baseline dependency reviews bound to a verified immutable receipt."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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
    baseline: Path, publication_sha256: str, documents: list[str], output: Path
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
        if saved["status"] != "privacy_only_validated":
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


def review_dependencies(
    output: Path,
    contexts: list[dict[str, Any]],
    baseline: Path,
    publication_sha256: str,
    journal: Path,
) -> list[dict[str, Any]]:
    """Close complete planner dependencies; any unresolved review stops promotion."""
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
                baseline, publication_sha256, failure.documents, output
            )
            if any("context_path" not in item for item in inventory["documents"]):
                privacy = output / "dependency-privacy"
                privacy_inventory = json.loads((privacy / "inventory.json").read_bytes())
                privacy_report = review_baseline_batch(privacy, privacy_inventory, journal)
                if privacy_report["exceptions"]:
                    raise ValueError(
                        "baseline source privacy prerequisites remain queued"
                    ) from failure
                inventory = prepare_dependencies(
                    baseline, publication_sha256, failure.documents, output
                )
            report = review_batch(output, inventory, "all", journal)
            if report["exceptions"]:
                raise ValueError("independent baseline dependencies remain queued") from failure
            selected.extend(
                json.loads((output / item["context_path"]).read_bytes())
                for item in inventory["documents"]
            )
    raise ValueError("whole-corpus dependency reconciliation remains incomplete")
