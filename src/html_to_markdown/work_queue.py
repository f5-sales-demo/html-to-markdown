"""Private Ubuntu queue exports for the awake MacBook review worker."""

from __future__ import annotations

import fcntl
import io
import json
import sqlite3
import tarfile
from pathlib import Path
from typing import Any, BinaryIO

from .capture_payloads import capture_payloads
from .capture_store import capture_digest, durable_links, store_bytes
from .content_inventory import verify_capture
from .curation import json_bytes
from .enrichment.analysis import sha
from .known_candidates import preserved_known_candidates
from .review_frontier import review_frontier as select_review_frontier
from .urls import SOURCE_ROOTS


def list_queues(root: Path, *, review_frontier: bool = False) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    inventories: dict[str, dict[str, Any]] = {}
    if not root.exists():
        return records
    for binding in sorted(root.glob("*/*/capture-binding.json")):
        directory = binding.parent
        if directory.is_symlink() or directory.name not in SOURCE_ROOTS:
            raise ValueError("unexpected private queue source directory")
        database = directory / "inventory.sqlite"
        if not database.is_file():
            continue
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            items = [
                json.loads(row[0])
                for row in connection.execute("SELECT data FROM documents ORDER BY url")
            ]
            phases = {
                row[0]: json.loads(row[1])
                for row in connection.execute("SELECT source,data FROM phases")
            }
        finally:
            connection.close()
        captured = [item for item in items if item.get("capture_complete")]
        fingerprint_inputs = {
            "phases": phases,
            "documents": [
                {
                    key: item.get(key)
                    for key in (
                        "identity",
                        "article_sha256",
                        "source_html_sha256",
                        "media",
                        "disposition",
                        "capture_complete",
                    )
                }
                for item in items
            ],
        }
        binding_data = json.loads(binding.read_bytes())
        inventories[directory.relative_to(root).as_posix()] = {
            "started_at": binding_data.get("started_at"),
            "identities": {item["identity"] for item in items},
            "capture_outcomes": {
                item["identity"]
                for item in items
                if item.get("capture_complete") is True
                or item.get("disposition") in ("capture_error", "access_queue", "policy_excluded")
            },
        }
        known_pin = binding_data["request"].get("known_candidates_sha256")
        if known_pin:
            preserved_known_candidates(directory, directory.name, known_pin)
            fingerprint_inputs["known_candidates_sha256"] = known_pin
        fingerprint = sha(json_bytes(fingerprint_inputs))
        records.append(
            {
                "queue": directory.relative_to(root).as_posix(),
                "source": directory.name,
                "captured": len(captured),
                "discovered": len(items),
                "fingerprint": fingerprint,
            }
        )
    for job_path in sorted((root / ".publication").glob("*/publication-job.json")):
        job = json.loads(job_path.read_bytes())
        if (
            not (job_path.parent / "staged-publication.json").exists()
            and not (job_path.parent / "superseded.json").exists()
        ):
            records.append(
                {
                    "queue": job_path.parent.relative_to(root).as_posix(),
                    "source": "all",
                    "kind": "enrichment",
                    "fingerprint": job["curated_sha256"],
                    "captured": 0,
                    "discovered": 0,
                }
            )
    return select_review_frontier(root, records, inventories) if review_frontier else records


# Preserve all stage evidence and checks in one transaction.
# pylint: disable-next=too-many-locals
def export_queue(root: Path, relative: str, stream: BinaryIO) -> None:
    path = Path(relative)
    if (
        not path.is_absolute()
        and ".." not in path.parts
        and len(path.parts) == 2
        and path.parts[0] == ".publication"
    ):
        directory = root / path
        job = json.loads((directory / "publication-job.json").read_bytes())
        from .curation import corpus_digest  # pylint: disable=import-outside-toplevel

        if corpus_digest(directory / "combined") != job["curated_sha256"]:
            raise ValueError("prepared enrichment corpus changed")
        job_files = {
            name: (directory / name).read_bytes()
            for name in (
                "publication-job.json",
                "enrichment-decisions.json",
                "combined/manifest.json",
                "combined/curation-audit.json",
            )
        }
        job_files.update(
            {
                file.relative_to(directory).as_posix(): file.read_bytes()
                for file in (directory / "combined/content").rglob("*")
                if file.is_file()
            }
        )
        with tarfile.open(fileobj=stream, mode="w|gz") as archive:
            for name, data in sorted(job_files.items()):
                member = tarfile.TarInfo(name)
                member.size, member.mode, member.mtime = len(data), 0o600, 0
                archive.addfile(member, io.BytesIO(data))
        return
    if (
        path.is_absolute()
        or ".." in path.parts
        or len(path.parts) != 2
        or path.parts[-1] not in SOURCE_ROOTS
    ):
        raise ValueError("invalid private queue identity")
    directory = root / path
    if directory.is_symlink():
        raise ValueError("private queue cannot be a symlink")
    connection = sqlite3.connect(f"file:{directory / 'inventory.sqlite'}?mode=ro", uri=True)
    try:
        connection.execute("BEGIN")
        items = [
            json.loads(row[0])
            for row in connection.execute("SELECT data FROM documents ORDER BY url")
        ]
        phases = {
            row[0]: json.loads(row[1])
            for row in connection.execute("SELECT source,data FROM phases")
        }
    finally:
        connection.close()
    binding = json.loads((directory / "capture-binding.json").read_bytes())
    ended_at = max(
        (item.get("captured_at", binding["started_at"]) for item in items),
        default=binding["started_at"],
    )
    source = path.parts[-1]
    inventory = {
        "schema_version": 1,
        "binding": binding["request"],
        "ended_at": ended_at,
        "documents": items,
        "sources": phases,
        "completeness": {
            "enumeration": phases.get(source, {}).get("enumeration_complete", False),
            "capture": all(item.get("capture_complete", False) for item in items),
            "review": False,
            "publication": False,
        },
    }
    files: dict[str, bytes] = {
        "inventory.json": json_bytes(inventory),
        "capture-binding.json": (directory / "capture-binding.json").read_bytes(),
    }
    known_pin = binding["request"].get("known_candidates_sha256")
    if known_pin:
        raw, _ = preserved_known_candidates(directory, binding["request"]["source"], known_pin)
        files["known-candidates/" + known_pin + ".json"] = raw
    for item in items:
        verify_capture(directory, item)
        for field in ("source_html_path", "article_path"):
            if item.get(field):
                files[item[field]] = (directory / item[field]).read_bytes()
        for media in item.get("media", []):
            files[media["path"]] = (directory / media["path"]).read_bytes()
    with tarfile.open(fileobj=stream, mode="w|gz") as archive:
        for name, data in sorted(files.items()):
            entry = tarfile.TarInfo(name)
            entry.size, entry.mode, entry.mtime = len(data), 0o600, 0
            archive.addfile(entry, io.BytesIO(data))


def _declared_payloads(data: tarfile.TarFile, names: set[str]) -> dict[str, str]:
    declared: dict[str, str] = {}
    if "inventory.json" not in names:
        return declared
    stream = data.extractfile("inventory.json")
    if stream is None:
        raise ValueError("private capture inventory missing")
    with stream:
        inventory = json.load(stream)
    if not isinstance(inventory, dict) or not isinstance(inventory.get("documents"), list):
        raise ValueError("invalid private capture inventory")
    for item in inventory["documents"]:
        if not isinstance(item, dict):
            raise ValueError("invalid private capture record")
        for name, expected in capture_payloads(item).items():
            if name not in names:
                raise ValueError("declared capture payload missing from private archive")
            if name in declared and declared[name] != expected:
                raise ValueError("conflicting private capture inventory digests")
            declared[name] = expected
    return declared


def _verify_transfer_payloads(
    data: tarfile.TarFile, members: list[tarfile.TarInfo], declared: dict[str, str]
) -> None:
    for member in members:
        expected = declared.get(member.name, capture_digest(Path(member.name)))
        if expected is None:
            continue
        stream = data.extractfile(member)
        if stream is None:
            raise ValueError("private queue member data missing")
        with stream:
            if sha(stream.read()) != expected:
                raise ValueError("captured transfer digest mismatch")


def unpack_queue(archive: Path, destination: Path, *, capture_store: Path | None = None) -> None:
    """Validate every member before writing any received private capture bytes."""
    with tarfile.open(archive) as data, durable_links():
        members = data.getmembers()
        names = set()
        for member in members:
            path = Path(member.name)
            name = path.as_posix()
            unsafe_path = (
                path.is_absolute()
                or ".." in path.parts
                or "\\" in member.name
                or not path.parts
                or name != member.name
            )
            if unsafe_path or name in names or not member.isfile() or member.size < 0:
                raise ValueError("unsafe private queue archive")
            if any((destination / parent).is_symlink() for parent in [path, *path.parents]):
                raise ValueError("unsafe private queue destination")
            names.add(name)
        if any(parent.as_posix() in names for name in names for parent in Path(name).parents):
            raise ValueError("conflicting private queue archive paths")
        declared = _declared_payloads(data, names) if capture_store is not None else {}
        # Verify every shared payload before writing any member, including mutable metadata.
        if capture_store is not None:
            _verify_transfer_payloads(data, members, declared)
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        from .content_inventory import atomic_private  # pylint: disable=import-outside-toplevel

        for member in members:
            path = Path(member.name)
            # Recheck immediately before writing as well as during complete preflight.
            if any((destination / parent).is_symlink() for parent in [path, *path.parents]):
                raise ValueError("unsafe private queue destination")
            stream = data.extractfile(member)
            if stream is None:
                raise ValueError("private queue member data missing")
            with stream:
                payload = stream.read()
                expected = (
                    declared.get(member.name, capture_digest(path))
                    if capture_store is not None
                    else None
                )
                if expected is not None and capture_store is not None:
                    if sha(payload) != expected:
                        raise ValueError("captured transfer digest mismatch")
                    store_bytes(capture_store, destination / path, payload)
                else:
                    atomic_private(destination / path, payload)


def pack_private_tree(directory: Path, stream: BinaryIO) -> None:
    with tarfile.open(fileobj=stream, mode="w|gz") as archive:
        for path in sorted(directory.rglob("*")):
            if path.is_symlink():
                raise ValueError("private input cannot contain symlinks")
            if not path.is_file():
                continue
            data = path.read_bytes()
            entry = tarfile.TarInfo(path.relative_to(directory).as_posix())
            entry.size, entry.mode, entry.mtime = len(data), 0o600, 0
            archive.addfile(entry, io.BytesIO(data))


def receive_source(root: Path, relative: str, digest: str, stream: BinaryIO) -> Path:
    import tempfile  # pylint: disable=import-outside-toplevel

    from .source_delivery import (  # pylint: disable=import-outside-toplevel
        private_lock,
        promote_source,
        source_destination,
        verify_bound_source,
    )

    destination = source_destination(root, relative, digest)
    with private_lock(destination.parent / ".receive.lock"):
        if destination.exists():
            verify_bound_source(destination, digest, Path(relative).name)
            while stream.read(1024 * 1024):
                pass
            return destination
        with tempfile.TemporaryDirectory(
            prefix="received-source-", dir=destination.parent
        ) as temporary:
            staging = Path(temporary)
            archive = staging / "input.tar.gz"
            with archive.open("wb") as target:
                import shutil  # pylint: disable=import-outside-toplevel

                shutil.copyfileobj(stream, target)
            unpack_queue(archive, staging / "source")
            return promote_source(root, staging / "source", destination, digest)


def receive_enrichment(root: Path, relative: str, digest: str, stream: BinaryIO) -> Path:
    (root / ".publication").mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / ".publication/coordinator.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _receive_enrichment(root, relative, digest, stream)


# Preserve all stage evidence and checks in one transaction.
# pylint: disable-next=too-many-locals
def _receive_enrichment(root: Path, relative: str, digest: str, stream: BinaryIO) -> Path:
    import shutil  # pylint: disable=import-outside-toplevel
    import tempfile  # pylint: disable=import-outside-toplevel

    from .enrichment.engine import replay  # pylint: disable=import-outside-toplevel
    from .package import build_manifest, write_release  # pylint: disable=import-outside-toplevel
    from .quality import write_quality_reports  # pylint: disable=import-outside-toplevel
    from .state import StateStore  # pylint: disable=import-outside-toplevel

    path = Path(relative)
    if (
        path.is_absolute()
        or ".." in path.parts
        or len(path.parts) != 2
        or path.parts[0] != ".publication"
    ):
        raise ValueError("invalid enrichment queue identity")
    directory = root / path
    if (directory / "superseded.json").exists():
        raise ValueError("received enrichment job has been superseded")
    data = stream.read()
    if sha(data) != digest:
        raise ValueError("received enrichment decision digest mismatch")
    destination = directory / "validated-enrichment.json"
    attempt = directory / "enrichment-attempts" / (digest + ".json")
    from .content_inventory import atomic_private  # pylint: disable=import-outside-toplevel

    atomic_private(attempt, data)
    if destination.exists():
        if sha(destination.read_bytes()) != digest:
            raise ValueError("cannot replace an already validated enrichment pin")
        replay(directory / "combined", destination, digest)
        return destination
    promotion = directory / "enrichment-promotion.json"
    if promotion.exists():
        pending = json.loads(promotion.read_bytes())
        if pending["input_sha256"] != digest:
            raise ValueError("interrupted promotion requires its exact enrichment pin")
        candidate_path = Path(pending["candidate_path"])
        if candidate_path.is_absolute() or ".." in candidate_path.parts:
            raise ValueError("unsafe enrichment promotion candidate")
        return _promote_enrichment(directory, directory / candidate_path, attempt, digest)
    staging = Path(tempfile.mkdtemp(prefix="enrichment-staging-", dir=directory))
    combined = staging / "combined"
    shutil.copytree(directory / "combined", combined)
    replay(combined, attempt, digest)
    store = StateStore(combined / "state.sqlite")
    try:
        write_quality_reports(combined)
        previous = json.loads((combined / "manifest.json").read_bytes())
        write_release(
            combined, build_manifest(combined, store, previous["started_at"], previous["ended_at"])
        )
    finally:
        store.close()
    from .enrichment.engine import verify_enrichment  # pylint: disable=import-outside-toplevel

    verify_enrichment(combined)
    atomic_private(
        promotion,
        json_bytes(
            {"input_sha256": digest, "candidate_path": combined.relative_to(directory).as_posix()}
        ),
    )
    return _promote_enrichment(directory, combined, attempt, digest)


def _promote_enrichment(directory: Path, candidate: Path, attempt: Path, digest: str) -> Path:
    from .content_inventory import atomic_private  # pylint: disable=import-outside-toplevel
    from .enrichment.engine import verify_enrichment  # pylint: disable=import-outside-toplevel

    destination = directory / "validated-enrichment.json"
    backup = directory / ("combined-before-enrichment-" + digest)
    if not backup.exists():
        verify_enrichment(candidate)
        (directory / "combined").rename(backup)
    if not (directory / "combined").exists():
        candidate.rename(directory / "combined")
    state = verify_enrichment(directory / "combined")
    if state["artifact_sha256"] != digest:
        raise ValueError("promoted enrichment pin mismatch")
    atomic_private(destination, attempt.read_bytes())
    return destination
