"""Relocate closed private evidence through a serialized, byte-verified transaction.

The default prepares and verifies a copy. --cutover additionally persists an
original-path bind and retires only independently reverified redundant backing.
All diagnostics and transaction inputs stay in the private evidence directory.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
import shlex
import shutil
import stat

# Fixed programs receive validated path arguments; no shell interprets them.
import subprocess  # nosec B404
import sys
import traceback
import uuid
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any


def private_json(path: Path, value: Any) -> None:
    """Commit private operational evidence without exposing raw diagnostics."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("unsafe private transaction path")
    pending = path.with_name(path.name + ".pending")
    flags = os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_NOFOLLOW
    with os.fdopen(os.open(pending, flags, 0o600), "w") as stream:
        json.dump(value, stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(pending, path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def digest(path: Path) -> str:
    """Hash complete bytes and reject an inode changed during the read."""
    before = path.stat()
    result = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            result.update(block)
    after = path.stat()

    def guards(value: os.stat_result) -> tuple[int, ...]:
        return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)

    if guards(before) != guards(after):
        raise ValueError("evidence changed while hashing")
    return result.hexdigest()


def checked_paths(source: Path, destination: Path, evidence: Path) -> None:
    """Reject overlapping, relative and symlinked transaction namespaces."""
    paths = (source, destination, evidence)
    for path in paths:
        if not path.is_absolute() or path == Path("/"):
            raise ValueError("transaction paths must be absolute non-root paths")
        if any(parent.is_symlink() for parent in (path, *path.parents)):
            raise ValueError("transaction namespaces cannot contain symlinks")
    if not source.is_dir():
        raise ValueError("source evidence directory is missing")
    for index, path in enumerate(paths):
        for other in paths[index + 1 :]:
            if path == other or path in other.parents or other in path.parents:
                raise ValueError("transaction namespaces overlap")


def tree_manifest(root: Path) -> dict[str, dict[str, Any]]:
    """Enumerate and hash every member, including duplicate hardlink paths."""
    result: dict[str, dict[str, Any]] = {}
    groups: dict[tuple[int, int], str] = {}
    for path in [root, *sorted(root.rglob("*"))]:
        info = path.lstat()
        item: dict[str, int | str] = {
            "mode": info.st_mode,
            "uid": info.st_uid,
            "gid": info.st_gid,
            "mtime_ns": info.st_mtime_ns,
        }
        if stat.S_ISREG(info.st_mode):
            item.update(type="file", size=info.st_size, sha256=digest(path))
            group = groups.setdefault((info.st_dev, info.st_ino), path.relative_to(root).as_posix())
            item["link_group"] = group
        elif stat.S_ISDIR(info.st_mode):
            item["type"] = "directory"
        elif stat.S_ISLNK(info.st_mode):
            raise ValueError("evidence member is a symlink")
        else:
            raise ValueError("unsupported evidence member type")
        result[path.relative_to(root).as_posix()] = item
    return result


def verify_tree(root: Path, manifest: dict[str, dict[str, Any]]) -> None:
    """Require exact bytes, ownership, modes, timestamps and namespace coverage."""
    for relative in manifest:
        path = PurePosixPath(relative)
        if path.is_absolute() or ".." in path.parts or path.as_posix() != relative:
            raise ValueError("unsafe evidence manifest member")
    observed = tree_manifest(root)
    if set(observed) != set(manifest):
        raise ValueError("evidence manifest coverage changed")
    if observed != manifest:
        raise ValueError("evidence bytes or metadata changed")


def complete_directory_timestamps(root: Path, manifest: dict[str, dict[str, Any]]) -> None:
    """Finish copy-directory metadata only after every payload guard matches."""
    observed = tree_manifest(root)
    if set(observed) != set(manifest):
        raise ValueError("copy namespace coverage changed")
    for relative, expected in manifest.items():
        current = observed[relative]
        comparable = {
            key: value
            for key, value in current.items()
            if not (expected["type"] == "directory" and key == "mtime_ns")
        }
        required = {
            key: value
            for key, value in expected.items()
            if not (expected["type"] == "directory" and key == "mtime_ns")
        }
        if comparable != required:
            raise ValueError("copy payload, ownership, mode or hardlink guard changed")
    for relative, expected in manifest.items():
        if expected["type"] == "directory":
            path = root / relative
            os.utime(path, ns=(path.stat().st_atime_ns, expected["mtime_ns"]))


def allocation_estimate(root: Path) -> dict[str, int]:
    """Count selected hardlinks before estimating exclusive inode allocation.

    Compressed filesystems can report logical st_blocks. Actual recovered
    capacity is therefore recorded separately with filesystem free-space checks.
    """
    groups = {}
    counts: Counter[tuple[int, int]] = Counter()
    for path in root.rglob("*"):
        info = path.lstat()
        if stat.S_ISREG(info.st_mode):
            key = (info.st_dev, info.st_ino)
            groups[key] = (info.st_blocks * 512, info.st_nlink)
            counts[key] += 1
    return {
        "selected_inode_bytes": sum(value[0] for value in groups.values()),
        "exclusive_inode_bytes": sum(
            value[0] for key, value in groups.items() if counts[key] == value[1]
        ),
    }


@contextmanager
def relocation_lock(coordinator: Path) -> Iterator[None]:
    """Serialize all transactions sharing the same storage coordinator."""
    if not coordinator.is_absolute() or any(
        p.is_symlink() for p in (coordinator, *coordinator.parents)
    ):
        raise ValueError("unsafe relocation coordinator")
    coordinator.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = coordinator / "global-relocation.lock"
    with os.fdopen(os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), "a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def scan_handles(
    root: Path, proc: Path = Path("/proc")
) -> dict[str, list[dict[str, Any]] | list[str]]:
    """Inspect actual descriptors and writable mappings; unknown access fails closed."""
    handles, denied = [], []
    prefix = str(root) + "/"
    for process in proc.iterdir():
        if not process.name.isdigit():
            continue
        try:
            for descriptor in (process / "fd").iterdir():
                try:
                    target = os.readlink(descriptor)
                    if target != str(root) and not target.startswith(prefix):
                        continue
                    lines = (process / "fdinfo" / descriptor.name).read_text().splitlines()
                    flags = int(
                        next(line.split()[1] for line in lines if line.startswith("flags:")), 8
                    )
                    handles.append(
                        {
                            "pid": int(process.name),
                            "fd": descriptor.name,
                            "target": target,
                            "writable": bool(flags & 3),
                        }
                    )
                except (FileNotFoundError, ProcessLookupError):
                    continue
                except (PermissionError, ValueError, StopIteration, IndexError):
                    denied.append(str(descriptor))
            for line in (process / "maps").read_text().splitlines():
                fields = line.split(None, 5)
                if (
                    len(fields) == 6
                    and "w" in fields[1]
                    and (fields[5] == str(root) or fields[5].startswith(prefix))
                ):
                    handles.append(
                        {"pid": int(process.name), "target": fields[5], "writable": True}
                    )
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError:
            denied.append(str(process))
    return {"handles": handles, "denied": denied}


def checked_command(evidence: Path, args: list[str], *, stdin: str | None = None) -> str:
    """Keep subprocess output private; command arguments never enter a shell."""
    result = subprocess.run(args, input=stdin, capture_output=True, text=True, check=False)  # nosec B603
    with (evidence / "operations.private.log").open("a", encoding="utf-8") as log:
        log.write(result.stdout + result.stderr)
    if result.returncode:
        raise subprocess.CalledProcessError(result.returncode, args)
    return result.stdout


def require_closed(source: Path, evidence: Path) -> None:
    """A privileged read-only scan rejects writers without signaling any worker."""
    args = [
        "/usr/bin/sudo",
        "-n",
        sys.executable,
        str(Path(__file__).resolve()),
        "--scan-source",
        str(source),
    ]
    observed = json.loads(checked_command(evidence, args))
    private_json(evidence / "handle-scan.private.json", observed)
    if observed["denied"] or any(item["writable"] for item in observed["handles"]):
        raise ValueError("source writers or unverified process access remain")


def is_mountpoint(path: Path) -> bool:
    """Distinguish a non-mountpoint from unknown command or access failures."""
    result = subprocess.run(  # nosec B603
        ["/usr/bin/mountpoint", "-q", "--", str(path)], capture_output=True, check=False
    )
    if result.returncode not in {0, 32}:
        raise ValueError("mountpoint state could not be verified")
    return result.returncode == 0


def retire_backend(
    backend: Path, manifest: dict[str, dict[str, Any]], evidence: Path, *, started: bool = False
) -> None:
    """Resume a previously verified retirement while checking every surviving file."""
    marker = evidence / "retirement-started.private.json"
    if not started:
        verify_tree(backend, manifest)
        private_json(marker, {"manifest_sha256": digest(evidence / "manifest.private.json")})
    elif not marker.is_file():
        raise ValueError("retirement has no independently verified start receipt")
    if json.loads(marker.read_bytes())["manifest_sha256"] != digest(
        evidence / "manifest.private.json"
    ):
        raise ValueError("retirement manifest changed")
    remaining = {".", *(path.relative_to(backend).as_posix() for path in backend.rglob("*"))}
    if not remaining <= set(manifest):
        raise ValueError("unexpected member during backend retirement")
    for relative in sorted(
        remaining, key=lambda value: (len(PurePosixPath(value).parts), value), reverse=True
    ):
        if relative == ".":
            continue
        path, expected = backend / relative, manifest[relative]
        info = path.lstat()
        if (info.st_mode, info.st_uid, info.st_gid) != (
            expected["mode"],
            expected["uid"],
            expected["gid"],
        ):
            raise ValueError("backend retirement metadata changed")
        if expected["type"] == "file":
            if (
                info.st_size != expected["size"]
                or info.st_mtime_ns != expected["mtime_ns"]
                or digest(path) != expected["sha256"]
            ):
                raise ValueError("backend retirement bytes changed")
            path.unlink()
        else:
            path.rmdir()
    private_json(evidence / "retirement-complete.private.json", {"complete": True})


def fstab_escape(value: str) -> str:
    """Escape fstab fields without shell interpolation or ambiguous controls."""
    if any(ord(character) < 32 for character in value) or "," in value:
        raise ValueError("unsupported mount field")
    return value.replace("\\", "\\134").replace(" ", "\\040")


def persist_mount(
    source: Path,
    destination: Path,
    evidence: Path,
    *,
    fstab_path: Path = Path("/etc/fstab"),
    unit_directory: Path | None = None,
) -> None:
    """Serialize an exact fstab update and a fail-closed processor prerequisite."""
    line = f"{fstab_escape(str(destination))} {fstab_escape(str(source))} none bind,nofail,x-systemd.requires-mounts-for={fstab_escape(str(destination))} 0 0\n"
    current = checked_command(evidence, ["/usr/bin/sudo", "-n", "cat", str(fstab_path)])
    matching = [
        row
        for row in current.splitlines()
        if row.split()
        and not row.lstrip().startswith("#")
        and len(row.split()) > 1
        and row.split()[1] == fstab_escape(str(source))
    ]
    if matching and matching != [line.strip()]:
        raise ValueError("conflicting persistent original-path mount")
    if not matching:
        backup = evidence / "fstab-before.private.json"
        if not backup.exists():
            private_json(backup, {"bytes_base64": base64.b64encode(current.encode()).decode()})
        writer = """import pathlib,sys,os,hashlib,base64
p=pathlib.Path(sys.argv[3]);assert p.is_absolute() and not p.is_symlink();old=p.read_bytes();assert hashlib.sha256(old).hexdigest()==sys.argv[1]
s=p.stat();t=p.with_name('fstab.closed-evidence.pending')
desired=old+base64.b64decode(sys.argv[2],validate=True);assert not t.is_symlink()
if t.exists():assert t.read_bytes()==desired
else:
 with t.open('xb') as f:f.write(desired);f.flush();os.fsync(f.fileno())
os.chmod(t,s.st_mode);os.chown(t,s.st_uid,s.st_gid);os.replace(t,p)
fd=os.open(p.parent,os.O_RDONLY|os.O_DIRECTORY);os.fsync(fd);os.close(fd)
"""
        checked_command(
            evidence,
            [
                "/usr/bin/sudo",
                "-n",
                "/usr/bin/python3",
                "-c",
                writer,
                hashlib.sha256(current.encode()).hexdigest(),
                base64.b64encode(line.encode()).decode(),
                str(fstab_path),
            ],
        )
    units = unit_directory or Path.home() / ".config/systemd/user"
    directory = units / "html-to-markdown-processing.service.d"
    filename = "closed-evidence-" + hashlib.sha256(str(source).encode()).hexdigest()[:16] + ".conf"
    content = "[Service]\nExecStartPre=/usr/bin/mountpoint -q " + shlex.quote(str(source)) + "\n"
    # Keep root's reserved capacity usable for this owned recovery prerequisite.
    writer = """import pathlib,sys,os
p=pathlib.Path(sys.argv[1]);d=p.parent;owner=d.parent.stat();assert not p.is_symlink() and not d.is_symlink();d.mkdir(mode=0o700,exist_ok=True);os.chown(d,owner.st_uid,owner.st_gid)
if p.exists():assert p.read_text()==sys.argv[2]
else:
 with p.open('x') as f:f.write(sys.argv[2]);f.flush();os.fsync(f.fileno())
 os.chmod(p,0o600);os.chown(p,owner.st_uid,owner.st_gid)
"""
    checked_command(
        evidence,
        [
            "/usr/bin/sudo",
            "-n",
            "/usr/bin/python3",
            "-c",
            writer,
            str(directory / filename),
            content,
        ],
    )
    if fstab_path == Path("/etc/fstab") and unit_directory is None:
        checked_command(evidence, ["/usr/bin/sudo", "-n", "systemctl", "daemon-reload"])
        checked_command(evidence, ["systemctl", "--user", "daemon-reload"])


def cutover(
    source: Path, destination: Path, evidence: Path, manifest: dict[str, dict[str, Any]]
) -> None:
    """Bind verified bytes atomically; keep them available after any later failure."""
    marker = evidence / "bound.private.json"
    intent_path = evidence / "cutover-intent.private.json"
    parent = evidence / "original-parent"
    backend = parent / source.name
    if not marker.exists():
        mounted = is_mountpoint(source)
        if mounted and not intent_path.exists():
            raise ValueError("source is already an unrelated mountpoint")
        if not mounted and source.stat().st_dev == destination.stat().st_dev:
            raise ValueError("cutover requires a different destination filesystem")
        verify_tree(source, manifest)
        verify_tree(destination, manifest)
        if not intent_path.exists():
            original = source.stat()
            private_json(
                intent_path,
                {
                    "source_device": original.st_dev,
                    "source_inode": original.st_ino,
                    "manifest_sha256": digest(evidence / "manifest.private.json"),
                },
            )
        intent = json.loads(intent_path.read_bytes())
        if intent["manifest_sha256"] != digest(evidence / "manifest.private.json"):
            raise ValueError("cutover intent manifest changed")
        require_closed(source, evidence)
        parent.mkdir(mode=0o700, exist_ok=True)
        if not is_mountpoint(parent):
            checked_command(
                evidence,
                ["/usr/bin/sudo", "-n", "mount", "--bind", str(source.parent), str(parent)],
            )
        checked_command(evidence, ["/usr/bin/sudo", "-n", "mount", "--make-private", str(parent)])
        original = backend.stat()
        if (original.st_dev, original.st_ino) != (intent["source_device"], intent["source_inode"]):
            raise ValueError("original backend view is unverified")
        if not mounted:
            checked_command(
                evidence, ["/usr/bin/sudo", "-n", "mount", "--bind", str(destination), str(source)]
            )
        if (source.stat().st_dev, source.stat().st_ino) != (
            destination.stat().st_dev,
            destination.stat().st_ino,
        ):
            raise ValueError("original path does not select the exact verified copy")
        private_json(
            marker,
            {
                "backend": str(backend),
                "parent_view": str(parent),
                "manifest_sha256": digest(evidence / "manifest.private.json"),
            },
        )
    bound = json.loads(marker.read_bytes())
    if bound != {
        "backend": str(backend),
        "parent_view": str(parent),
        "manifest_sha256": digest(evidence / "manifest.private.json"),
    }:
        raise ValueError("bound transaction manifest changed")
    verify_tree(source, manifest)
    verify_tree(destination, manifest)
    persist_mount(source, destination, evidence)
    if not (evidence / "retirement-complete.private.json").exists():
        if (
            backend.stat().st_dev == source.stat().st_dev
            or source.stat().st_dev != destination.stat().st_dev
        ):
            raise ValueError("backend retirement filesystem is unverified")
        require_closed(backend, evidence)
        require_closed(source, evidence)
        retire_backend(
            backend,
            manifest,
            evidence,
            started=(evidence / "retirement-started.private.json").exists(),
        )
    parent = Path(bound["parent_view"])
    if is_mountpoint(parent):
        checked_command(evidence, ["/usr/bin/sudo", "-n", "umount", str(parent)])
    verify_tree(source, manifest)
    private_json(
        evidence / "complete.private.json",
        {
            "complete": True,
            "manifest_sha256": digest(evidence / "manifest.private.json"),
            "workers_signaled": False,
        },
    )


def relocate(
    source: Path, destination: Path, evidence: Path, coordinator: Path, *, apply: bool = False
) -> dict[str, Any]:
    """Resume exact copy/checkpoint inputs under one global transaction lock."""
    checked_paths(source, destination, evidence)
    if (
        coordinator in (source, destination)
        or source in coordinator.parents
        or destination in coordinator.parents
    ):
        raise ValueError("coordinator must be outside relocated namespaces")
    with relocation_lock(coordinator):
        evidence.mkdir(mode=0o700, parents=True, exist_ok=True)
        if evidence.stat().st_mode & 0o077:
            raise ValueError("transaction evidence directory is not private")
        launch = {
            "source": str(source),
            "destination": str(destination),
            "coordinator": str(coordinator),
            "tool_sha256": digest(Path(__file__)),
        }
        claim = evidence / "launch.private.json"
        if claim.exists():
            if json.loads(claim.read_bytes()) != launch:
                raise ValueError("transaction inputs or tool changed")
        else:
            if destination.exists():
                raise ValueError("unowned relocation destination already exists")
            private_json(claim, launch)
        free_before = shutil.disk_usage(source).free
        manifest_path = evidence / "manifest.private.json"
        if (evidence / "cutover-intent.private.json").exists() and not apply:
            raise ValueError("pending cutover requires explicit cutover resume")
        if (
            not (evidence / "bound.private.json").exists()
            and not (evidence / "cutover-intent.private.json").exists()
        ):
            require_closed(source, evidence)
            if not manifest_path.exists():
                private_json(manifest_path, tree_manifest(source))
                private_json(evidence / "allocation.private.json", allocation_estimate(source))
            manifest = json.loads(manifest_path.read_bytes())
            verify_tree(source, manifest)
            size = sum(
                item.get("size", 0)
                for relative, item in manifest.items()
                if item.get("link_group") == relative
            )
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if (
                not destination.exists()
                and shutil.disk_usage(destination.parent).free <= size + 64 * 1024**2
            ):
                raise ValueError("destination capacity is insufficient for the complete copy")
            checked_command(
                evidence,
                [
                    "/usr/bin/rsync",
                    "-aH",
                    "--numeric-ids",
                    "--partial",
                    "--",
                    str(source) + "/",
                    str(destination) + "/",
                ],
            )
            complete_directory_timestamps(destination, manifest)
            verify_tree(source, manifest)
            verify_tree(destination, manifest)
            private_json(
                evidence / "copy-verified.private.json",
                {"manifest_sha256": digest(manifest_path), "complete": True},
            )
        manifest = json.loads(manifest_path.read_bytes())
        if apply:
            cutover(source, destination, evidence, manifest)
        return {
            "state": "relocated" if apply else "copy_verified",
            "entries": len(manifest),
            "files": sum(item["type"] == "file" for item in manifest.values()),
            "logical_bytes": sum(item.get("size", 0) for item in manifest.values()),
            "source_filesystem_free_before": free_before,
            "source_filesystem_free_after": shutil.disk_usage(source.parent).free,
            "workers_signaled": False,
            "publication_approved": False,
        }


def main() -> int:
    """Emit aggregate results; preserve detailed failures only in private evidence."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--coordination-root", type=Path)
    parser.add_argument("--cutover", action="store_true")
    parser.add_argument("--scan-source", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.scan_source is not None:
        print(json.dumps(scan_handles(args.scan_source), sort_keys=True))
        return 0
    if sys.platform != "linux" or not all(
        (args.source, args.destination, args.evidence, args.coordination_root)
    ):
        parser.error("Linux and all four transaction paths are required")
    try:
        result = relocate(
            args.source, args.destination, args.evidence, args.coordination_root, apply=args.cutover
        )
    except (ValueError, OSError, subprocess.CalledProcessError) as failure:
        claim = args.evidence / "launch.private.json"
        safe = (
            args.evidence.is_dir()
            and not any(p.is_symlink() for p in (args.evidence, *args.evidence.parents))
            and not args.evidence.stat().st_mode & 0o077
        )
        if safe and claim.is_file():
            try:
                owned = json.loads(claim.read_bytes()).get("source") == str(args.source)
            except (ValueError, OSError, AttributeError):
                owned = False
            if owned:
                name = "failure-" + uuid.uuid4().hex + ".private.json"
                private_json(
                    args.evidence / name,
                    {"error_class": type(failure).__name__, "traceback": traceback.format_exc()},
                )
        print(
            json.dumps(
                {
                    "state": "failed",
                    "error_class": type(failure).__name__,
                    "publication_approved": False,
                }
            )
        )
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
