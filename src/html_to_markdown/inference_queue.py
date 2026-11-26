"""Private Ubuntu inference jobs with byte-verified Mac evidence acknowledgments."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO

from .content_inventory import atomic_private
from .curation import json_bytes
from .enrichment.codex_client import CodexClient
from .enrichment.contracts import ResponseEvidence
from .enrichment.gates import request_hash
from .enrichment.model_policy import validate_new_request
from .models import utc_now
from .work_queue import unpack_queue

MAX_TRANSFER_BYTES = 4 * 1024**3


def _hex(value: str, length: int) -> None:
    if not re.fullmatch("[0-9a-f]{" + str(length) + "}", value):
        raise ValueError("Invalid private inference identity")


def _job(root: Path, identity: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{64}/[0-9]{6}", identity):
        raise ValueError("Unsafe private inference job identity")
    if root.is_symlink() or any(
        (root / p).is_symlink() for p in [identity, identity.split("/")[0]]
    ):
        raise ValueError("Private inference queue cannot follow symlinks")
    return root / identity


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError("Invalid private inference record")
    return value


def _write(path: Path, value: Any) -> None:
    atomic_private(path, json_bytes(value))


@contextmanager
def _lock(root: Path) -> Iterator[None]:
    if root.is_symlink():
        raise ValueError("Private inference queue cannot follow symlinks")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / "queue.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def submit(root: Path, request: dict[str, Any], executor_sha256: str) -> str:
    """Resume an exact pending job; failed or invalidated results get a new attempt."""
    _hex(executor_sha256, 64)
    validate_new_request(request)
    digest = request_hash(request)
    with _lock(root):
        parent = root / digest
        prior = sorted(p for p in parent.glob("[0-9]" * 6) if p.is_dir())
        if prior:
            latest = prior[-1]
            state = _read(latest / "state.json")
            if _read(latest / "request.json") != request:
                raise ValueError("Persisted inference request changed")
            if (
                state["executor_sha256"] == executor_sha256
                and not (latest / "invalidated.json").exists()
            ):
                if state["state"] in {"queued", "claimed"} or (
                    state["state"] == "received" and not (latest / "receipt.json").exists()
                ):
                    return latest.relative_to(root).as_posix()
                evidence = result(root, latest.relative_to(root).as_posix())
                if evidence is not None and evidence.error is None:
                    return latest.relative_to(root).as_posix()
        number = int(prior[-1].name) + 1 if prior else 1
        if number > 999999:
            raise ValueError("Private inference attempt counter exhausted")
        identity = digest + "/" + f"{number:06d}"
        job = _job(root, identity)
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        staging = parent / (".submission-" + uuid.uuid4().hex)
        staging.mkdir(mode=0o700)
        _write(staging / "request.json", request)
        _write(
            staging / "state.json",
            {
                "job": identity,
                "state": "queued",
                "request_sha256": digest,
                "executor_sha256": executor_sha256,
                "queued_at": utc_now(),
            },
        )
        staging.rename(job)
        return identity


def claim(
    root: Path,
    owner: str,
    executor_sha256: str,
    limit: int = 4,
    *,
    new_limit: int | None = None,
) -> list[dict[str, Any]]:
    """A durable owner resumes its existing claims; sleep never expires a live job."""
    _hex(owner, 32)
    _hex(executor_sha256, 64)
    if not 1 <= limit <= 4:
        raise ValueError("At most four inference jobs may be claimed")
    if new_limit is not None and (
        not isinstance(new_limit, int) or isinstance(new_limit, bool) or not 0 <= new_limit <= 4
    ):
        raise ValueError("New inference claims require between zero and four available slots")
    with _lock(root):
        records = [(p.parent, _read(p)) for p in sorted(root.glob("*/*/state.json"))]
        active = [
            (p, s)
            for p, s in records
            if s["state"] == "claimed"
            or (s["state"] == "received" and not (p / "receipt.json").exists())
        ]
        owned = [(p, s) for p, s in active if s["owner"] == owner]
        queued = [(p, s) for p, s in records if s["state"] == "queued"]
        if len(active) >= 4 and queued and not owned:
            raise ValueError("Another Mac worker holds all inference slots")
        available = max(0, min(limit - len(owned), 4 - len(active)))
        if new_limit is not None:
            # Receipts can commit before a Mac future observes its acknowledgment.
            # Its declared free local slots also bound newly claimed jobs.
            available = min(available, new_limit)
        selected = owned[:limit] + queued[:available]
        results = []
        for job, state in selected:
            if state["executor_sha256"] != executor_sha256:
                raise ValueError("Mac executor differs from queued inference context")
            request = _read(job / "request.json")
            if request_hash(request) != state["request_sha256"]:
                raise ValueError("Queued inference request digest changed")
            validate_new_request(request)
            state.update(state="claimed", owner=owner)
            state.setdefault("claimed_at", utc_now())
            _write(job / "state.json", state)
            results.append(
                {
                    "job": state["job"],
                    "owner": owner,
                    "executor_sha256": executor_sha256,
                    "request": request,
                }
            )
        return results


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        while data := stream.read(1024 * 1024):
            h.update(data)
    return h.hexdigest()


def _validated(
    job: Path, payload: Path, state: dict[str, Any]
) -> tuple[ResponseEvidence, dict[str, str]]:
    pins = _read(payload / "manifest.json")
    names = {p.relative_to(payload).as_posix() for p in payload.rglob("*") if p.is_file()}
    if names != set(pins) | {"manifest.json"}:
        raise ValueError("Returned evidence manifest coverage changed")
    for name, digest in pins.items():
        _hex(digest, 64)
        if _digest(payload / name) != digest:
            raise ValueError("Returned inference evidence bytes changed")
    envelope = _read(payload / "envelope.json")
    if any(envelope[k] != state[k] for k in ["job", "owner", "executor_sha256"]):
        raise ValueError("Returned inference job owner or executor changed")
    evidence = ResponseEvidence.model_validate(envelope["evidence"])
    request = _read(job / "request.json")
    if evidence.request != request or evidence.request_sha256 != request_hash(request):
        raise ValueError("Returned inference source request changed")
    if (
        evidence.reasoning != "high"
        or evidence.transport != "codex_litellm"
        or evidence.model != request["model"]
    ):
        raise ValueError("Returned inference transport, model or reasoning changed")
    if evidence.source_boundary is not None:
        raise ValueError(
            "Broker accepts actual leaf responses; source aggregation belongs on Ubuntu"
        )
    request_file = payload / "journal" / (evidence.request_sha256 + ".request.json")
    if _read(request_file) != request:
        raise ValueError("Returned original request checkpoint changed")
    attempts = [_read(p) for p in (payload / "journal").glob("*.attempt-*.json")]
    if not any(attempt.get("evidence") == evidence.model_dump() for attempt in attempts):
        raise ValueError("Returned inference lacks its original raw attempt")
    if evidence.error is None:
        CodexClient.validate_transport_response(evidence, request)
        events = evidence.response.get("events", [])
        if len([e for e in events if e.get("type") == "turn.completed"]) != 1:
            raise ValueError("Returned inference lacks exactly one complete turn")
    return evidence, pins


def _share_request_checkpoint(
    job: Path, payload: Path, evidence: ResponseEvidence, pins: dict[str, str]
) -> bool:
    """Share only exact immutable bytes; temporary links stay outside sealed payloads."""
    source = job / "request.json"
    name = "journal/" + evidence.request_sha256 + ".request.json"
    target = payload / name
    if any(path.is_symlink() for path in (source, target, target.parent, payload)):
        raise ValueError("Request checkpoint sharing cannot follow symlinks")
    before, other = source.lstat(), target.lstat()
    if not stat.S_ISREG(before.st_mode) or not stat.S_ISREG(other.st_mode):
        raise ValueError("Request checkpoints must be private regular files")
    if (before.st_mode | other.st_mode) & 0o077:
        raise ValueError("Request checkpoint permissions are not private")
    if (before.st_dev, before.st_uid, before.st_gid, before.st_mode) != (
        other.st_dev,
        other.st_uid,
        other.st_gid,
        other.st_mode,
    ):
        return False
    if sorted(os.listxattr(source)) != sorted(os.listxattr(target)) or any(
        os.getxattr(source, key) != os.getxattr(target, key) for key in os.listxattr(source)
    ):
        return False
    if _digest(source) != pins[name]:
        # Equivalent JSON is insufficient: each original serialization is evidence.
        return False
    temporary = job / (".request-checkpoint-" + uuid.uuid4().hex)
    try:
        try:
            os.link(source, temporary, follow_symlinks=False)
        except OSError as error:
            if error.errno == errno.EXDEV:
                return False
            raise
        if not stat.S_ISREG(temporary.lstat().st_mode) or _digest(temporary) != pins[name]:
            raise ValueError("Request checkpoint changed during storage sharing")
        os.replace(temporary, target)
        for directory in (job, target.parent):
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        if not source.samefile(target):
            raise ValueError("Request checkpoint changed during storage sharing")
        return True
    finally:
        temporary.unlink(missing_ok=True)


def receive(
    root: Path, identity: str, owner: str, archive_sha256: str, stream: BinaryIO
) -> dict[str, Any]:
    """Preserve original uploaded bytes and acknowledge only a complete independent replay."""
    _hex(owner, 32)
    _hex(archive_sha256, 64)
    job = _job(root, identity)
    with _lock(root):
        state = _read(job / "state.json")
        if state.get("owner") != owner or state["state"] not in {"claimed", "received"}:
            raise ValueError("Inference result belongs to another or unclaimed job")
        uploads = job / "uploads"
        uploads.mkdir(exist_ok=True, mode=0o700)
        archive = uploads / (archive_sha256 + "-" + uuid.uuid4().hex + ".tar.gz")
        count, h = 0, hashlib.sha256()
        with os.fdopen(
            os.open(archive, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb"
        ) as target:
            while chunk := stream.read(1024 * 1024):
                count += len(chunk)
                target.write(chunk)
                h.update(chunk)
                if count > MAX_TRANSFER_BYTES:
                    raise ValueError("Inference evidence transfer exceeds its bounded spool")
            target.flush()
            os.fsync(target.fileno())
        if h.hexdigest() != archive_sha256:
            raise ValueError("Incomplete or changed inference transfer; bytes preserved")
        if (job / "receipt.json").exists():
            receipt = _read(job / "receipt.json")
            if receipt["archive_sha256"] != archive_sha256:
                raise ValueError("Cannot replace a received inference result")
            result(root, identity)
            return receipt
        payload = uploads / archive_sha256
        if payload.exists():
            shutil.move(payload, uploads / (archive_sha256 + ".preserved-" + uuid.uuid4().hex))
        unpack_queue(archive, payload)
        evidence, pins = _validated(job, payload, state)
        shared = _share_request_checkpoint(job, payload, evidence, pins)
        receipt = {
            "job": identity,
            "owner": owner,
            "executor_sha256": state["executor_sha256"],
            "archive_sha256": archive_sha256,
            "response_sha256": evidence.response_sha256,
            "evidence_file_sha256": pins,
            "all_evidence_files_verified": True,
            "request_checkpoint_storage": "shared" if shared else "independent",
            "transport_only": True,
            "article_approved": False,
            "received_at": utc_now(),
        }
        state.update(
            state="received", payload=payload.relative_to(job).as_posix(), error=evidence.error
        )
        _write(job / "state.json", state)
        _write(job / "receipt.json", receipt)
        return receipt


def result(root: Path, identity: str) -> ResponseEvidence | None:
    """Every result read rechecks preserved source and returned evidence; no stale cache."""
    job = _job(root, identity)
    if not (job / "receipt.json").exists() or (job / "invalidated.json").exists():
        return None
    state = _read(job / "state.json")
    evidence, pins = _validated(job, job / state["payload"], state)
    receipt = _read(job / "receipt.json")
    if (
        receipt["response_sha256"] != evidence.response_sha256
        or receipt["evidence_file_sha256"] != pins
    ):
        raise ValueError("Inference receipt differs from preserved result")
    return evidence


def invalidate(root: Path, identity: str, response_sha256: str) -> None:
    """Retain rejected transport evidence without spending or resetting semantic budgets."""
    _hex(response_sha256, 64)
    job = _job(root, identity)
    with _lock(root):
        receipt = _read(job / "receipt.json")
        if receipt["response_sha256"] != response_sha256:
            raise ValueError("Cannot invalidate another inference response")
        _write(
            job / "invalidated.json",
            {
                "response_sha256": response_sha256,
                "article_approved": False,
                "reason": "independent_semantic_validation_failed",
            },
        )


def queue_status(root: Path, owner: str) -> dict[str, Any]:
    """Expose queue and claim ages without article bodies, media or raw diagnostics."""
    _hex(owner, 32)
    states = [_read(path) for path in sorted(root.glob("*/*/state.json"))]
    return {
        "queued": sum(s["state"] == "queued" for s in states),
        "claimed": sum(s["state"] == "claimed" for s in states),
        "owned": sum(s["state"] == "claimed" and s.get("owner") == owner for s in states),
        "checked_at": utc_now(),
    }
