"""Synchronize immutable inference execution without changing source review contexts."""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess  # nosec B404
from pathlib import Path
from typing import Any

from .enrichment.queued_client import executor_context


def contract() -> dict[str, Any]:
    """Native execution and model permission rules are the queued job's authority."""
    return {
        "schema_version": 1,
        "executor_sha256": executor_context(),
        "model_policy_sha256": hashlib.sha256(
            Path(__file__).with_name("enrichment").joinpath("model_policy.py").read_bytes()
        ).hexdigest(),
    }


def synchronize(transport: Path, host: str, executable: str) -> dict[str, Any]:
    if platform.system() != "Darwin":
        raise ValueError("inference executor synchronization runs on the MacBook")
    result = subprocess.run(
        [str(transport), "--host", host, "--", executable, "export-inference-executor"],
        capture_output=True,
        check=True,
        timeout=120,
    )  # nosec B603
    peer = json.loads(result.stdout)
    if (
        not isinstance(peer, dict)
        or not isinstance(peer.get("schema_version"), int)
        or isinstance(peer.get("schema_version"), bool)
        or peer != contract()
    ):
        raise ValueError("Mac and Ubuntu inference executor or model-policy pins differ")
    return peer
