"""Share immutable verified release assets without sharing mutable review checkpoints."""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import subprocess  # nosec B404
import tempfile
from pathlib import Path

from .content_inventory import atomic_private
from .curation import json_bytes
from .models import utc_now
from .package import RELEASE_ASSET_NAMES
from .refresh import _verified_baseline

ASSETS = (*RELEASE_ASSET_NAMES, "publication.json")


def _safe_directory(directory: Path) -> None:
    if any(path.is_symlink() for path in (directory, *directory.parents)):
        raise ValueError("verified baseline paths cannot contain symlinks")


def _verify(directory: Path, pin: str, tag: str, *, closed: bool = False) -> None:
    _safe_directory(directory)
    if any((directory / name).is_symlink() for name in ASSETS):
        raise ValueError("verified baseline assets cannot be symlinks")
    if closed and {path.name for path in directory.iterdir()} != set(ASSETS):
        raise ValueError("verified baseline cache has an unexpected asset set")
    _verified_baseline(directory, pin)
    receipt = json.loads((directory / "publication.json").read_bytes())
    if receipt["release_tag"] != tag:
        raise ValueError("verified baseline snapshot tag changed")


def _link_file(source: Path, destination: Path) -> None:
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


def _snapshot(source: Path, destination: Path, pin: str, tag: str) -> None:
    """Only a complete verified six-file snapshot receives its final identity."""
    _safe_directory(destination)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    scratch = Path(tempfile.mkdtemp(prefix=".baseline-snapshot-", dir=destination.parent))
    # Failed copies remain private for diagnosis and never acquire the final name.
    for name in ASSETS:
        _link_file(source / name, scratch / name)
    _verify(scratch, pin, tag, closed=True)
    scratch.rename(destination)


def _download(root: Path, destination: Path, pin: str, tag: str) -> None:
    gh = shutil.which("gh")
    if gh is None:
        raise ValueError("existing GitHub CLI is unavailable for verified baseline capture")
    attempts = root / "attempts"
    _safe_directory(attempts)
    attempts.mkdir(mode=0o700, exist_ok=True)
    attempt = Path(tempfile.mkdtemp(prefix=pin + "-", dir=attempts))
    downloads = attempt / "downloads"
    downloads.mkdir(mode=0o700)
    status = {"publication_sha256": pin, "snapshot_tag": tag, "started_at": utc_now()}
    atomic_private(attempt / "status.json", json_bytes({**status, "state": "downloading"}))
    try:
        with (
            (attempt / "stdout.private.log").open("wb") as stdout,
            (attempt / "stderr.private.log").open("wb") as stderr,
        ):
            subprocess.run(
                [
                    gh,
                    "release",
                    "download",
                    tag,
                    "--repo",
                    "f5-sales-demo/html-to-markdown",
                    "--dir",
                    str(downloads),
                ],
                stdout=stdout,
                stderr=stderr,
                check=True,
            )  # nosec B603
        _verify(downloads, pin, tag, closed=True)
        downloads.rename(destination)
    except (OSError, ValueError, subprocess.CalledProcessError) as failure:
        atomic_private(
            attempt / "status.json",
            json_bytes(
                {
                    **status,
                    "state": "failed",
                    "error_class": type(failure).__name__,
                    "ended_at": utc_now(),
                }
            ),
        )
        raise
    atomic_private(
        attempt / "status.json", json_bytes({**status, "state": "verified", "ended_at": utc_now()})
    )


def _share_existing(source: Path, directory: Path) -> None:
    """Replace already-verified uploaded copies with the same immutable cache bytes."""
    for name in ASSETS:
        original, destination = source / name, directory / name
        before, after = original.stat(), destination.stat()
        if before.st_dev != after.st_dev or before.st_ino == after.st_ino:
            continue
        with tempfile.TemporaryDirectory(prefix=".baseline-link-", dir=directory.parent) as tmp:
            linked = Path(tmp) / name
            try:
                os.link(original, linked)
            except OSError:
                # Existing verified bytes remain useful when physical sharing is unsupported.
                continue
            os.replace(linked, destination)


def baseline_snapshot(
    directory: Path, pin: str, tag: str, *, cache_root: Path | None = None
) -> Path:
    """Use one receipt-verified download and retain each job's complete input layout."""
    if re.fullmatch(r"[0-9a-f]{64}", pin) is None:
        raise ValueError("invalid verified baseline receipt digest")
    if re.fullmatch(r"content-[0-9]{8}T[0-9]{6}Z", tag) is None:
        raise ValueError("invalid verified baseline snapshot tag")
    root = cache_root or Path(
        os.environ.get(
            "HTML_TO_MARKDOWN_BASELINE_CACHE",
            str(Path.home() / ".local/share/html-to-markdown/baseline-cache"),
        )
    )
    _safe_directory(root)
    _safe_directory(directory)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = root / (pin + ".lock")
    if lock_path.is_symlink():
        raise ValueError("verified baseline cache lock cannot be a symlink")
    cached = root / pin
    _safe_directory(cached)
    if directory == cached:
        raise ValueError("job baseline must be separate from the shared cache identity")
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if directory.exists():
            _verify(directory, pin, tag)
        if not cached.exists():
            if directory.exists():
                _snapshot(directory, cached, pin, tag)
            else:
                _download(root, cached, pin, tag)
        _verify(cached, pin, tag, closed=True)
        if not directory.exists():
            _snapshot(cached, directory, pin, tag)
        else:
            _share_existing(cached, directory)
        _verify(directory, pin, tag)
    return directory
