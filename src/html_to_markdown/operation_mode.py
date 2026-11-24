"""Explicit Ubuntu operation mode survives service upgrades and legacy installers."""

from __future__ import annotations

import fcntl
import json
import os
import platform
import stat
import subprocess  # nosec B404
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .content_inventory import atomic_private
from .curation import json_bytes

FILENAME = "operation-mode.private.json"
MARKER = "automatic-operation.private.enabled"
TIMERS = (
    "html-to-markdown-official-daily.timer",
    "html-to-markdown-other-daily.timer",
    "html-to-markdown-full-sunday.timer",
    "html-to-markdown-processing.timer",
)


def _root(root: Path) -> None:
    if not root.is_absolute() or root.is_symlink() or any(ord(c) < 32 for c in str(root)):
        raise ValueError("unsafe operation mode queue root")


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate operation mode key")
        result[key] = value
    return result


def operation_mode(root: Path) -> str:
    """A fresh queue is manual; invalid explicit configuration cannot enable clocks."""
    _root(root)
    path = root / FILENAME
    if path.is_symlink():
        raise ValueError("unsafe operation mode configuration link")
    if not path.exists():
        return "manual"
    if not path.is_file() or path.stat().st_size > 4096:
        raise ValueError("invalid operation mode configuration file")
    payload = path.read_bytes()
    if len(payload) > 4096:
        raise ValueError("operation mode configuration exceeds size bound")
    try:
        value = json.loads(payload, object_pairs_hook=_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as failure:
        raise ValueError("invalid operation mode JSON") from failure
    if not isinstance(value, dict) or set(value) != {"schema_version", "mode"}:
        raise ValueError("invalid operation mode schema")
    version = value["schema_version"]
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version != 1
        or value["mode"] not in ("manual", "scheduled")
    ):
        raise ValueError("invalid operation mode schema")
    return str(value["mode"])


@contextmanager
def _lock(root: Path) -> Iterator[None]:
    _root(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(
        root / ".operation-mode.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "a+b") as lock:
        if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
            raise ValueError("unsafe operation mode lock")
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _guards(root: Path, names: list[str], mode: str) -> None:
    marker = root / MARKER
    if marker.is_symlink() or (marker.exists() and not marker.is_file()):
        raise ValueError("unsafe automatic operation marker")
    # Systemd expands percent specifiers and parses quoted path arguments.
    path = str(marker).replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
    guard = ('[Unit]\nConditionPathExists="' + path + '"\n').encode()
    units = Path.home() / ".config/systemd/user"
    for name in names:
        if name not in TIMERS:
            raise ValueError("unowned operation timer")
        directory = units / (name + ".d")
        if directory.is_symlink():
            raise ValueError("unsafe operation timer guard directory")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = directory / "90-manual-operation.conf"
        if target.is_symlink():
            raise ValueError("unsafe operation timer guard link")
    # Validate every guard before changing the opt-in used by already loaded units.
    for name in names:
        target = units / (name + ".d") / "90-manual-operation.conf"
        atomic_private(target, guard)
    if mode == "manual":
        marker.unlink(missing_ok=True)
    else:
        atomic_private(marker, b"explicit scheduled operation\n")


def apply_timer_mode(root: Path, names: list[str]) -> str:
    """Serialize the final installer decision with changes to persistent user intent."""
    if any(name not in TIMERS for name in names):
        raise ValueError("unowned operation timer")
    with _lock(root):
        mode = operation_mode(root)
        _guards(root, names, mode)
        subprocess.run(
            ["/usr/bin/systemctl", "--user", "daemon-reload"], check=True, capture_output=True
        )  # nosec B603
        action = "disable" if mode == "manual" else "enable"
        subprocess.run(
            ["/usr/bin/systemctl", "--user", action, "--now", *names],
            check=True,
            capture_output=True,
        )  # nosec B603
        return mode


def configure_operation_mode(root: Path, mode: str) -> dict[str, Any]:
    """Manual selection stops only owned timers; scheduled selection requires installation."""
    if platform.system() != "Linux":
        raise ValueError("operation mode is configured on Ubuntu")
    if mode not in ("manual", "scheduled"):
        raise ValueError("unknown operation mode")
    with _lock(root):
        operation_mode(root)
        names = [name for name in TIMERS if (Path.home() / ".config/systemd/user" / name).exists()]
        # Preserve explicit intent even if scheduler control fails and needs retry.
        atomic_private(root / FILENAME, json_bytes({"schema_version": 1, "mode": mode}))
        if mode == "manual":
            _guards(root, names, mode)
            if names:
                subprocess.run(
                    ["/usr/bin/systemctl", "--user", "daemon-reload"],
                    check=True,
                    capture_output=True,
                )  # nosec B603
                subprocess.run(
                    ["/usr/bin/systemctl", "--user", "disable", "--now", *names],
                    check=True,
                    capture_output=True,
                )  # nosec B603
    return {"mode": mode, "schedules_started": False, "services_stopped": False}


_MAC_TRIGGERS = (
    "StartInterval",
    "StartCalendarInterval",
    "KeepAlive",
    "WatchPaths",
    "QueueDirectories",
    "StartOnMount",
    "LaunchEvents",
    "Sockets",
    "MachServices",
)


def mac_agent_schedule(root: Path, definition: dict[str, Any]) -> dict[str, Any]:
    """A fresh or explicitly manual worker is registered for one-shot invocation."""
    mode = operation_mode(root)
    result = {k: v for k, v in definition.items() if k not in _MAC_TRIGGERS}
    result["RunAtLoad"] = mode == "scheduled"
    if mode == "scheduled":
        result["StartInterval"] = 900
    return result


def validate_mac_agent_schedule(root: Path, definition: dict[str, Any]) -> str:
    """A stale periodic pending file cannot override persisted manual intent."""
    mode = operation_mode(root)
    triggers = set(definition).intersection(_MAC_TRIGGERS)
    if mode == "manual":
        if triggers or definition.get("RunAtLoad", False) is not False:
            raise ValueError("manual inference cannot activate a scheduled definition")
    elif (
        triggers != {"StartInterval"}
        or isinstance(definition.get("StartInterval"), bool)
        or definition.get("StartInterval") != 900
        or definition.get("RunAtLoad") is not True
    ):
        raise ValueError("scheduled inference requires the explicit bounded interval")
    return mode
