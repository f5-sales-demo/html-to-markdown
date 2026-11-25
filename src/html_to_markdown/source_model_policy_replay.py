"""Exact historical source request reconstruction, without evidence mutation."""

from copy import deepcopy
from typing import Any

from .enrichment.model_policy import compatible_request


def replay_request(expected: dict[str, Any], recorded: dict[str, Any]) -> dict[str, Any]:
    """Require full equality after the registered model-policy reconstruction."""
    reconstructed = (
        deepcopy(expected) if expected == recorded else compatible_request(expected, recorded)
    )
    if reconstructed != recorded:
        raise ValueError("stale complete source review request or changed model-policy context")
    return reconstructed
