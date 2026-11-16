"""Verified snapshots share physical storage without accepting stale or partial inputs."""

import errno
import json
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_source_refresh import prepare

from html_to_markdown import baseline_cache
from html_to_markdown.baseline_cache import ASSETS, _link_file, baseline_snapshot
from html_to_markdown.enrichment.analysis import sha

TAG = "content-20261002T000100Z"


def _download(monkeypatch, source, *, fail_first=False, extra=False):
    calls = []

    def run(args, **_kwargs):
        calls.append(args)
        destination = Path(args[args.index("--dir") + 1])
        if fail_first and len(calls) == 1:
            (destination / "manifest.json").write_bytes(b"partial capture")
            raise subprocess.CalledProcessError(1, args)
        for name in ASSETS:
            shutil.copy2(source / name, destination / name)
        if extra:
            (destination / "unexpected.txt").write_bytes(b"unreviewed")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(baseline_cache.shutil, "which", lambda _name: "/fixture/gh")
    monkeypatch.setattr(baseline_cache.subprocess, "run", run)
    return calls


def test_existing_verified_input_and_second_job_share_exact_asset_inodes(tmp_path):
    source, _, pin = prepare(tmp_path)
    before = {name: (source / name).read_bytes() for name in ASSETS}
    cache = tmp_path / "cache"
    assert baseline_snapshot(source, pin, TAG, cache_root=cache) == source
    job = tmp_path / "second/job/baseline" / pin
    baseline_snapshot(job, pin, TAG, cache_root=cache)
    for name, data in before.items():
        assert (job / name).read_bytes() == (cache / pin / name).read_bytes() == data
        assert (source / name).stat().st_ino == (job / name).stat().st_ino
    assert {p.name for p in job.iterdir()} == set(ASSETS)
    assert {name: (source / name).read_bytes() for name in ASSETS} == before


def test_concurrent_jobs_use_one_complete_download(tmp_path, monkeypatch):
    source, _, pin = prepare(tmp_path)
    calls = _download(monkeypatch, source)
    cache = tmp_path / "cache"
    jobs = [tmp_path / "job-one" / pin, tmp_path / "job-two" / pin]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(baseline_snapshot, job, pin, TAG, cache_root=cache) for job in jobs]
        assert [future.result() for future in futures] == jobs
    assert len(calls) == 1
    assert calls[0][1:3] == ["release", "download"]
    assert (jobs[0] / "publication.json").read_bytes() == (
        jobs[1] / "publication.json"
    ).read_bytes()
    assert (jobs[0] / ASSETS[0]).stat().st_ino == (jobs[1] / ASSETS[0]).stat().st_ino


def test_existing_uploaded_job_shares_storage_without_dropping_private_auxiliary_files(tmp_path):
    source, _, pin = prepare(tmp_path)
    cache = tmp_path / "cache"
    baseline_snapshot(source, pin, TAG, cache_root=cache)
    uploaded = tmp_path / "uploaded" / "baseline" / pin
    shutil.copytree(source, uploaded)
    auxiliary = uploaded / "private-local-receipt.json"
    auxiliary.write_bytes(b'{"untrusted":true}')
    assert (uploaded / ASSETS[0]).stat().st_ino != (cache / pin / ASSETS[0]).stat().st_ino
    before = {name: (uploaded / name).read_bytes() for name in ASSETS}
    baseline_snapshot(uploaded, pin, TAG, cache_root=cache)
    for name, data in before.items():
        assert (uploaded / name).read_bytes() == data
        assert (uploaded / name).stat().st_ino == (cache / pin / name).stat().st_ino
    assert auxiliary.read_bytes() == b'{"untrusted":true}'
    assert not (cache / pin / auxiliary.name).exists()


def test_failed_download_is_preserved_and_retry_does_not_reuse_it(tmp_path, monkeypatch):
    source, _, pin = prepare(tmp_path)
    calls = _download(monkeypatch, source, fail_first=True)
    cache, job = tmp_path / "cache", tmp_path / "job" / pin
    with pytest.raises(subprocess.CalledProcessError):
        baseline_snapshot(job, pin, TAG, cache_root=cache)
    assert not job.exists() and not (cache / pin).exists()
    failed = list((cache / "attempts").glob("*/status.json"))
    assert len(failed) == 1 and json.loads(failed[0].read_bytes())["state"] == "failed"
    partial = failed[0].parent / "downloads/manifest.json"
    assert partial.read_bytes() == b"partial capture"
    baseline_snapshot(job, pin, TAG, cache_root=cache)
    assert len(calls) == 2 and partial.read_bytes() == b"partial capture"
    states = [
        json.loads(p.read_bytes())["state"] for p in (cache / "attempts").glob("*/status.json")
    ]
    assert sorted(states) == ["failed", "verified"]


def test_unexpected_download_asset_cannot_become_a_cache_hit(tmp_path, monkeypatch):
    source, _, pin = prepare(tmp_path)
    _download(monkeypatch, source, extra=True)
    cache = tmp_path / "cache"
    with pytest.raises(ValueError, match="unexpected asset set"):
        baseline_snapshot(tmp_path / "job" / pin, pin, TAG, cache_root=cache)
    assert not (cache / pin).exists()
    status = next((cache / "attempts").glob("*/status.json"))
    assert json.loads(status.read_bytes())["state"] == "failed"


@pytest.mark.parametrize(
    "change",
    ["tag", "pin", "archive", "asset_symlink", "cache_symlink", "lock_symlink", "attempts_symlink"],
)
def test_stale_or_unsafe_inputs_fail_closed(tmp_path, monkeypatch, change):
    source, _, pin = prepare(tmp_path)
    cache = tmp_path / "cache"
    cache.mkdir()
    tag = TAG
    if change == "tag":
        tag = "content-20261003T000100Z"
    elif change == "pin":
        pin = "f" * 64
    elif change == "archive":
        (source / "html-to-markdown-content.tar.gz").write_bytes(b"changed bytes")
    elif change == "asset_symlink":
        target = tmp_path / "outside.json"
        target.write_bytes((source / "publication.json").read_bytes())
        (source / "publication.json").unlink()
        (source / "publication.json").symlink_to(target)
    elif change == "cache_symlink":
        (cache / pin).symlink_to(tmp_path / "absent")
    elif change == "lock_symlink":
        (cache / (pin + ".lock")).symlink_to(tmp_path / "outside-lock")
    else:
        (cache / "attempts").symlink_to(tmp_path / "outside-attempts")
        _download(monkeypatch, source)
        source = tmp_path / "uncaptured" / pin
    with pytest.raises((ValueError, OSError)):
        baseline_snapshot(source, pin, tag, cache_root=cache)


def test_cached_tampering_and_identical_byte_symlinks_are_rejected(tmp_path):
    source, _, pin = prepare(tmp_path)
    cache = tmp_path / "cache"
    baseline_snapshot(source, pin, TAG, cache_root=cache)
    cached = cache / pin
    outside = tmp_path / "same-receipt.json"
    outside.write_bytes((cached / "publication.json").read_bytes())
    (cached / "publication.json").unlink()
    (cached / "publication.json").symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        baseline_snapshot(tmp_path / "second" / pin, pin, TAG, cache_root=cache)
    (cached / "publication.json").unlink()
    (cached / "publication.json").write_bytes(outside.read_bytes())
    (cached / "html-to-markdown-content.tar.gz").write_bytes(b"corrupt shared asset")
    with pytest.raises(ValueError):
        baseline_snapshot(tmp_path / "second" / pin, pin, TAG, cache_root=cache)


def test_distinct_receipt_cannot_reuse_another_cache_identity(tmp_path):
    source, _, pin = prepare(tmp_path)
    cache = tmp_path / "cache"
    baseline_snapshot(source, pin, TAG, cache_root=cache)
    changed = tmp_path / "changed"
    shutil.copytree(source, changed)
    receipt = json.loads((changed / "publication.json").read_bytes())
    receipt["source_commit"] = "b" * 40
    (changed / "publication.json").write_text(json.dumps(receipt))
    new_pin = sha((changed / "publication.json").read_bytes())
    assert new_pin != pin
    baseline_snapshot(changed, new_pin, TAG, cache_root=cache)
    assert (cache / new_pin / "publication.json").read_bytes() != (
        cache / pin / "publication.json"
    ).read_bytes()


def test_cross_filesystem_copy_fallback_preserves_complete_inputs(tmp_path, monkeypatch):
    source, _, pin = prepare(tmp_path)

    def unsupported(*_args):
        raise OSError(errno.EXDEV, "different filesystem")

    monkeypatch.setattr(baseline_cache.os, "link", unsupported)
    cache = tmp_path / "cache"
    baseline_snapshot(source, pin, TAG, cache_root=cache)
    for name in ASSETS:
        assert (source / name).read_bytes() == (cache / pin / name).read_bytes()
        assert (source / name).stat().st_ino != (cache / pin / name).stat().st_ino


def test_interrupted_snapshot_is_not_published_or_reused(tmp_path, monkeypatch):
    source, _, pin = prepare(tmp_path)
    original = _link_file
    count = 0

    def interrupted(src, dst):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("interrupted copy")
        original(src, dst)

    monkeypatch.setattr(baseline_cache, "_link_file", interrupted)
    cache = tmp_path / "cache"
    with pytest.raises(OSError, match="interrupted"):
        baseline_snapshot(source, pin, TAG, cache_root=cache)
    assert not (cache / pin).exists()
    partial = list(cache.glob(".baseline-snapshot-*"))
    assert len(partial) == 1 and len(list(partial[0].iterdir())) == 1
    monkeypatch.setattr(baseline_cache, "_link_file", original)
    baseline_snapshot(source, pin, TAG, cache_root=cache)
    assert partial[0].exists() and len(list(cache.glob(".baseline-snapshot-*"))) == 1
