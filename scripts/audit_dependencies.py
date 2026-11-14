"""Audit every active locked third-party dependency and require complete evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


def active_requirements(text: str) -> dict[str, str]:
    """Read exact pins, applying the current interpreter's platform markers."""
    result: dict[str, str] = {}
    for line in text.replace("\\\n", " ").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split(" --hash=")
        if any(not re.fullmatch(r"sha256:[0-9a-f]{64}", part.strip()) for part in parts[1:]):
            raise ValueError("Invalid dependency distribution hash")
        requirement = Requirement(parts[0])
        pins = list(requirement.specifier)
        if requirement.url or len(pins) != 1 or pins[0].operator != "==" or "*" in pins[0].version:
            raise ValueError("Dependency audit requires exact registry version pins")
        if requirement.marker and not requirement.marker.evaluate():
            continue
        name = canonicalize_name(requirement.name)
        if name in result:
            raise ValueError("Duplicate active dependency pin: " + name)
        result[name] = pins[0].version
    if not result:
        raise ValueError("Dependency audit has no active locked dependencies")
    return result


def verify_report(report: Any, expected: dict[str, str]) -> None:
    """Skipped, missing, unexpected, duplicate and vulnerable entries all fail."""
    if not isinstance(report, dict) or not isinstance(report.get("dependencies"), list):
        raise ValueError("Invalid dependency audit report")
    actual: dict[str, str] = {}
    vulnerable = []
    for entry in report["dependencies"]:
        if not isinstance(entry, dict) or "skip_reason" in entry:
            raise ValueError("Dependency audit contains an unaudited dependency")
        if not isinstance(entry.get("name"), str) or not isinstance(entry.get("version"), str):
            raise ValueError("Dependency audit is missing dependency identity")
        name = canonicalize_name(entry["name"])
        if name in actual:
            raise ValueError("Dependency audit contains duplicate results: " + name)
        actual[name] = entry["version"]
        if not isinstance(entry.get("vulns"), list):
            raise ValueError("Dependency audit is missing vulnerability evidence")
        if entry["vulns"]:
            vulnerable.append(name)
    if actual != expected:
        raise ValueError("Dependency audit coverage differs from the active locked pins")
    if vulnerable:
        raise ValueError("Known vulnerabilities in: " + ", ".join(sorted(vulnerable)))


def main() -> int:
    """Export immutable dependency pins and require a complete vulnerability audit."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Preserve the dependency audit receipt")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    try:
        export = subprocess.run(
            [
                "uv",
                "export",
                "--all-groups",
                "--locked",
                "--format",
                "requirements-txt",
                "--no-emit-project",
                "--no-header",
                "--no-annotate",
            ],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
        expected = active_requirements(export.stdout)
        with tempfile.TemporaryDirectory(prefix="locked-dependency-audit-") as directory:
            requirements = Path(directory) / "requirements.txt"
            report_path = Path(directory) / "report.json"
            requirements.write_text(export.stdout)
            audit = subprocess.run(
                [
                    "uv",
                    "run",
                    "--locked",
                    "pip-audit",
                    "--requirement",
                    str(requirements),
                    "--require-hashes",
                    "--disable-pip",
                    "--strict",
                    "--format",
                    "json",
                    "--progress-spinner",
                    "off",
                    "--output",
                    str(report_path),
                ],
                cwd=root,
                check=False,
            )
            report = json.loads(report_path.read_bytes())
        receipt = {
            "schema_version": 1,
            "lock_sha256": hashlib.sha256((root / "uv.lock").read_bytes()).hexdigest(),
            "requirements_sha256": hashlib.sha256(export.stdout.encode()).hexdigest(),
            "python_version": sys.version.split()[0],
            "platform": sys.platform,
            "active_locked_dependencies": expected,
            "audit": report,
            "audit_exit_code": audit.returncode,
        }
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n")
        verify_report(report, expected)
        if audit.returncode:
            raise ValueError("Dependency audit command failed")
        print(
            f"Audited {len(expected)} locked dependencies; "
            "no known vulnerabilities or skipped dependencies."
        )
        return 0
    except (ValueError, OSError, subprocess.CalledProcessError) as failure:
        print("Dependency audit failed: " + str(failure), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
