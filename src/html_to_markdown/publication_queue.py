"""Private Ubuntu assembly and safe draft promotion under the snapshot workflow lock."""

from __future__ import annotations

import fcntl
import json
import platform
import shutil
import subprocess  # nosec B404
import tempfile
from pathlib import Path
from typing import Any

import httpx

from . import __version__
from .combined_sources import combine_sources
from .content_inventory import atomic_private
from .curation import corpus_digest, json_bytes
from .enrichment.analysis import sha
from .enrichment.engine import load_artifact, prepare, verify_enrichment
from .models import utc_now
from .package import RELEASE_ASSET_NAMES, verify_publication_receipt, write_publication_receipt
from .refresh import _verified_baseline
from .source_inputs import verify_source


def deployed_revision() -> dict[str, Any]:
    response = httpx.get(
        "https://f5-sales-demo.github.io/html-to-markdown/api/revision.json", timeout=30
    )
    response.raise_for_status()
    return dict(response.json())


def prepare_publication(queue_root: Path, output: Path) -> dict[str, Any]:
    """Promote a complete prepared job atomically; preserve unsuccessful attempts."""
    if output.exists():
        raise ValueError("publication preparation requires a fresh output")
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = Path(tempfile.mkdtemp(prefix="preparing-", dir=output.parent))
    try:
        job = _prepare_publication(queue_root, staging)
        staging.rename(output)
        return job
    except Exception:  # pylint: disable=broad-exception-caught
        staging.rename(staging.with_name("failed-" + staging.name))
        raise


def _prepare_publication(queue_root: Path, output: Path) -> dict[str, Any]:
    if platform.system() != "Linux":
        raise ValueError("publication assembly is routed to Ubuntu")
    revision = deployed_revision()
    gh = shutil.which("gh")
    if gh is None:
        raise ValueError("existing Ubuntu GitHub credential CLI is unavailable")
    baseline = output / "baseline"
    baseline.mkdir(parents=True, mode=0o700)
    subprocess.run(
        [
            gh,
            "release",
            "download",
            revision["snapshot_tag"],
            "--repo",
            "f5-sales-demo/html-to-markdown",
            "--dir",
            str(baseline),
        ],
        check=True,
        capture_output=True,
    )  # nosec B603
    _verified_baseline(baseline, revision["publication_sha256"])
    selected: dict[str, tuple[Path, str]] = {}
    ranks: dict[str, tuple[str, int]] = {}
    rejected = []
    for receipt_path in sorted((queue_root / ".reviewed").glob("*/*/*/source-review-receipt.json")):
        directory = receipt_path.parent
        digest = sha(receipt_path.read_bytes())
        if directory.name != digest:
            raise ValueError("private reviewed input directory does not match its pin")
        try:
            receipt = verify_source(directory, digest)
        except (ValueError, KeyError, TypeError, OSError) as failure:
            rejected.append(
                {
                    "input_sha256": digest,
                    "queue": directory.parent.parent.name + "/" + directory.parent.name,
                    "reason": "source_input_verification_failed",
                    "error_class": type(failure).__name__,
                }
            )
            continue
        source = receipt["source"]
        rank = (
            receipt["ended_at"],
            sum(
                item["disposition"] in {"accepted", "policy_excluded"}
                for item in receipt["documents"]
            ),
        )
        if source not in selected or rank > ranks[source]:
            selected[source], ranks[source] = (directory, digest), rank
    atomic_private(queue_root / ".publication/source-input-exceptions.json", json_bytes(rejected))
    if not selected:
        raise ValueError("no independently reviewed source input is ready")
    combined = output / "combined"
    combine_sources(baseline, selected, combined, revision["publication_sha256"])
    decisions = output / "enrichment-decisions.json"
    prepare(
        combined,
        baseline,
        decisions,
        tag=revision["snapshot_tag"],
        receipt_sha256=revision["publication_sha256"],
        version=2,
    )
    job = {
        "schema_version": 1,
        "kind": "enrichment",
        "baseline_tag": revision["snapshot_tag"],
        "baseline_receipt_sha256": revision["publication_sha256"],
        "curated_sha256": corpus_digest(combined),
        "selected_sources": {source: pin for source, (_, pin) in selected.items()},
        "rejected_source_inputs": rejected,
        "prepared_at": utc_now(),
    }
    atomic_private(output / "publication-job.json", json_bytes(job))
    return job


def stage_publication(output: Path, *, source_commit: str) -> dict[str, Any]:
    """Upload only consumer assets; the workflow rechecks the deployed baseline."""
    if platform.system() != "Linux":
        raise ValueError("GitHub publication mutations are routed to Ubuntu")
    job = json.loads((output / "publication-job.json").read_bytes())
    combined = output / "combined"
    if not (combined / "enrichment-state.json").is_file():
        raise ValueError("publication requires the complete initial enrichment pass")
    if job["baseline_receipt_sha256"] != deployed_revision()["publication_sha256"]:
        raise ValueError("publication baseline has been superseded")
    verify_enrichment(combined)
    artifact = load_artifact(combined / ".enrichment/decisions.json")
    if artifact.schema_version != 2 or any(
        not record.v2
        or (
            not record.v2.get("attempts")
            and record.v2.get("failure_reason") != "complete_media_unavailable"
        )
        for record in artifact.documents
    ):
        raise ValueError("initial enrichment pass has unattempted documents")
    pending_path = output / "staging-publication.json"
    if pending_path.exists():
        pending = json.loads(pending_path.read_bytes())
        if pending["source_commit"] != source_commit or pending["receipt_sha256"] != sha(
            (combined / "publication.json").read_bytes()
        ):
            raise ValueError("staged candidate software or receipt changed")
        tag, digest = pending["tag"], pending["receipt_sha256"]
    else:
        tag = "content-" + utc_now().replace("-", "").replace(":", "").replace("+0000", "Z")
        write_publication_receipt(
            combined,
            tag=tag,
            source_commit=source_commit,
            created_at=json.loads((combined / "manifest.json").read_bytes())["ended_at"],
            published_at=utc_now(),
        )
        digest = sha((combined / "publication.json").read_bytes())
        atomic_private(
            pending_path,
            json_bytes({"tag": tag, "receipt_sha256": digest, "source_commit": source_commit}),
        )
    gh = shutil.which("gh")
    if gh is None:
        raise ValueError("Ubuntu GitHub credential CLI is unavailable")
    existing = subprocess.run(
        [
            gh,
            "release",
            "view",
            tag,
            "--repo",
            "f5-sales-demo/html-to-markdown",
            "--json",
            "isDraft,targetCommitish,assets",
        ],
        capture_output=True,
        text=True,
        check=False,
    )  # nosec B603
    if existing.returncode == 0:
        release = json.loads(existing.stdout)
        if not release["isDraft"] or release["targetCommitish"] != source_commit:
            raise ValueError("candidate release is not the expected private draft")
        expected = set((*RELEASE_ASSET_NAMES, "publication.json"))
        actual = {asset["name"] for asset in release["assets"]}
        if actual - expected:
            raise ValueError("candidate draft contains unexpected assets")
        if expected - actual:
            subprocess.run(
                [
                    gh,
                    "release",
                    "upload",
                    tag,
                    "--repo",
                    "f5-sales-demo/html-to-markdown",
                    *(str(combined / name) for name in sorted(expected - actual)),
                ],
                check=True,
                capture_output=True,
            )  # nosec B603
    elif "release not found" in existing.stderr.lower():
        subprocess.run(
            [
                gh,
                "release",
                "create",
                tag,
                "--repo",
                "f5-sales-demo/html-to-markdown",
                "--draft",
                "--latest=false",
                "--target",
                source_commit,
                "--title",
                tag,
                "--generate-notes",
                *(str(combined / name) for name in (*RELEASE_ASSET_NAMES, "publication.json")),
            ],
            check=True,
            capture_output=True,
        )  # nosec B603
    else:
        raise subprocess.CalledProcessError(
            existing.returncode, existing.args, existing.stdout, existing.stderr
        )
    with tempfile.TemporaryDirectory(prefix="draft-verification-", dir=output) as temporary:
        download = Path(temporary)
        subprocess.run(
            [
                gh,
                "release",
                "download",
                tag,
                "--repo",
                "f5-sales-demo/html-to-markdown",
                "--dir",
                str(download),
            ],
            check=True,
            capture_output=True,
        )  # nosec B603
        verify_publication_receipt(
            download,
            json.loads((download / "publication.json").read_bytes()),
            expected_tag=tag,
            expected_source_commit=source_commit,
            expected_receipt_sha256=digest,
            actual_asset_names={path.name for path in download.iterdir()},
        )
    subprocess.run(
        [
            gh,
            "workflow",
            "run",
            "publish-reviewed-content.yml",
            "--repo",
            "f5-sales-demo/html-to-markdown",
            "--ref",
            "main",
            "-f",
            "candidate-tag=" + tag,
            "-f",
            "publication-sha256=" + digest,
            "-f",
            "baseline-receipt-sha256=" + job["baseline_receipt_sha256"],
        ],
        check=True,
        capture_output=True,
    )  # nosec B603
    result = {
        "tag": tag,
        "receipt_sha256": digest,
        "baseline_receipt_sha256": job["baseline_receipt_sha256"],
        "source_commit": source_commit,
    }
    atomic_private(output / "staged-publication.json", json_bytes(result))
    return result


# Preserve all stage evidence and checks in one transaction.
# pylint: disable-next=too-many-return-statements
def advance_publication(queue_root: Path, *, source_commit: str | None = None) -> dict[str, Any]:
    """Progress preparation, Mac review and locked promotion without mixing baselines."""
    if platform.system() != "Linux":
        raise ValueError("publication coordination runs on Ubuntu")
    state_root = queue_root / ".publication"
    state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state_root / "coordinator.lock").open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"state": "already_running"}
        current = state_root / "current-job.json"
        inputs = [
            (str(path), sha(path.read_bytes()))
            for path in sorted((queue_root / ".reviewed").glob("*/*/*/source-review-receipt.json"))
        ]
        revision = deployed_revision() if inputs or current.exists() else None
        fingerprint = sha(
            json_bytes(
                {
                    "inputs": inputs,
                    "software": __version__,
                    "baseline": revision["publication_sha256"] if revision else None,
                }
            )
        )
        if current.exists():
            state = json.loads(current.read_bytes())
            job = state_root / state["job"]
            prepared = json.loads((job / "publication-job.json").read_bytes())
            superseded = (
                revision is not None
                and prepared["baseline_receipt_sha256"] != revision["publication_sha256"]
            )
            if not superseded and (job / "staged-publication.json").exists():
                return {"state": "waiting_for_exact_deployment", "job": state["job"]}
            if not superseded and (not inputs or state.get("fingerprint") == fingerprint):
                if not (job / "validated-enrichment.json").exists():
                    return {"state": "waiting_for_mac_enrichment", "job": state["job"]}
                if source_commit is None:
                    return {"state": "waiting_for_qualified_software_commit", "job": state["job"]}
                return {"state": "staged", **stage_publication(job, source_commit=source_commit)}
            atomic_private(
                job / "superseded.json",
                json_bytes({"baseline": revision, "new_fingerprint": fingerprint}),
            )
        if not inputs:
            return {"state": "waiting_for_source_reviews"}
        job_name = "job-" + fingerprint
        job = state_root / job_name
        if not (job / "publication-job.json").exists():
            prepare_publication(queue_root, job)
        atomic_private(current, json_bytes({"job": job_name, "fingerprint": fingerprint}))
        return {"state": "prepared_for_mac_enrichment", "job": job_name}
