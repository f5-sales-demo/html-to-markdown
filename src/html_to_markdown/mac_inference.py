"""Bounded Mac inference spools, acknowledged against durable Ubuntu evidence."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import platform
import re
import shutil
import subprocess  # nosec B404
import time
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path
from typing import Any

from .content_inventory import atomic_private
from .curation import json_bytes
from .enrichment.codex_client import CodexClient
from .enrichment.gates import request_hash
from .enrichment.queued_client import executor_context
from .inference_executor_contract import synchronize as synchronize_executor
from .models import utc_now
from .work_queue import pack_private_tree


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        while data := stream.read(1024 * 1024):
            value.update(data)
    return value.hexdigest()


def _job(output: Path, identity: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{64}/[0-9]{6}", identity):
        raise ValueError("Unsafe private inference identity")
    path = output / "jobs" / identity
    if any(parent.is_symlink() for parent in [path, path.parent, path.parent.parent, output]):
        raise ValueError("Inference spool cannot follow symlinks")
    return path


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError("Invalid private inference spool record")
    return value


def _pins(job: Path) -> dict[str, str]:
    result = {}
    for path in sorted(job.rglob("*")):
        if path.is_symlink():
            raise ValueError("Inference spool contains a symlink")
        if path.is_file() and path.relative_to(job).as_posix() != "manifest.json":
            result[path.relative_to(job).as_posix()] = _digest(path)
    return result


def _seal(job: Path) -> dict[str, str]:
    manifest = job / "manifest.json"
    if not manifest.exists():
        atomic_private(manifest, json_bytes(_pins(job)))
    pins = _read(manifest)
    if pins != _pins(job):
        raise ValueError("Completed inference spool changed before acknowledgment")
    return pins


def _execute(job: Path, original: CodexClient) -> None:
    if (job / "envelope.json").exists():
        return
    claim = _read(job / "claim.json")
    if claim["executor_sha256"] != executor_context():
        raise ValueError("Pending inference requires its exact installed executor")
    if request_hash(claim["request"]) != claim["job"].split("/")[0]:
        raise ValueError("Pending inference original request changed")
    # Each view shares the dispatcher's global four-job lock and configured route,
    # while preserving its own complete original attempts in a bounded job journal.
    view = copy.copy(original)
    view.journal = job / "journal"
    view.journal.mkdir(exist_ok=True, mode=0o700)
    evidence = view.execute(claim["request"])
    atomic_private(
        job / "envelope.json",
        json_bytes(
            {key: claim[key] for key in ("job", "owner", "executor_sha256")}
            | {"evidence": evidence.model_dump()}
        ),
    )


def _archive(output: Path, job: Path) -> tuple[Path, str]:
    identity = _read(job / "claim.json")["job"].replace("/", "-")
    directory = output / "transfers"
    directory.mkdir(exist_ok=True, mode=0o700)
    archive = directory / (identity + ".tar.gz")
    pointer = directory / (identity + ".json")
    _seal(job)
    if pointer.exists():
        record = _read(pointer)
        if _digest(archive) != record["archive_sha256"]:
            raise ValueError("Preserved inference transfer archive changed")
        return archive, record["archive_sha256"]
    if archive.exists():
        # A crash before the transfer pointer committed left an incomplete
        # representation. Keep it recoverable; original journal bytes are intact.
        history = job / "transfer-history" / uuid.uuid4().hex
        history.mkdir(parents=True, mode=0o700)
        archive.rename(history / "interrupted.tar.gz")
        (job / "manifest.json").rename(history / "sealed-manifest.json")
        _seal(job)
    with archive.open("xb") as stream:
        pack_private_tree(job, stream)
    digest = _digest(archive)
    atomic_private(pointer, json_bytes({"archive_sha256": digest}))
    return archive, digest


def _return(
    output: Path, job: Path, *, host: str, executable: str, queue: str, transport: Path
) -> None:
    claim = _read(job / "claim.json")
    archive, digest = _archive(output, job)
    pins = _seal(job)
    command = [
        str(transport),
        "--host",
        host,
        "--stdin",
        "--",
        executable,
        "receive-inference-result",
        "--queue-root",
        queue,
        "--job",
        claim["job"],
        "--owner",
        claim["owner"],
        "--archive-sha256",
        digest,
    ]
    with archive.open("rb") as stream:
        response = subprocess.run(
            command, stdin=stream, capture_output=True, check=True, timeout=1800
        )  # nosec B603
    receipt = json.loads(response.stdout)
    expected = {
        "job": claim["job"],
        "owner": claim["owner"],
        "executor_sha256": claim["executor_sha256"],
        "archive_sha256": digest,
        "evidence_file_sha256": pins,
        "all_evidence_files_verified": True,
        "transport_only": True,
        "article_approved": False,
    }
    if any(receipt.get(key) != value for key, value in expected.items()):
        raise ValueError("Ubuntu acknowledgment does not verify the complete Mac spool")
    if _seal(job) != pins:
        raise ValueError("Mac spool changed during verified return")
    receipts = output / "receipts"
    identity = claim["job"].replace("/", "-")
    atomic_private(receipts / (identity + ".json"), json_bytes(receipt))
    # The transport and all model attempts now exist in verified private Ubuntu
    # storage. Cleanup follows the receipt, never a model success or HTTP status.
    shutil.rmtree(job)
    archive.unlink()
    # Transfer pointer uses the flat job name without the archive extension.
    (output / "transfers" / (identity + ".json")).unlink(missing_ok=True)


def _load_jobs(
    output: Path, owner: str, *, host: str, executable: str, queue: str, transport: Path
) -> list[Path]:
    jobs = [p.parent for p in sorted((output / "jobs").glob("*/*/claim.json"))]
    if not jobs:
        command = [
            str(transport),
            "--host",
            host,
            "--",
            executable,
            "claim-inference-jobs",
            "--queue-root",
            queue,
            "--owner",
            owner,
            "--executor-sha256",
            executor_context(),
        ]
        response = subprocess.run(command, capture_output=True, check=True, timeout=600)  # nosec B603
        claims = json.loads(response.stdout)
        if not isinstance(claims, list) or len(claims) > 4:
            raise ValueError("Private inference queue returned an invalid job batch")
        for claim in claims:
            if claim["owner"] != owner or claim["executor_sha256"] != executor_context():
                raise ValueError("Claimed inference owner or executor changed")
            job = _job(output, claim["job"])
            if request_hash(claim["request"]) != claim["job"].split("/")[0]:
                raise ValueError("Claimed inference request changed")
            job.mkdir(parents=True, exist_ok=True, mode=0o700)
            atomic_private(job / "claim.json", json_bytes(claim))
            jobs.append(job)
    if len(jobs) > 4:
        raise ValueError("Mac inference spool exceeds four active jobs")
    return jobs


def _infer_jobs(
    output: Path,
    jobs: list[Path],
    status: dict[str, Any],
    client_factory: Callable[..., CodexClient],
) -> None:
    ready = [job for job in jobs if not (job / "envelope.json").exists()]
    if ready:
        client = client_factory(output / "dispatcher-journal")
        try:
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = {pool.submit(_execute, job, client): job for job in ready}
                for future in as_completed(futures):
                    job = futures[future]
                    try:
                        future.result()
                    except Exception as failure:  # pylint: disable=broad-exception-caught
                        status["errors"][job.relative_to(output).as_posix()] = type(
                            failure
                        ).__name__
        finally:
            client.close()


def inference_once(
    *,
    host: str,
    remote_executable: str,
    remote_queue: str,
    output: Path,
    transport: Path,
    client_factory: Callable[..., CodexClient] = CodexClient,
) -> dict[str, Any]:
    """Run at most four isolated current jobs; never mirror a source corpus."""
    if platform.system() != "Darwin":
        raise ValueError("Inference runs exclusively on the MacBook")
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (output / "worker.lock").open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"state": "already_running", "completed": 0, "pending": 0}
        synchronize_executor(transport, host, remote_executable)
        owner_file = output / "owner.json"
        if not owner_file.exists():
            atomic_private(owner_file, json_bytes({"owner": uuid.uuid4().hex}))
        owner = _read(owner_file)["owner"]
        if not isinstance(owner, str) or not re.fullmatch(r"[0-9a-f]{32}", owner):
            raise ValueError("Invalid durable Mac inference owner")
        jobs = _load_jobs(
            output,
            owner,
            host=host,
            executable=remote_executable,
            queue=remote_queue,
            transport=transport,
        )
        status: dict[str, Any] = {
            "state": "running",
            "started_at": utc_now(),
            "completed": 0,
            "pending": len(jobs),
            "article_approved": False,
            "errors": {},
        }
        atomic_private(output / "worker-status.json", json_bytes(status))
        _infer_jobs(output, jobs, status, client_factory)
        for job in jobs:
            if not (job / "envelope.json").exists():
                continue
            try:
                _return(
                    output,
                    job,
                    host=host,
                    executable=remote_executable,
                    queue=remote_queue,
                    transport=transport,
                )
                status["completed"] += 1
            except Exception as failure:  # pylint: disable=broad-exception-caught
                status["errors"][job.relative_to(output).as_posix()] = type(failure).__name__
        status.update(
            state="pending_return" if status["completed"] != len(jobs) else "idle",
            pending=len(jobs) - status["completed"],
            ended_at=utc_now(),
        )
        atomic_private(output / "worker-status.json", json_bytes(status))
        return status


DRAIN_IDLE_GRACE_SECONDS = 30
DRAIN_POLL_SECONDS = 10


def _queue_heartbeat(kwargs: dict[str, Any]) -> None:
    owner = _read(kwargs["output"] / "owner.json")["owner"]
    args = [
        str(kwargs["transport"]),
        "--host",
        kwargs["host"],
        "--",
        kwargs["remote_executable"],
        "inference-queue-status",
        "--queue-root",
        kwargs["remote_queue"],
        "--owner",
        owner,
    ]
    status: dict[str, Any] = {"checked_at": utc_now(), "state": "checking"}
    try:
        response = subprocess.run(args, capture_output=True, check=True, timeout=60)  # nosec B603
        status.update(state="inference_active", queue=json.loads(response.stdout))
    except Exception as failure:  # pylint: disable=broad-exception-caught
        status.update(state="queue_check_failed", error_class=type(failure).__name__)
    atomic_private(kwargs["output"] / "queue-heartbeat.json", json_bytes(status))


def _await_batch(
    future: Future[dict[str, Any]], deadline: float, kwargs: dict[str, Any]
) -> tuple[dict[str, Any], float]:
    while True:
        try:
            result = future.result(timeout=max(0.01, min(30, deadline - time.monotonic())))
        except FutureTimeout:
            if not future.done():
                if time.monotonic() >= deadline:
                    _queue_heartbeat(kwargs)
                    deadline = time.monotonic() + 900
                continue
            # A completed worker can itself raise TimeoutError. Propagate it
            # rather than confusing it with a live Future's polling timeout.
            result = future.result()
        if time.monotonic() >= deadline:
            _queue_heartbeat(kwargs)
            deadline = time.monotonic() + 900
        return result, deadline


def monitored_inference(**kwargs: Any) -> dict[str, Any]:
    """Drain ready four-job batches, preserving the fifteen-minute idle clock."""
    completed, batches = 0, 0
    quiet_deadline: float | None = None
    heartbeat_deadline = time.monotonic() + 900
    with ThreadPoolExecutor(max_workers=1) as pool:
        while True:
            future = pool.submit(inference_once, **kwargs)
            batch, heartbeat_deadline = _await_batch(future, heartbeat_deadline, kwargs)
            completed += batch.get("completed", 0)
            batches += 1
            report = {
                **batch,
                "completed": completed,
                "batches": batches,
                "article_approved": False,
            }
            atomic_private(kwargs["output"] / "monitor-status.json", json_bytes(report))
            if batch.get("state") != "idle":
                return report
            if batch.get("completed", 0):
                quiet_deadline = time.monotonic() + DRAIN_IDLE_GRACE_SECONDS
            elif quiet_deadline is None or time.monotonic() >= quiet_deadline:
                return report
            time.sleep(min(DRAIN_POLL_SECONDS, max(0, quiet_deadline - time.monotonic())))
