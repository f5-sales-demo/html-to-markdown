"""Complete immutable retained-source context shared by privacy and relevance reviews."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .baseline_late_validation import existing_validation_context
from .baseline_review_renewal import preparation_controller as _controller
from .curation import blocks, json_bytes
from .enrichment.analysis import sha
from .render import split_document
from .source_review import review_context_digest, scan, text_fields
from .source_review_jobs import _media_context


def _relevance_evidence(
    output: Path, article: str, source_sha: str, receipt: str
) -> dict[str, Any]:
    """Bind prior claims as untrusted context; they never confer a relevance approval."""
    path = output / "relevance-evidence" / (sha(article) + ".json")
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("unsafe baseline relevance evidence")
    if not path.exists():
        return {}
    evidence: dict[str, Any] = json.loads(path.read_bytes())
    if not isinstance(evidence, dict):
        raise ValueError("baseline relevance evidence must be an object")
    if (
        evidence.get("baseline_article") != article
        or evidence.get("baseline_article_sha256") != source_sha
        or evidence.get("baseline_receipt_sha256") != receipt
    ):
        raise ValueError("baseline relevance evidence addresses a different source")
    return evidence


def baseline_context(
    _capture: Path, output: Path, saved: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Replay original bytes and native frames before constructing any model request."""
    if saved.get("selected_existing_validation"):
        return existing_validation_context(output, saved)
    if saved.get("existing_proposal_pending"):
        raise ValueError("current historical-proposal validation remains required")
    binding = saved["binding"]
    relative = Path(saved["source"])
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("unsafe baseline privacy source path")
    source = output / relative
    files = {
        p.relative_to(source).as_posix(): sha(p.read_bytes())
        for p in sorted(source.rglob("*"))
        if p.is_file()
    }
    if not all(
        (
            not source.is_symlink(),
            not any(p.is_symlink() for p in source.rglob("*")),
            files == saved["input_file_hashes"],
            sha(json_bytes(files)) == binding["source_files_sha256"],
            binding["preparation_controller_sha256"] == _controller(),
            binding["source_privacy_controller_sha256"] == review_context_digest(),
            sha(json_bytes(binding)) == saved["binding_sha256"],
        )
    ):
        raise ValueError("stale or unsafe baseline source privacy inputs")
    metadata, body = split_document((source / saved["item"]["article_path"]).read_text())
    visual, fields, media = _media_context(source, output, saved["item"])
    fields = {**text_fields(metadata, body), **fields}
    context = {
        "identity": saved["identity"],
        "binding": binding,
        "binding_sha256": saved["binding_sha256"],
        "metadata": metadata,
        "body": body,
        "source_article_html": "",
        "source_limitation": "Complete previously verified published Markdown and every associated media frame are supplied. Original HTML is unavailable. This is retained-source review, never a fresh capture or proof of current source availability.",
        "fields": fields,
        "findings": scan(fields),
        "media": media,
        "structural_addresses": [
            {"address": f"b:{i}", "source_address": b.location, "sha256": b.sha256, "text": b.text}
            for i, b in enumerate(blocks(body))
        ],
    }
    prior_relevance = _relevance_evidence(
        output,
        binding["baseline_article"],
        binding["baseline_article_sha256"],
        binding["baseline_receipt_sha256"],
    )
    if binding.get("relevance_evidence_sha256") != (
        sha(json_bytes(prior_relevance)) if prior_relevance else None
    ):
        raise ValueError("baseline relevance evidence changed after preparation")
    if prior_relevance:
        context["prior_relevance_evidence"] = prior_relevance
        context["relevance_reconciliation_scope"] = (
            "Prior inclusion, exclusion and necessary-fact classifications are untrusted historical "
            "claims. Independently reconcile them against the complete supplied article. Do not "
            "discard useful navigation or reference facts merely because the article is short; "
            "do not retain an irrelevant shell merely because it was previously published. "
            "Explain the disposition using current source evidence. Later preservation guards "
            "remain mandatory and this context cannot waive them."
        )
    return context, visual
