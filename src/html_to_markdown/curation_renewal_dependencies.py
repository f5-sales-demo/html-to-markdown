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
from .refresh import _verified_baseline
from .render import split_document
from .source_review import scan, text_fields
from .source_review_jobs import _media_context


# Published input is retained verified content; this never claims a fresh capture.
# pylint: disable-next=too-many-locals
def prepare_dependencies(
    baseline: Path, publication_sha256: str, documents: list[str], output: Path
) -> dict[str, Any]:
    files = _verified_baseline(baseline, publication_sha256)
    records = []
    for article in sorted(set(documents)):
        path = Path(article)
        if (
            path.is_absolute()
            or ".." in path.parts
            or path.parts[:1] != ("content",)
            or path.name != "index.md"
            or article not in files
        ):
            raise ValueError("dependency lacks a verified baseline article")
        inputs = {
            "articles/" + name: data
            for name, data in files.items()
            if name == article or Path(name).parent == path.parent / "assets"
        }
        file_hashes = {name: sha(data) for name, data in sorted(inputs.items())}
        source = output / "dependency-sources" / publication_sha256 / sha(article)
        for name, data in inputs.items():
            atomic_private(source / Path(name).relative_to("articles"), data)
        metadata, body = split_document(files[article].decode())
        media = media_inventory(source / article, body)
        if any(item["kind"] != "local" or not item["sha256"] for item in media):
            raise ValueError("baseline dependency has unavailable or external media")
        visual, fields, receipts = _media_context(
            source,
            output / "dependency-media",
            {
                "media": [
                    {"path": (path.parent / item["reference"]).as_posix(), "sha256": item["sha256"]}
                    for item in media
                ]
            },
        )
        original = {
            "metadata": metadata,
            "body": body,
            "media": receipts,
            "findings": scan({**text_fields(metadata, body), **fields}),
        }
        evidence = {
            "input_kind": "retained_verified_baseline",
            "baseline_receipt_sha256": publication_sha256,
            "baseline_article": article,
            "baseline_article_sha256": sha(files[article]),
            "source_limitation": "Original HTML is unavailable here; review the complete previously verified published article. This is not a fresh source admission.",
            "original": original,
            "privacy_candidate": original,
            "policy_topics": _topic_rules(original, original),
        }
        context = proposal_context(
            metadata,
            body,
            receipts,
            sha(json_bytes(file_hashes)),
            load_curation_policy().sha256,
            evidence,
        )
        directory = output / context["binding_sha256"]
        for name, data in inputs.items():
            atomic_private(directory / "inputs" / name, data)
        atomic_private(directory / "context.json", json_bytes(context))
        atomic_private(directory / "visual.json", json_bytes(visual))
        records.append(
            {
                "identity": context["identity"],
                "binding_sha256": context["binding_sha256"],
                "context_path": context["binding_sha256"] + "/context.json",
                "visual_path": context["binding_sha256"] + "/visual.json",
                "reason": "independent_baseline_dependency_review_required",
            }
        )
    inventory = {
        "schema_version": 1,
        "baseline_receipt_sha256": publication_sha256,
        "documents": records,
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
