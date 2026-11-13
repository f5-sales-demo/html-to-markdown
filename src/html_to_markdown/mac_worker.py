"""Awake MacBook queue worker; credentials remain in the existing Codex route."""

from __future__ import annotations

import fcntl
import json
import os
import platform
import shlex
import subprocess  # nosec B404
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path
from typing import Any

from .content_inventory import atomic_private
from .content_policy import load_content_policy
from .curation import json_bytes, load_curation_policy
from .curation_renewal_worker import advance_renewals
from .enrichment.analysis import sha
from .enrichment.generate_v2 import generate_v2
from .models import utc_now
from .source_inputs import seal_source
from .source_review import review_context_digest
from .source_review_jobs import run_reviews
from .work_queue import pack_private_tree, unpack_queue


# Preserve all stage evidence and checks in one transaction.
# pylint: disable-next=too-many-locals,too-many-statements
def worker_once(
    *,
    host: str,
    remote_executable: str,
    remote_queue: str,
    output: Path,
    transport: Path,
    source_commit: str | None = None,
    batch_size: int = 8,
) -> dict[str, Any]:
    if platform.system() != "Darwin":
        raise ValueError("the review worker runs exclusively on the MacBook")
    if batch_size < 1:
        raise ValueError("review batch size must be positive")
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (output / "worker.lock").open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"state": "already_running"}
        status: dict[str, Any] = {
            "state": "checking",
            "checked_at": utc_now(),
            "concurrency": 4,
            "queues": [],
        }
        atomic_private(output / "worker-status.json", json_bytes(status))
        # pylint: disable=import-outside-toplevel
        from .policy_activation import require_active_cohort
        from .policy_transport import synchronize_policy  # pylint: disable=import-outside-toplevel

        policy_state = Path(
            os.environ.get(
                "HTML_TO_MARKDOWN_POLICY_STATE",
                str(Path.home() / ".local/share/html-to-markdown/policy"),
            )
        )
        status["policy_activation"] = synchronize_policy(
            transport,
            host,
            remote_executable,
            os.environ.get(
                "HTML_TO_MARKDOWN_POLICY_REPOSITORY", "/data/robin-GIT/html-to-markdown"
            ),
            os.environ.get(
                "HTML_TO_MARKDOWN_REMOTE_POLICY_STATE", str(Path(remote_queue).parent / "policy")
            ),
            policy_state,
            remote_queue,
        )
        require_active_cohort(policy_state)
        atomic_private(output / "worker-status.json", json_bytes(status))
        args = [
            str(transport),
            "--host",
            host,
            "--",
            remote_executable,
            "list-review-queues",
            "--queue-root",
            remote_queue,
        ]
        result = subprocess.run(args, capture_output=True, check=True, timeout=60)  # nosec B603
        queues = json.loads(result.stdout)
        if not isinstance(queues, list):
            raise ValueError("private queue list is not an array")
        for entry in queues:
            identity = entry["queue"]
            path = Path(identity)
            if path.is_absolute() or ".." in path.parts or len(path.parts) != 2:
                raise ValueError("unsafe private queue identity")
            directory = output / path
            checkpoint = directory / "checkpoint.json"
            previous = json.loads(checkpoint.read_bytes()) if checkpoint.exists() else {}
            context_pin = sha(
                json_bytes(
                    {
                        "source_review": review_context_digest(),
                        "curation": load_curation_policy().sha256,
                        "api": load_content_policy().policy_digest,
                        "enrichment": {
                            name: sha((Path(__file__).parent / "enrichment" / name).read_bytes())
                            for name in ("contracts_v2.py", "generate_v2.py", "v2.py")
                        },
                    }
                )
            )
            if (
                previous.get("fingerprint") == entry["fingerprint"]
                and previous.get("request_context_sha256") == context_pin
                and (previous.get("completed") or previous.get("settled"))
            ):
                continue
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            status["state"] = "reviewing"
            status["queues"].append(
                {
                    "queue": identity,
                    "captured": entry["captured"],
                    "discovered": entry["discovered"],
                }
            )
            atomic_private(output / "worker-status.json", json_bytes(status))
            capture = directory / "capture"
            capture_checkpoint = directory / "capture-checkpoint.json"
            received = (
                json.loads(capture_checkpoint.read_bytes()) if capture_checkpoint.exists() else {}
            )
            if (
                received.get("fingerprint") != entry["fingerprint"]
                or not (
                    capture
                    / (
                        "enrichment-decisions.json"
                        if entry.get("kind") == "enrichment"
                        else "inventory.json"
                    )
                ).exists()
            ):
                _receive_capture(
                    transport, host, remote_executable, remote_queue, identity, capture
                )
                atomic_private(
                    capture_checkpoint, json_bytes({"fingerprint": entry["fingerprint"]})
                )
            if entry.get("kind") == "enrichment":
                decisions = capture / "enrichment-decisions.json"
                enrichment = generate_v2(
                    capture / "combined",
                    decisions,
                    phase="all",
                    journal=output / "journal",
                    limit=batch_size,
                )
                if enrichment["pending"]:
                    atomic_private(
                        checkpoint,
                        json_bytes(
                            {
                                "fingerprint": entry["fingerprint"],
                                "request_context_sha256": context_pin,
                                "completed": False,
                                "reviewed_at": utc_now(),
                                "pending": enrichment["pending"],
                            }
                        ),
                    )
                    continue
                _return_enrichment(
                    transport, host, remote_executable, remote_queue, identity, decisions
                )
                atomic_private(
                    checkpoint,
                    json_bytes(
                        {
                            "fingerprint": entry["fingerprint"],
                            "request_context_sha256": context_pin,
                            "completed": True,
                            "reviewed_at": utc_now(),
                        }
                    ),
                )
                continue
            review = directory / "review"
            run_reviews(capture, review, phase="prepare", journal=output / "journal")
            report = run_reviews(
                capture, review, phase="all", journal=output / "journal", limit=batch_size
            )
            try:
                status["curation"] = advance_renewals(
                    capture,
                    review,
                    directory / "curation-renewal",
                    output / "journal",
                    transport,
                    host,
                    remote_executable,
                    remote_queue,
                )
            except (ValueError, OSError, subprocess.CalledProcessError) as failure:
                # Curation jobs cannot prevent passing source articles or other
                # queues from advancing. Full diagnostics remain private.
                atomic_private(
                    directory / "curation-renewal-error.json", json_bytes({"error": str(failure)})
                )
                status["curation"] = {"state": "queued", "error_class": type(failure).__name__}
            completed = not report["exceptions"] and not report["pending"]
            # Passing articles progress while exceptions stay visible.
            sealed = directory / "sealed" / (entry["fingerprint"] + "-" + sha(json_bytes(report)))
            if not sealed.exists():
                seal_source(capture, review, sealed)
            admission_pin = sha((sealed / "source-review-receipt.json").read_bytes())
            if previous.get("returned_admission_sha256") != admission_pin:
                _return_source(
                    transport,
                    host,
                    remote_executable,
                    remote_queue,
                    identity,
                    sealed,
                    admission_pin,
                )
            atomic_private(
                checkpoint,
                json_bytes(
                    {
                        "fingerprint": entry["fingerprint"],
                        "request_context_sha256": context_pin,
                        "completed": completed,
                        "settled": not report["pending"],
                        "reviewed_at": utc_now(),
                        "exceptions": len(report["exceptions"]),
                        "pending": report["pending"],
                        "returned_admission_sha256": admission_pin,
                    }
                ),
            )
        status["state"] = "idle"
        args = [
            str(transport),
            "--host",
            host,
            "--",
            remote_executable,
            "advance-publication",
            "--queue-root",
            remote_queue,
        ]
        if source_commit is not None:
            args.extend(["--source-commit", source_commit])
        publication = subprocess.run(args, capture_output=True, check=True, timeout=1800)  # nosec B603
        status["publication"] = json.loads(publication.stdout)
        status["ended_at"] = utc_now()
        atomic_private(output / "worker-status.json", json_bytes(status))
        return status


def _receive_capture(
    transport: Path,
    host: str,
    remote_executable: str,
    remote_queue: str,
    identity: str,
    capture: Path,
) -> None:
    with tempfile.TemporaryDirectory(prefix="private-content-queue-") as temporary:
        archive = Path(temporary) / "queue.tar.gz"
        with archive.open("wb") as stream:
            args = [
                str(transport),
                "--host",
                host,
                "--",
                remote_executable,
                "export-review-queue",
                "--queue-root",
                remote_queue,
                "--queue",
                identity,
            ]
            subprocess.run(args, stdout=stream, stderr=subprocess.PIPE, check=True, timeout=600)  # nosec B603
        unpack_queue(archive, capture)


def _return_enrichment(
    transport: Path,
    host: str,
    remote_executable: str,
    remote_queue: str,
    identity: str,
    decisions: Path,
) -> None:
    digest = sha(decisions.read_bytes())
    args = [
        str(transport),
        "--host",
        host,
        "--stdin",
        "--",
        remote_executable,
        "receive-enrichment-input",
        "--queue-root",
        remote_queue,
        "--queue",
        identity,
        "--decisions-sha256",
        digest,
    ]
    with decisions.open("rb") as stream:
        subprocess.run(args, stdin=stream, capture_output=True, check=True, timeout=1800)  # nosec B603


def _return_source(
    transport: Path,
    host: str,
    remote_executable: str,
    remote_queue: str,
    identity: str,
    sealed: Path,
    admission_pin: str,
) -> None:
    with tempfile.TemporaryDirectory(prefix="reviewed-source-transfer-") as temporary:
        archive = Path(temporary) / "source.tar.gz"
        with archive.open("wb") as stream:
            pack_private_tree(sealed, stream)
        args = [
            str(transport),
            "--host",
            host,
            "--stdin",
            "--",
            remote_executable,
            "receive-review-input",
            "--queue-root",
            remote_queue,
            "--queue",
            identity,
            "--review-sha256",
            admission_pin,
        ]
        with archive.open("rb") as stream:
            subprocess.run(args, stdin=stream, capture_output=True, check=True, timeout=600)  # nosec B603


def monitored_worker(**kwargs: Any) -> dict[str, Any]:
    """Check the queue every 900 seconds independently of active inference."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(worker_once, **kwargs)
        deadline = time.monotonic() + 900
        while True:
            try:
                return future.result(timeout=max(0.01, min(30, deadline - time.monotonic())))
            except FutureTimeout:
                if future.done():
                    return future.result()
                if time.monotonic() < deadline:
                    continue
                heartbeat: dict[str, Any] = {"checked_at": utc_now(), "state": "checking"}
                args = [
                    str(kwargs["transport"]),
                    "--host",
                    kwargs["host"],
                    "--",
                    kwargs["remote_executable"],
                    "list-review-queues",
                    "--queue-root",
                    kwargs["remote_queue"],
                ]
                try:
                    response = subprocess.run(args, capture_output=True, check=True, timeout=60)  # nosec B603
                    queues = json.loads(response.stdout)
                    if not isinstance(queues, list):
                        raise ValueError("queue heartbeat did not return an array") from None
                    heartbeat.update(state="inference_active", queues=len(queues))
                except Exception as failure:  # pylint: disable=broad-exception-caught
                    heartbeat.update(state="queue_check_failed", error_class=type(failure).__name__)
                atomic_private(kwargs["output"] / "queue-heartbeat.json", json_bytes(heartbeat))
                deadline = time.monotonic() + 900


def install_launch_agent(
    *,
    host: str,
    remote_executable: str,
    remote_queue: str,
    output: Path,
    transport: Path,
    source_commit: str | None = None,
) -> Path:
    import plistlib  # pylint: disable=import-outside-toplevel

    if platform.system() != "Darwin":
        raise ValueError("launch agents are installed on the MacBook")
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    executable = Path(sys.executable).parent / "html-to-markdown"
    command = [
        str(executable),
        "review-worker",
        "--monitor",
        "--host",
        host,
        "--remote-executable",
        remote_executable,
        "--remote-queue",
        remote_queue,
        "--output",
        str(output),
        "--transport",
        str(transport),
    ]
    if source_commit is not None:
        command.extend(["--source-commit", source_commit])
    label = "com.f5-sales-demo.content-review"
    # A login shell loads the established credential route. The plist stores
    # executable paths and arguments, never an API key or provider override.
    definition = {
        "Label": label,
        "ProgramArguments": ["/bin/zsh", "-l", "-c", "exec " + shlex.join(command)],
        "StartInterval": 900,
        "RunAtLoad": True,
        "ProcessType": "Background",
        "StandardOutPath": str(output / "worker.private.log"),
        "StandardErrorPath": str(output / "worker.private.err"),
        "Umask": 0o077,
    }
    destination = Path.home() / "Library" / "LaunchAgents" / (label + ".plist")
    atomic_private(destination, plistlib.dumps(definition, sort_keys=True))
    subprocess.run(
        ["/bin/launchctl", "bootstrap", "gui/" + str(os.getuid()), str(destination)],
        check=True,
        capture_output=True,
    )  # nosec B603
    return destination
