"""Shared private model evidence transport without source or curation orchestration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .content_inventory import atomic_private
from .curation import json_bytes
from .enrichment.analysis import sha
from .enrichment.contracts import ResponseEvidence
from .enrichment.gates import request_hash
from .errors import TransportFailure
from .media_observation_jobs import RequestExecutor


def save(path: Path, value: dict[str, Any]) -> None:
    data = json_bytes(value)
    if path.exists() and path.read_bytes() != data:
        previous = path.read_bytes()
        atomic_private(path.parent / "history" / (sha(previous) + ".json"), previous)
    atomic_private(path, data)


def execute(path: Path, body: dict[str, Any], client: RequestExecutor) -> ResponseEvidence:
    if path.exists():
        saved = ResponseEvidence.model_validate_json(path.read_bytes())
        if not saved.error and saved.request_sha256 == request_hash(body):
            return saved
    evidence = client.execute(body)
    save(path, evidence.model_dump())
    if evidence.error:
        raise TransportFailure("curation renewal transport failed; private evidence preserved")
    return evidence
