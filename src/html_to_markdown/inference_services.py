"""User services preserve Ubuntu capture clocks and the Mac credential route."""

from __future__ import annotations

import fcntl
import os
import platform
import plistlib
import re
import shlex
import subprocess  # nosec B404
import sys
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from .content_inventory import atomic_private
from .curation import json_bytes
from .operation_mode import (
    apply_timer_mode,
    mac_agent_schedule,
    operation_mode,
    validate_mac_agent_schedule,
)


def install_processing_service(
    *,
    queue_root: Path,
    inference_queue: Path,
    output: Path,
    executable: str,
    transport: Path,
    source_commit: str | None = None,
) -> dict[str, str]:
    """Ubuntu owns all durable source, review, curation and packaging work."""
    if platform.system() != "Linux":
        raise ValueError("Processing services run on Ubuntu")
    if not all(path.is_absolute() for path in (queue_root, inference_queue, output, transport)):
        raise ValueError("Processing service paths must be absolute")
    operation_mode(queue_root)
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    command = [
        str(Path(sys.executable).parent / "html-to-markdown"),
        "processing-worker",
        "--queue-root",
        str(queue_root),
        "--inference-queue",
        str(inference_queue),
        "--output",
        str(output),
        "--executable",
        executable,
        "--transport",
        str(transport),
    ]
    if source_commit is not None:
        command += ["--source-commit", source_commit]
    units = Path.home() / ".config/systemd/user"
    units.mkdir(parents=True, exist_ok=True)
    label = "html-to-markdown-processing"
    service = (
        "[Unit]\nDescription=Private Ubuntu content processing\n[Service]\nType=exec\nUMask=0077\nExecStart="
        + shlex.join(command)
        + "\n"
    )
    timer = "[Unit]\nDescription=Private content processing clock\n[Timer]\nOnBootSec=30\nOnUnitInactiveSec=900\n[Install]\nWantedBy=timers.target\n"
    atomic_private(units / (label + ".service"), service.encode())
    atomic_private(units / (label + ".timer"), timer.encode())
    apply_timer_mode(queue_root, [label + ".timer"])
    return {"service": label + ".service", "timer": label + ".timer"}


def stage_inference_agent(
    *, host: str, remote_executable: str, remote_queue: str, output: Path, transport: Path
) -> Path:
    """Stage a bounded worker; credentials are loaded by the existing login route."""
    if platform.system() != "Darwin":
        raise ValueError("Inference launch agents run on the MacBook")
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    command = [
        str(Path(sys.executable).parent / "html-to-markdown"),
        "mac-inference-worker",
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
    definition = {
        "Label": "com.f5-sales-demo.content-review",
        "ProgramArguments": ["/bin/zsh", "-l", "-c", "exec " + shlex.join(command)],
        "ProcessType": "Background",
        "Umask": 0o077,
        "StandardOutPath": str(output / "worker.private.log"),
        "StandardErrorPath": str(output / "worker.private.err"),
    }
    path = output / "agent.pending.plist"
    atomic_private(path, plistlib.dumps(mac_agent_schedule(output, definition), sort_keys=True))
    return path


def activate_inference_agent(output: Path) -> dict[str, Any]:
    """Replace the legacy agent only after actual worker and inference quiescence."""
    if platform.system() != "Darwin":
        raise ValueError("Inference launch agents run on the MacBook")
    target = "gui/" + str(os.getuid()) + "/com.f5-sales-demo.content-review"
    state = subprocess.run(
        ["/bin/launchctl", "print", target], capture_output=True, text=True, check=False
    )  # nosec B603
    if re.search(r"^\s*pid = \d+\s*$", state.stdout, re.M):
        return {"state": "waiting_for_live_worker"}
    base = Path.home() / ".local/share/html-to-markdown"
    with ExitStack() as stack:
        try:
            for path in [
                base / "worker/worker.lock",
                base / "inference.lock",
                output / "worker.lock",
            ]:
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                lock = stack.enter_context(path.open("a+b"))
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"state": "waiting_for_live_inference"}
        state = subprocess.run(
            ["/bin/launchctl", "print", target], capture_output=True, text=True, check=False
        )  # nosec B603
        if re.search(r"^\s*pid = \d+\s*$", state.stdout, re.M):
            return {"state": "waiting_for_live_worker"}
        pending = output / "agent.pending.plist"
        data = pending.read_bytes()
        definition = plistlib.loads(data)
        mode = validate_mac_agent_schedule(output, definition)
        if "mac-inference-worker" not in definition["ProgramArguments"][-1]:
            raise ValueError("Pending launch agent is not the bounded inference worker")
        destination = Path.home() / "Library/LaunchAgents/com.f5-sales-demo.content-review.plist"
        if state.returncode == 0:
            subprocess.run(["/bin/launchctl", "bootout", target], capture_output=True, check=True)  # nosec B603
        if destination.exists():
            original = destination.read_bytes()
            atomic_private(output / "previous-agent.private.plist", original)
        atomic_private(destination, data)
        subprocess.run(["/bin/launchctl", "enable", target], capture_output=True, check=True)  # nosec B603
        subprocess.run(
            ["/bin/launchctl", "bootstrap", "gui/" + str(os.getuid()), str(destination)],
            capture_output=True,
            check=True,
        )  # nosec B603
    receipt = {
        "state": "inference_agent_activated",
        "interval_seconds": 900 if mode == "scheduled" else None,
        "operation_mode": mode,
        "active_inference_interrupted": False,
    }
    atomic_private(output / "agent-activation.private.json", json_bytes(receipt))
    return receipt
