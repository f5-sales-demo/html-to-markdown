"""Independent private Ubuntu collectors at the existing daily and Sunday clocks."""

from __future__ import annotations

import asyncio
import fcntl
import platform
import re
import shutil
import subprocess  # nosec B404
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx

from .content_inventory import atomic_private, collect_content
from .curation import json_bytes
from .known_candidates import snapshot_known_candidates
from .models import utc_now
from .refresh import _verified_baseline
from .urls import SOURCE_ROOTS


def allocate_collection_run(root: Path) -> Path:
    """Create a private run atomically while preserving every concurrent capture."""
    base = "capture-" + utc_now().replace(":", "").replace("-", "")
    for index in range(10000):
        suffix = "" if index == 0 else f"-{index:04d}"
        run = root / (base + suffix)
        try:
            run.mkdir(mode=0o700)
        except FileExistsError:
            continue
        return run
    raise ValueError("Capture run allocation exhausted; existing evidence is preserved")


def verified_baseline_files(root: Path, tag: str, pin: str) -> dict[str, bytes]:
    """Serialize initial downloads and expose only independently verified baseline bytes."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,128}", tag):
        raise ValueError("Unsafe baseline release tag")
    cache = root / ".baselines"
    if cache.is_symlink():
        raise ValueError("Baseline cache cannot be a symlink")
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    baseline = cache / tag
    with (cache / (tag + ".lock")).open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if baseline.exists():
            return _verified_baseline(baseline, pin)
        gh = shutil.which("gh")
        if gh is None:
            raise ValueError("Existing Ubuntu GitHub credential CLI is unavailable")
        staging = cache / (".download-" + uuid.uuid4().hex)
        staging.mkdir(mode=0o700)
        subprocess.run(
            [
                gh,
                "release",
                "download",
                tag,
                "--repo",
                "f5-sales-demo/html-to-markdown",
                "--dir",
                str(staging),
            ],
            check=True,
            capture_output=True,
        )  # nosec B603
        files = _verified_baseline(staging, pin)
        staging.rename(baseline)
        return files


async def scheduled_collection(root: Path, *, source: str, full: bool) -> dict[str, Any]:
    if platform.system() != "Linux":
        raise ValueError("private collectors run independently on Ubuntu")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            "https://f5-sales-demo.github.io/html-to-markdown/api/revision.json"
        )
        response.raise_for_status()
        revision = response.json()
    tag, pin = revision["snapshot_tag"], revision["publication_sha256"]
    files = verified_baseline_files(root, tag, pin)
    run = allocate_collection_run(root)
    previous = run / "previous-manifest.json"
    atomic_private(previous, files["manifest.json"])
    selected = (
        list(SOURCE_ROOTS)
        if source == "all"
        else (
            [name for name in SOURCE_ROOTS if name != "docs-cloud-f5-com"]
            if source == "other"
            else [source]
        )
    )
    reports: dict[str, Any] = {}

    async def collect(name: str) -> None:
        lock_path = root / (name + ".collector.lock")
        with lock_path.open("a+b") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                reports[name] = {"state": "deferred_active_collector"}
                return
            known = run / (name + "-known-candidates.json")
            atomic_private(known, json_bytes(snapshot_known_candidates(root, name)))
            reports[name] = await collect_content(
                run / name,
                source=name,
                previous_manifest=previous,
                known_candidates=known,
                full=full,
                capture_store=root / ".capture-store",
            )

    await asyncio.gather(*(collect(name) for name in selected))
    result = {
        "started_at": run.name,
        "baseline_tag": tag,
        "baseline_receipt_sha256": pin,
        "full": full,
        "sources": {
            name: {
                "counts": report.get("counts"),
                "completeness": report.get("completeness"),
                "state": report.get("state", "completed"),
            }
            for name, report in reports.items()
        },
    }
    atomic_private(run / "collection-receipt.json", json_bytes(result))
    return result


def install_collectors(root: Path) -> list[str]:
    if platform.system() != "Linux":
        raise ValueError("collection timers are installed on Ubuntu")
    units = Path.home() / ".config" / "systemd" / "user"
    units.mkdir(parents=True, exist_ok=True)
    executable = Path(sys.executable).parent / "html-to-markdown"
    names = []
    for name, source, calendar, full in [
        ("official-daily", "docs-cloud-f5-com", "*-*-* 04:23:00 UTC", False),
        ("other-daily", "other", "*-*-* 04:43:00 UTC", False),
        ("full-sunday", "all", "Sun *-*-* 03:00:00 UTC", True),
    ]:
        label = "html-to-markdown-" + name
        command = f'"{executable}" scheduled-collection --queue-root "{root}" --source {source}' + (
            " --full" if full else ""
        )
        service = f"[Unit]\nDescription=Private F5 content {name} collector\n[Service]\nType=oneshot\nUMask=0077\nExecStart={command}\nTimeoutStartSec=12h\n"
        timer = f"[Unit]\nDescription=F5 content {name} collection clock\n[Timer]\nOnCalendar={calendar}\nPersistent=true\n[Install]\nWantedBy=timers.target\n"
        atomic_private(units / (label + ".service"), service.encode())
        atomic_private(units / (label + ".timer"), timer.encode())
        names.append(label + ".timer")
    subprocess.run(
        ["/usr/bin/systemctl", "--user", "daemon-reload"], check=True, capture_output=True
    )  # nosec B603
    subprocess.run(
        ["/usr/bin/systemctl", "--user", "enable", "--now", *names], check=True, capture_output=True
    )  # nosec B603
    return names
