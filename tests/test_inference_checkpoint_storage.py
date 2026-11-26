"""Verified intake shares immutable checkpoint bytes while retaining every evidence path."""

import errno
import io
import json
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_inference_queue import EXECUTOR, OWNER, body, packet

import html_to_markdown.inference_queue as queue
from html_to_markdown.curation import json_bytes
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.inference_queue import _validated
from html_to_markdown.work_queue import pack_private_tree


def setup_job(tmp_path, monkeypatch):
    root = tmp_path / "queue"
    job = queue.submit(root, body(), EXECUTOR)
    queue.claim(root, OWNER, EXECUTOR)
    data, evidence = packet(tmp_path, monkeypatch, job)
    return root, job, data, evidence


def checkpoint(root, job):
    directory = root / job
    state = json.loads((directory / "state.json").read_bytes())
    return directory / state["payload"] / "journal" / (job.split("/")[0] + ".request.json")


def repack(spool):
    pins = {
        p.relative_to(spool).as_posix(): sha(p.read_bytes())
        for p in spool.rglob("*")
        if p.is_file() and p.name != "manifest.json"
    }
    (spool / "manifest.json").write_bytes(json_bytes(pins))
    stream = io.BytesIO()
    pack_private_tree(spool, stream)
    return stream.getvalue()


def test_verified_intake_shares_bytes_preserves_source_and_replays_retried_ack(
    tmp_path, monkeypatch
):
    root, job, data, evidence = setup_job(tmp_path, monkeypatch)
    source = root / job / "request.json"
    before = source.stat()
    original = source.read_bytes()
    receipt = queue.receive(root, job, OWNER, sha(data), io.BytesIO(data))
    target = checkpoint(root, job)
    assert source.samefile(target)
    assert source.read_bytes() == target.read_bytes() == original
    after = source.stat()
    assert (before.st_ino, before.st_mode, before.st_mtime_ns) == (
        after.st_ino,
        after.st_mode,
        after.st_mtime_ns,
    )
    assert receipt["request_checkpoint_storage"] == "shared"
    assert queue.result(root, job) == evidence
    assert queue.receive(root, job, OWNER, sha(data), io.BytesIO(data)) == receipt
    assert all(sha(p.read_bytes()) == sha(data) for p in (root / job / "uploads").glob("*.tar.gz"))
    assert not list((root / job).rglob(".request-checkpoint-*"))


def test_equivalent_json_with_different_original_bytes_remains_independent(tmp_path, monkeypatch):
    root, job, _, evidence = setup_job(tmp_path, monkeypatch)
    target = tmp_path / "spool/journal" / (evidence.request_sha256 + ".request.json")
    target.write_text(json.dumps(body(), separators=(",", ":")))
    original = target.read_bytes()
    data = repack(tmp_path / "spool")
    receipt = queue.receive(root, job, OWNER, sha(data), io.BytesIO(data))
    saved = checkpoint(root, job)
    assert saved.read_bytes() == original
    assert not saved.samefile(root / job / "request.json")
    assert receipt["request_checkpoint_storage"] == "independent"
    assert queue.result(root, job) == evidence


@pytest.mark.parametrize("replace_source", [False, True])
def test_atomic_replacement_of_shared_path_preserves_other_bytes_and_fails_replay(
    tmp_path, monkeypatch, replace_source
):
    root, job, data, _ = setup_job(tmp_path, monkeypatch)
    queue.receive(root, job, OWNER, sha(data), io.BytesIO(data))
    source, target = root / job / "request.json", checkpoint(root, job)
    assert source.samefile(target)
    original = source.read_bytes()
    changed, retained = (source, target) if replace_source else (target, source)
    request = body()
    request["input"][0]["content"][0]["text"] += " changed"
    queue.atomic_private(changed, json_bytes(request))
    assert retained.read_bytes() == original and not source.samefile(target)
    with pytest.raises(ValueError):
        queue.result(root, job)


def test_link_replace_failure_keeps_both_original_paths_without_ack_or_temporary_leak(
    tmp_path, monkeypatch
):
    root, job, data, _ = setup_job(tmp_path, monkeypatch)
    original = (root / job / "request.json").read_bytes()
    replace = queue.os.replace

    def fail_link(source, target):
        if Path(source).name.startswith(".request-checkpoint-"):
            raise OSError("simulated link transaction interruption")
        return replace(source, target)

    monkeypatch.setattr(queue.os, "replace", fail_link)
    with pytest.raises(OSError, match="link transaction"):
        queue.receive(root, job, OWNER, sha(data), io.BytesIO(data))
    assert not (root / job / "receipt.json").exists()
    assert (root / job / "request.json").read_bytes() == original
    assert not list((root / job).rglob(".request-checkpoint-*"))
    assert list((root / job / "uploads").glob("*/journal/*.request.json"))
    monkeypatch.setattr(queue.os, "replace", replace)
    queue.receive(root, job, OWNER, sha(data), io.BytesIO(data))
    assert queue.result(root, job).error is None


def test_link_transaction_never_changes_sealed_manifest_coverage(tmp_path, monkeypatch):
    root, job, data, evidence = setup_job(tmp_path, monkeypatch)
    replace = queue.os.replace
    observed = []

    def check_coverage(source, target):
        if Path(source).name.startswith(".request-checkpoint-"):
            payload = Path(target).parent.parent
            state = json.loads((root / job / "state.json").read_bytes())
            assert not Path(source).is_relative_to(payload)
            assert _validated(root / job, payload, state)[0] == evidence
            observed.append(True)
        return replace(source, target)

    monkeypatch.setattr(queue.os, "replace", check_coverage)
    queue.receive(root, job, OWNER, sha(data), io.BytesIO(data))
    assert observed == [True]
    assert queue.result(root, job) == evidence


@pytest.mark.parametrize("kind", ["mode", "xattr", "device"])
def test_private_metadata_or_filesystem_difference_retains_independent_checkpoint(
    tmp_path, monkeypatch, kind
):
    root, job, data, evidence = setup_job(tmp_path, monkeypatch)
    unpack = queue.unpack_queue
    lstat = Path.lstat

    def different_metadata(archive, destination):
        unpack(archive, destination)
        target = destination / "journal" / (evidence.request_sha256 + ".request.json")
        if kind == "mode":
            target.chmod(stat.S_IRUSR)
        elif kind == "xattr":
            monkeypatch.setattr(
                queue.os, "listxattr", lambda p: ["user.test"] if Path(p) == target else []
            )
            monkeypatch.setattr(queue.os, "getxattr", lambda p, name: b"test")
        else:

            def different_device(path):
                result = lstat(path)
                if path == target:
                    return SimpleNamespace(
                        st_dev=result.st_dev + 1,
                        st_mode=result.st_mode,
                        st_uid=result.st_uid,
                        st_gid=result.st_gid,
                    )
                return result

            monkeypatch.setattr(Path, "lstat", different_device)

    monkeypatch.setattr(queue, "unpack_queue", different_metadata)
    receipt = queue.receive(root, job, OWNER, sha(data), io.BytesIO(data))
    saved = checkpoint(root, job)
    assert not saved.samefile(root / job / "request.json")
    assert receipt["request_checkpoint_storage"] == "independent"
    assert queue.result(root, job) == evidence


@pytest.mark.parametrize("failure", [errno.EXDEV, errno.ENOSPC])
def test_link_errors_only_fallback_for_cross_filesystem(tmp_path, monkeypatch, failure):
    root, job, data, _ = setup_job(tmp_path, monkeypatch)

    def fail_link(*args, **kwargs):
        raise OSError(failure, "simulated link failure")

    monkeypatch.setattr(queue.os, "link", fail_link)
    if failure == errno.ENOSPC:
        with pytest.raises(OSError, match="simulated link failure"):
            queue.receive(root, job, OWNER, sha(data), io.BytesIO(data))
        assert not (root / job / "receipt.json").exists()
    else:
        receipt = queue.receive(root, job, OWNER, sha(data), io.BytesIO(data))
        assert receipt["request_checkpoint_storage"] == "independent"
        assert queue.result(root, job).error is None
    assert not list((root / job).rglob(".request-checkpoint-*"))


def test_changed_checkpoint_cannot_be_shared_or_acknowledged(tmp_path, monkeypatch):
    root, job, _, _ = setup_job(tmp_path, monkeypatch)
    target = next((tmp_path / "spool/journal").glob("*.request.json"))
    original = (root / job / "request.json").read_bytes()
    target.write_bytes(original + b"changed")
    stream = io.BytesIO()
    pack_private_tree(tmp_path / "spool", stream)
    data = stream.getvalue()
    with pytest.raises(ValueError, match="evidence bytes changed"):
        queue.receive(root, job, OWNER, sha(data), io.BytesIO(data))
    assert (root / job / "request.json").read_bytes() == original
    assert not (root / job / "receipt.json").exists()
    assert not list((root / job).rglob(".request-checkpoint-*"))


@pytest.mark.parametrize("kind", ["symlink", "public_permissions"])
def test_unsafe_checkpoint_cannot_be_shared_or_acknowledged(tmp_path, monkeypatch, kind):
    root, job, data, evidence = setup_job(tmp_path, monkeypatch)
    unpack = queue.unpack_queue
    original = (root / job / "request.json").read_bytes()

    def unsafe_checkpoint(archive, destination):
        unpack(archive, destination)
        target = destination / "journal" / (evidence.request_sha256 + ".request.json")
        if kind == "symlink":
            target.unlink()
            target.symlink_to(root / job / "request.json")
        else:
            target.chmod(0o644)

    monkeypatch.setattr(queue, "unpack_queue", unsafe_checkpoint)
    with pytest.raises(ValueError, match="symlinks|permissions"):
        queue.receive(root, job, OWNER, sha(data), io.BytesIO(data))
    assert (root / job / "request.json").read_bytes() == original
    assert not (root / job / "receipt.json").exists()
    assert not list((root / job).rglob(".request-checkpoint-*"))
