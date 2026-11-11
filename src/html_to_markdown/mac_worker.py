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
from pathlib import Path
from typing import Any

from .content_inventory import atomic_private
from .content_policy import load_content_policy
from .curation import json_bytes, load_curation_policy
from .enrichment.analysis import sha
from .models import utc_now
from .source_inputs import seal_source
from .source_review import review_context_digest
from .source_review_jobs import run_reviews
from .work_queue import pack_private_tree, unpack_queue


def worker_once(
    *,
    host: str,
    remote_executable: str,
    remote_queue: str,
    output: Path,
    transport: Path,
    source_commit: str | None = None,
) -> dict[str, Any]:
    if platform.system() != "Darwin":
        raise ValueError("the review worker runs exclusively on the MacBook")
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
                and previous.get("completed")
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
                    subprocess.run(
                        args, stdout=stream, stderr=subprocess.PIPE, check=True, timeout=600
                    )  # nosec B603
                capture = directory / "capture"
                unpack_queue(archive, capture)
            if entry.get("kind") == "enrichment":
                from .enrichment.generate_v2 import (
                    generate_v2,  # pylint: disable=import-outside-toplevel
                )

                decisions = capture / "enrichment-decisions.json"
                generate_v2(
                    capture / "combined", decisions, phase="all", journal=directory / "journal"
                )
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
                    subprocess.run(
                        args, stdin=stream, capture_output=True, check=True, timeout=1800
                    )  # nosec B603
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
            run_reviews(capture, review, phase="prepare", journal=directory / "journal")
            report = run_reviews(capture, review, phase="all", journal=directory / "journal")
            completed = not report["exceptions"]
            # Passing articles progress while exceptions stay visible.
            sealed = directory / "sealed" / (entry["fingerprint"] + "-" + sha(json_bytes(report)))
            if not sealed.exists():
                seal_source(capture, review, sealed)
            admission_pin = sha((sealed / "source-review-receipt.json").read_bytes())
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
            atomic_private(
                checkpoint,
                json_bytes(
                    {
                        "fingerprint": entry["fingerprint"],
                        "request_context_sha256": context_pin,
                        "completed": completed,
                        "reviewed_at": utc_now(),
                        "exceptions": len(report["exceptions"]),
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
