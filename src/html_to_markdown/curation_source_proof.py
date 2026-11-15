"""Compose a renewed source proof only over the identical reviewed intermediate."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

from .curation import json_bytes
from .enrichment.analysis import sha
from .enrichment.contracts import ResponseEvidence


def current_source_evidence(output: Path, context: dict[str, Any]) -> dict[str, Any]:
    """Source approval is replayed independently; curation cannot update its expected bytes."""
    # Source review imports curation transport; keep the dependency at execution time.
    # pylint: disable-next=import-outside-toplevel
    from .baseline_source_review import verify_baseline_prerequisite

    evidence: dict[str, Any] = copy.deepcopy(context["evidence"])
    original_pin = evidence.get("baseline_source_privacy_record_sha256")
    if original_pin is None:
        return evidence
    if not re.fullmatch(r"[0-9a-f]{64}", str(original_pin)):
        raise ValueError("invalid original source prerequisite pin")
    article = Path(evidence["baseline_article"])
    if (
        article.is_absolute()
        or ".." in article.parts
        or article.parts[:1] != ("content",)
        or article.name != "index.md"
        or evidence.get("input_kind") != "retained_verified_baseline"
    ):
        raise ValueError("unsafe renewed source prerequisite identity")
    privacy = output / "dependency-privacy"
    record = privacy / "documents" / (sha(article.as_posix()) + ".json")
    if privacy.is_symlink() or record.parent.is_symlink() or record.is_symlink():
        raise ValueError("unsafe renewed source prerequisite evidence")
    current_pin = sha(record.read_bytes())
    if current_pin == original_pin:
        verify_baseline_prerequisite(output, context)
        return evidence
    history = privacy / "history" / (original_pin + ".json")
    if history.is_symlink() or history.parent.is_symlink() or not history.is_file():
        raise ValueError("original source prerequisite proof is unavailable")
    if sha(history.read_bytes()) != original_pin:
        raise ValueError("original source prerequisite proof changed")
    before = json.loads(history.read_bytes())
    current = json.loads(record.read_bytes())
    if selected := current.get("selected_existing_validation"):
        request_pin = selected["request_sha256"]
        if not re.fullmatch(r"[0-9a-f]{64}", str(request_pin)):
            raise ValueError("invalid current source validation request pin")
        cached = privacy / "existing-validation" / record.stem / (request_pin + ".json")
        if cached.is_symlink() or cached.parent.is_symlink() or not cached.is_file():
            raise ValueError("current source validation evidence is unavailable")
        captured = ResponseEvidence.model_validate_json(cached.read_bytes())
        if captured.error or captured.model_dump(mode="json") != current.get(
            "privacy_validation_evidence"
        ):
            raise ValueError("current source validation record changed or failed")
    if (
        before["binding"]["baseline_article"] != article.as_posix()
        or before["binding"]["baseline_receipt_sha256"] != evidence["baseline_receipt_sha256"]
        or current["binding"]["baseline_article"] != article.as_posix()
        or current["binding"]["baseline_receipt_sha256"] != evidence["baseline_receipt_sha256"]
    ):
        raise ValueError("renewed source prerequisite addresses a different baseline")
    evidence["baseline_source_privacy_record_sha256"] = current_pin
    # This replays current source/privacy/credential enforcement and compares the entire
    # metadata, body, media and file inventory to the unchanged original curation input.
    verify_baseline_prerequisite(output, {**context, "evidence": evidence})
    evidence["source_proof_transition"] = {
        "version": "curation-source-proof/1",
        "original_record_sha256": original_pin,
        "current_record_sha256": current_pin,
        "baseline_receipt_sha256": evidence["baseline_receipt_sha256"],
        "source_files_sha256": context["source_files_sha256"],
        "controller_sha256": sha(Path(__file__).read_bytes()),
        "intermediate_unchanged": True,
    }
    # Include this complete transition in the subsequent independent request binding.
    evidence["source_proof_transition_sha256"] = sha(
        json_bytes(evidence["source_proof_transition"])
    )
    return evidence
