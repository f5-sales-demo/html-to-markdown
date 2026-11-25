"""Feed private actual-planner prerequisites back into bounded source scheduling."""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import subprocess  # nosec B404
from pathlib import Path
from typing import Any

from .content_inventory import atomic_private
from .curation import json_bytes
from .enrichment.analysis import sha
from .reference_repair_inputs import safe_file
from .review_priorities import FILENAME, load_priorities, parse_priorities

_DIGEST = re.compile(r"[0-9a-f]{64}")
_DOCUMENT = re.compile(
    r"content/(docs-cloud-f5-com|community-f5-com|my-f5-com|www-f5-com)/(.+)/index\.md"
)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate planner dependency key")
        result[key] = value
    return result


def dependency_identities(payload: bytes) -> list[str]:
    """Use only supported source-relative paths, never diagnostic text or model output."""
    if len(payload) > 1024 * 1024:
        raise ValueError("planner dependency report exceeds size bound")
    try:
        report = json.loads(payload, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as failure:
        raise ValueError("invalid planner dependency report") from failure
    if not isinstance(report, dict) or not isinstance(report.get("documents"), list):
        raise ValueError("planner dependency report requires document paths")
    identities = []
    for document in report["documents"]:
        match = _DOCUMENT.fullmatch(document) if isinstance(document, str) else None
        if match is None or document != Path(document).as_posix():
            raise ValueError("invalid planner prerequisite document path")
        identities.append(match[1] + ":" + match[2])
    values = sorted(set(identities))
    parse_priorities(json_bytes({"schema_version": 1, "identities": values, "urls": []}))
    return values


def register_priorities(queue: Path, identities: list[str], report_sha256: str) -> dict[str, Any]:
    """Atomically merge hints on the queue owner; approvals and model guards stay intact."""
    if not _DIGEST.fullmatch(report_sha256):
        raise ValueError("invalid planner dependency report digest")
    incoming = parse_priorities(
        json_bytes({"schema_version": 1, "identities": identities, "urls": []})
    )
    if queue.is_symlink() or not queue.is_dir():
        raise ValueError("unsafe planner priority queue root")
    descriptor = os.open(
        queue / ".review-priorities.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "a+b") as lock:
        if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
            raise ValueError("unsafe planner priority lock")
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = load_priorities(queue / FILENAME)
        payload = json_bytes(
            {
                "schema_version": 1,
                "identities": sorted(current.identities | incoming.identities),
                "urls": sorted(current.urls),
            }
        )
        final = parse_priorities(payload)
        added = len(incoming.identities - current.identities)
        changed = current.sha256 is None or added > 0
        digest = sha(payload) if changed else current.sha256
        receipt = {
            "schema_version": 1,
            "dependency_report_sha256": report_sha256,
            "before_sha256": current.sha256,
            "after_sha256": digest,
            "added": added,
            "identities": len(final.identities),
            "urls": len(final.urls),
            "publication_approved": False,
        }
        evidence = queue / ".review-priority-updates"
        if evidence.is_symlink():
            raise ValueError("unsafe planner priority evidence directory")
        evidence.mkdir(mode=0o700, exist_ok=True)
        receipt_bytes = json_bytes(receipt)
        # Preserve both generations before advancing the scheduling pointer.
        original = payload
        if current.sha256 is not None:
            original = safe_file(queue, FILENAME).read_bytes()
            if sha(original) != current.sha256:
                raise ValueError("planner priority input changed during registration")
            _preserve(evidence / (current.sha256 + ".input.json"), original)
        _preserve(evidence / (str(digest) + ".input.json"), payload if changed else original)
        if changed:
            atomic_private(queue / FILENAME, payload)
        load_priorities(queue / FILENAME, expected_sha256=str(digest))
        _preserve(evidence / (sha(receipt_bytes) + ".receipt.json"), receipt_bytes)
        return receipt


def _preserve(path: Path, payload: bytes) -> None:
    """Keep immutable generations and avoid repeat disk writes for identical hints."""
    if path.exists() or path.is_symlink():
        if safe_file(path.parent, path.name).read_bytes() != payload:
            raise ValueError("planner priority evidence changed")
    else:
        atomic_private(path, payload)


def prioritize_dependencies(
    output: Path,
    transport: Path,
    host: str,
    remote_executable: str,
    remote_queue: str,
) -> dict[str, Any] | None:
    """Return a safe registration receipt after pending or failed curation reviews."""
    report = output / "dependency-exceptions.json"
    if not report.exists() and not report.is_symlink():
        return None
    checked = safe_file(output, report.name)
    if checked.stat().st_size > 1024 * 1024:
        raise ValueError("planner dependency report exceeds size bound")
    payload = checked.read_bytes()
    identities = dependency_identities(payload)
    if not identities:
        return None
    args = [
        str(transport),
        "--host",
        host,
        "--",
        remote_executable,
        "register-review-priorities",
        "--queue-root",
        remote_queue,
        "--dependency-report-sha256",
        sha(payload),
    ]
    for identity in identities:
        args.extend(["--identity", identity])
    result = subprocess.run(args, capture_output=True, check=True, timeout=60)  # nosec B603
    receipt = json.loads(result.stdout)
    if (
        not isinstance(receipt, dict)
        or receipt.get("dependency_report_sha256") != sha(payload)
        or receipt.get("publication_approved") is not False
    ):
        raise ValueError("planner priority registration receipt mismatch")
    atomic_private(output / "priority-registration.private.json", json_bytes(receipt))
    return receipt
