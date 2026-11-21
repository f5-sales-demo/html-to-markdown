"""Carry original curation budgets across controller upgrades without granting approval."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from .curation import json_bytes
from .enrichment.analysis import sha


class ExistingRenewalRequired(ValueError):
    """An unchanged source has preserved attempts requiring current validation only."""


def _core(context: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(context)
    result.pop("binding_sha256")
    result.pop("controller_sha256")
    result["evidence"].pop("selected_existing_proposal", None)
    return result


def select_renewal_context(context: dict[str, Any], output: Path) -> dict[str, Any]:
    """Select an exact current validation, or preserve the previous editing allowance."""
    core = _core(context)
    historical_attempts = False
    selected: list[dict[str, Any]] = []
    for path in sorted(output.glob("*/context.json")):
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError("unsafe preserved curation context")
        observed = json.loads(path.read_bytes())
        if observed.get("identity") != context["identity"] or _core(observed) != core:
            continue
        unsigned = {k: v for k, v in observed.items() if k != "binding_sha256"}
        if (
            observed["binding_sha256"] != path.parent.name
            or sha(json_bytes(unsigned)) != path.parent.name
        ):
            raise ValueError("preserved curation context digest changed")
        if observed["binding_sha256"] == context["binding_sha256"]:
            continue
        state_path = path.parent / "state.json"
        origin = observed["evidence"].get("selected_existing_proposal")
        if (
            origin
            and observed["controller_sha256"] == context["controller_sha256"]
            and state_path.is_file()
        ):
            state = json.loads(state_path.read_bytes())
            if (
                state.get("status") == "independently_validated"
                and state.get("selected_existing_proposal") == origin
                and (path.parent / "validated.json").is_file()
            ):
                selected.append(observed)
        if state_path.exists() or any(path.parent.glob("attempt-*/editor.json")):
            historical_attempts = True
    if len(selected) > 1:
        raise ValueError(
            "multiple current selected curation validations require explicit resolution"
        )
    if selected:
        # Full original/request/fact/media replay remains mandatory in review_batch.
        return selected[0]
    if historical_attempts:
        raise ExistingRenewalRequired(
            "preserved curation attempts require current independent validation"
        )
    return context
