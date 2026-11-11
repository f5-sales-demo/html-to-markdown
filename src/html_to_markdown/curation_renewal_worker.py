"""Advance queued source curation independently of capture and publication."""

from __future__ import annotations

import fcntl
import json
import platform
import shutil
import subprocess  # nosec B404
import tempfile
from pathlib import Path
from typing import Any, BinaryIO

from .content_inventory import atomic_private
from .curation import json_bytes
from .curation_renewal_delivery import submit_policy
from .curation_renewal_inputs import prepare_renewals
from .curation_renewal_jobs import review_batch
from .curation_renewal_publication import stage_policy, validate_planner
from .enrichment.analysis import sha
from .enrichment.codex_client import CodexClient
from .publication_queue import deployed_revision
from .refresh import _verified_baseline
from .work_queue import pack_private_tree, unpack_queue


# The original admission remains visible while independent catalog jobs progress.
# pylint: disable-next=too-many-locals,too-many-arguments
def advance_renewals(
    capture: Path,
    review: Path,
    output: Path,
    journal: Path,
    transport: Path,
    host: str,
    remote_executable: str,
    remote_queue: str,
) -> dict[str, Any]:
    inventory = prepare_renewals(capture, review, output)
    if not inventory["documents"]:
        return {"state": "no_curation_exceptions"}
    report = review_batch(output, inventory, "all", journal)
    approved = {
        d["identity"] for d in report["documents"] if d["disposition"] == "independently_validated"
    }
    contexts = [
        json.loads((output / d["context_path"]).read_bytes())
        for d in inventory["documents"]
        if d["identity"] in approved
    ]
    if not contexts:
        return {"state": "queued", "exceptions": report["exceptions"]}
    revision = deployed_revision()
    pin = revision["publication_sha256"]
    baseline = output / "baseline" / pin
    if not baseline.exists():
        baseline.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        gh = shutil.which("gh")
        if gh is None:
            raise ValueError("existing GitHub CLI is unavailable for verified baseline capture")
        with tempfile.TemporaryDirectory(prefix="baseline-", dir=baseline.parent) as temporary:
            subprocess.run(
                [
                    gh,
                    "release",
                    "download",
                    revision["snapshot_tag"],
                    "--repo",
                    "f5-sales-demo/html-to-markdown",
                    "--dir",
                    temporary,
                ],
                capture_output=True,
                check=True,
            )  # nosec B603
            _verified_baseline(Path(temporary), pin)
            Path(temporary).rename(baseline)
    _verified_baseline(baseline, pin)
    client = CodexClient(journal, concurrency=4)
    try:
        qualification = validate_planner(output, contexts, baseline, pin, client)
    finally:
        client.close()
    package = {
        "contexts": [context["binding_sha256"] for context in contexts],
        "baseline": pin,
        "qualification_sha256": qualification["qualification_sha256"],
    }
    package_bytes = json_bytes(package)
    atomic_private(output / "renewal-package.json", package_bytes)
    with tempfile.TemporaryDirectory(prefix="private-renewal-transfer-") as temporary:
        archive = Path(temporary) / "renewals.tar.gz"
        with archive.open("wb") as stream:
            pack_private_tree(output, stream)
        with archive.open("rb") as stream:
            result = subprocess.run(
                [
                    str(transport),
                    "--host",
                    host,
                    "--stdin",
                    "--",
                    remote_executable,
                    "receive-curation-renewal",
                    "--queue-root",
                    remote_queue,
                    "--package-sha256",
                    sha(package_bytes),
                ],
                stdin=stream,
                capture_output=True,
                check=True,
                timeout=1800,
            )  # nosec B603
    delivery = json.loads(result.stdout)
    atomic_private(output / "delivery.json", json_bytes(delivery))
    return {
        "state": "policy_pr_pending",
        "independently_validated": len(contexts),
        "exceptions": report["exceptions"],
        "delivery": delivery,
    }


def receive_renewals(
    queue: Path, digest: str, stream: BinaryIO, repository: Path
) -> dict[str, Any]:
    """Ubuntu replays preserved private evidence before producing public catalogs."""
    if platform.system() != "Linux":
        raise ValueError("received catalog delivery is routed to Ubuntu")
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("unsafe curation package digest")
    root = queue / ".curation" / digest
    if root.is_symlink() or root.parent.is_symlink():
        raise ValueError("unsafe received curation directory")
    root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="received-renewals-", dir=root.parent) as temporary:
        archive = Path(temporary) / "input.tar.gz"
        with archive.open("wb") as target:
            shutil.copyfileobj(stream, target)
        candidate = Path(temporary) / "evidence"
        unpack_queue(archive, candidate)
        if sha((candidate / "renewal-package.json").read_bytes()) != digest:
            raise ValueError("curation package digest mismatch")
        if not root.exists():
            candidate.rename(root)
    package = json.loads((root / "renewal-package.json").read_bytes())
    contexts = []
    for pin in package["contexts"]:
        if len(pin) != 64 or any(c not in "0123456789abcdef" for c in pin):
            raise ValueError("unsafe received curation context identity")
        contexts.append(json.loads((root / pin / "context.json").read_bytes()))
    pin = package["baseline"]
    if len(pin) != 64 or any(c not in "0123456789abcdef" for c in pin):
        raise ValueError("unsafe received curation baseline identity")
    (queue / ".publication").mkdir(mode=0o700, parents=True, exist_ok=True)
    with (queue / ".publication/coordinator.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        receipt = stage_policy(root, contexts, root / "baseline" / pin, pin, root / "staged")
        if receipt["qualification_sha256"] != package["qualification_sha256"]:
            raise ValueError("received actual-planner qualification changed")
        return submit_policy(repository, root / "staged", root / "delivery")
