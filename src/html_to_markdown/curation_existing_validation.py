"""Select a completed proposal for current validation without another editor request."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

from .content_inventory import atomic_private
from .curation import json_bytes, load_curation_policy
from .curation_renewal import Proposal, compile_proposal, proposal_context
from .curation_renewal_media import candidate_visuals, source_media
from .curation_renewal_review import review_request
from .curation_section_validation import validate_sections
from .curation_source_proof import current_source_evidence
from .enrichment.analysis import sha
from .enrichment.contracts import EDITOR_MODEL, ResponseEvidence
from .enrichment.gates import parse_response, request_hash


def _write_once(path: Path, data: bytes) -> None:
    if path.is_symlink() or (path.exists() and path.read_bytes() != data):
        raise ValueError("selected curation evidence changed")
    if not path.exists():
        atomic_private(path, data)


def _semantic(candidate: dict[str, Any]) -> dict[str, Any]:
    """Only current binding envelopes differ; every actual transformation stays exact."""
    result = copy.deepcopy(candidate)
    for key in ("binding_sha256", "output_sha256", "proposal_sha256"):
        result.pop(key)
    result["proposal"].pop("binding_sha256")
    return result


def _original(output: Path, pin: str) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    if not re.fullmatch(r"[0-9a-f]{64}", pin):
        raise ValueError("unsafe original curation identity")
    directory = output / pin
    if directory.is_symlink() or any(p.is_symlink() for p in directory.rglob("*")):
        raise ValueError("unsafe original curation evidence")
    context = json.loads((directory / "context.json").read_bytes())
    state = json.loads((directory / "state.json").read_bytes())
    unsigned = {k: v for k, v in context.items() if k != "binding_sha256"}
    if sha(json_bytes(unsigned)) != pin or context["binding_sha256"] != pin:
        raise ValueError("original curation context digest changed")
    hashes = {
        p.relative_to(directory / "inputs").as_posix(): sha(p.read_bytes())
        for p in sorted((directory / "inputs").rglob("*"))
        if p.is_file()
    }
    if sha(json_bytes(hashes)) != context["source_files_sha256"]:
        raise ValueError("original curation input files changed")
    if context["policy_sha256"] != load_curation_policy().sha256:
        raise ValueError("original curation uses a stale policy")
    return directory, context, state


# Preserve complete old and current bindings alongside the single deterministic projection.
# pylint: disable-next=too-many-locals
def validate_existing_curation(
    output: Path, original_pin: str, attempt: int, client: Any = None
) -> dict[str, Any]:
    """A selected candidate grants validation only, even when the editor budget is exhausted."""
    original, context, state = _original(output, original_pin)
    if attempt < 0 or attempt > state["attempt"]:
        raise ValueError("invalid selected original editor attempt")
    previous = original / f"attempt-{attempt:03d}"
    editor = ResponseEvidence.model_validate_json((previous / "editor.json").read_bytes())
    decision = parse_response(editor, Proposal, EDITOR_MODEL, request_hash(editor.request))
    visual = json.loads((original / "visual.json").read_bytes())
    payload = json.loads(editor.request["input"][0]["content"][0]["text"])
    if (
        editor.request != review_request(context, visual, feedback=payload["validation_feedback"])
        or decision.binding_sha256 != original_pin
        or decision.unresolved
    ):
        raise ValueError("selected original proposal is incomplete or addresses different inputs")
    old_candidate = json.loads((previous / "candidate.json").read_bytes())
    if old_candidate["proposal"] != decision.model_dump(exclude_unset=True) or old_candidate[
        "output_sha256"
    ] != sha(json_bytes({k: v for k, v in old_candidate.items() if k != "output_sha256"})):
        raise ValueError("selected original candidate or proposal changed")
    origin = {
        "original_binding_sha256": original_pin,
        "original_attempt": attempt,
        "context_sha256": sha((original / "context.json").read_bytes()),
        "state_sha256": sha((original / "state.json").read_bytes()),
        "editor_request_sha256": editor.request_sha256,
        "candidate_sha256": old_candidate["output_sha256"],
        "selection_controller_sha256": sha(Path(__file__).read_bytes()),
        "new_editor_proposals": 0,
    }
    current = proposal_context(
        context["metadata"],
        context["body"],
        context["media"],
        context["source_files_sha256"],
        context["policy_sha256"],
        {**current_source_evidence(output, context), "selected_existing_proposal": origin},
    )
    directory = output / current["binding_sha256"]
    _write_once(directory / "context.json", json_bytes(current))
    _write_once(directory / "visual.json", (original / "visual.json").read_bytes())
    for path in sorted((original / "inputs").rglob("*")):
        if path.is_file():
            _write_once(
                directory / "inputs" / path.relative_to(original / "inputs"), path.read_bytes()
            )
    for name in ("editor.json", "candidate.json"):
        _write_once(directory / "original" / name, (previous / name).read_bytes())
    for name in ("context.json", "state.json"):
        _write_once(directory / "original" / name, (original / name).read_bytes())
    raw = (
        source_media(context, original)
        if any(m.masks for t in decision.topics for m in t.media)
        else {}
    )
    # This is a compiler input, never a changed or rebound model response.
    projected = {
        **decision.model_dump(exclude_unset=True),
        "binding_sha256": current["binding_sha256"],
    }
    candidate = compile_proposal(current, projected, raw)
    if _semantic(candidate) != _semantic(old_candidate):
        raise ValueError("current compiler changes the selected original transformation")
    body = review_request(
        current, visual + candidate_visuals(candidate, raw), candidate, state["feedback"]
    )
    sectioned = validate_sections(body, directory / "selected-section-validation", client)
    result = {
        "identity": current["identity"],
        "binding_sha256": current["binding_sha256"],
        "candidate": candidate,
        "preservation": sectioned["validation"],
        "independently_validated": True,
        "planner_validation_required": True,
        "editor_request_sha256": editor.request_sha256,
        "validator_request_sha256": request_hash(body),
        "section_validation": sectioned,
        "selected_existing_proposal": origin,
    }
    _write_once(directory / "validated.json", json_bytes(result))
    current_state = {
        **state,
        "status": "independently_validated",
        "retryable": False,
        "selected_existing_proposal": origin,
    }
    _write_once(directory / "state.json", json_bytes(current_state))
    return result
