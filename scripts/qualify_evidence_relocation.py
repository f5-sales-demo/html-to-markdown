"""Exercise relocation and interrupted cutovers on an isolated Linux filesystem.

Only the newly created workspace and its loop filesystem are mutated. Persistent
mount and processor prerequisite records use isolated configuration fixtures.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import runpy

# Exact arguments target the newly created isolated filesystem fixture.
import subprocess  # nosec B404
from pathlib import Path
from typing import Any
from unittest.mock import patch


def command(*args: str) -> None:
    """Use exact arguments for the explicitly owned filesystem fixture."""
    subprocess.run(args, check=True, capture_output=True)  # nosec B603


def run_case(
    module: dict[str, Any], workspace: Path, volume: Path, number: int, interruption: str | None
) -> dict[str, object]:
    """Check one isolated transaction with its own interruption state."""
    globals_ = module["cutover"].__globals__
    private = module["private_json"]
    source = workspace / f"source-{number}"
    source.mkdir(mode=0o700)
    payload = bytes(range(256)) * 4096
    first = source / "first.bin"
    first.write_bytes(payload)
    first.chmod(0o600)
    os.utime(first, ns=(1700000000000000001, 1700000000000000003))
    os.link(first, source / "second.bin")
    (source / "empty").mkdir(mode=0o700)
    snapshot = module["tree_manifest"](source)
    destination = volume / f"case-{number}"
    evidence = workspace / f"evidence-{number}"
    interrupted = False
    original_unlink = Path.unlink

    def fail_bound(path: Path, value: Any) -> None:
        nonlocal interrupted
        if interruption == "bound" and path.name == "bound.private.json" and not interrupted:
            interrupted = True
            raise OSError("simulated interruption after successful original-path bind")
        private(path, value)

    def fail_retirement(path: Path, missing_ok: bool = False) -> None:
        nonlocal interrupted
        if (
            interruption == "retirement"
            and path.name == "first.bin"
            and "original-parent" in path.parts
            and not interrupted
        ):
            original_unlink(path, missing_ok=missing_ok)
            interrupted = True
            raise OSError("simulated interruption after retiring one verified redundant member")
        return original_unlink(path, missing_ok=missing_ok)

    with first.open("rb") as reader:
        globals_["private_json"] = fail_bound
        try:
            with patch.object(Path, "unlink", fail_retirement):
                try:
                    module["relocate"](
                        source, destination, evidence, workspace / "coordinator", apply=True
                    )
                except OSError:
                    if not interrupted:
                        raise
        finally:
            globals_["private_json"] = private
        if interruption is not None:
            if not interrupted:
                raise ValueError("expected fixture interruption was not reached")
            module["relocate"](source, destination, evidence, workspace / "coordinator", apply=True)
        module["verify_tree"](source, snapshot)
        module["verify_tree"](destination, snapshot)
        if reader.read() != payload:
            raise ValueError("an original open reader lost its complete bytes")
    command("sudo", "-n", "umount", str(source))
    return {
        "interruption": interruption,
        "complete_bytes_metadata_and_hardlinks_verified": True,
        "original_read_handle_preserved": True,
        "resume_verified": interrupted,
    }


def qualify(workspace: Path) -> dict[str, object]:
    """Verify actual bind behavior, interrupted commit recovery and live readers."""
    if not workspace.is_absolute() or workspace.exists():
        raise ValueError("qualification requires a new absolute workspace")
    workspace.mkdir(mode=0o700, parents=True)
    script = Path(__file__).with_name("relocate_closed_evidence.py")
    module = runpy.run_path(str(script))
    globals_ = module["cutover"].__globals__
    persist, private = module["persist_mount"], module["private_json"]
    fstab = workspace / "fstab.fixture"
    fstab.write_text("# isolated relocation fixture\n")
    fstab.chmod(0o600)
    units = workspace / "units.fixture"
    units.mkdir(mode=0o700)

    def isolated_mount(source: Path, destination: Path, evidence: Path) -> None:
        persist(source, destination, evidence, fstab_path=fstab, unit_directory=units)

    globals_["persist_mount"] = isolated_mount
    image = workspace / "filesystem.btrfs"
    command("fallocate", "-l", "1G", str(image))
    image.chmod(0o600)
    command("/usr/sbin/mkfs.btrfs", "-f", str(image))
    volume = workspace / "volume"
    volume.mkdir(mode=0o700)
    command("sudo", "-n", "mount", "-o", "loop,compress-force=zstd:3", str(image), str(volume))
    command("sudo", "-n", "chown", f"{os.getuid()}:{os.getgid()}", str(volume))
    volume.chmod(0o700)
    cases = [
        run_case(module, workspace, volume, number, interruption)
        for number, interruption in enumerate((None, "bound", "retirement"))
    ]
    command("sudo", "-n", "umount", str(volume))
    receipt = {
        "state": "isolated_filesystem_qualification_passed",
        "cases": cases,
        "tool_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
        "production_fstab_or_units_modified": False,
        "workers_signaled": False,
        "inference_requests": 0,
    }
    private(workspace / "qualification.private.json", receipt)
    return receipt


def main() -> None:
    """Retain the complete fixture and emit only its aggregate acceptance receipt."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    print(json.dumps(qualify(args.workspace), sort_keys=True))


if __name__ == "__main__":
    main()
