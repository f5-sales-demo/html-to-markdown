"""Collection clocks run on Ubuntu while MacBook inference is unavailable."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from html_to_markdown.collection_service import (
    allocate_collection_run,
    install_collectors,
    verified_baseline_files,
)


def test_daily_and_weekly_timers_keep_existing_clocks_and_private_umask(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("html_to_markdown.collection_service.platform.system", lambda: "Linux")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    calls = []
    monkeypatch.setattr(
        "html_to_markdown.collection_service.subprocess.run",
        lambda args, **kwargs: calls.append(args),
    )
    names = install_collectors(tmp_path / "private-queue")
    assert len(names) == 3
    units = tmp_path / ".config/systemd/user"
    assert "04:23:00 UTC" in (units / names[0]).read_text()
    assert "Sun *-*-* 03:00:00 UTC" in (units / names[2]).read_text()
    service = (units / "html-to-markdown-full-sunday.service").read_text()
    assert "--full" in service and "UMask=0077" in service
    assert "review-content" not in service
    assert calls[-1][:4] == ["/usr/bin/systemctl", "--user", "enable", "--now"]


def test_collection_timer_installation_cannot_move_inference_to_ubuntu(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("html_to_markdown.collection_service.platform.system", lambda: "Darwin")
    with pytest.raises(ValueError, match="Ubuntu"):
        install_collectors(tmp_path)


def test_concurrent_collectors_allocate_distinct_runs_without_overwriting_evidence(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "html_to_markdown.collection_service.utc_now", lambda: "2026-11-19T17:49:08Z"
    )
    existing = tmp_path / "capture-20261119T174908Z"
    existing.mkdir()
    marker = existing / "preserved.json"
    marker.write_bytes(b'{"original_capture":true}')
    with ThreadPoolExecutor(max_workers=8) as pool:
        runs = list(pool.map(lambda _: allocate_collection_run(tmp_path), range(16)))
    assert len(set(runs)) == 16
    assert all(run.is_dir() and run.stat().st_mode & 0o077 == 0 for run in runs)
    assert existing not in runs
    assert marker.read_bytes() == b'{"original_capture":true}'


def test_normal_collection_run_name_stays_compatible(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "html_to_markdown.collection_service.utc_now", lambda: "2026-11-19T17:49:08Z"
    )
    assert allocate_collection_run(tmp_path).name == "capture-20261119T174908Z"


def test_cold_baseline_download_is_serialized_and_verified_before_visibility(tmp_path, monkeypatch):
    calls = []

    def download(args, **_kwargs):
        calls.append(args)
        directory = Path(args[args.index("--dir") + 1])
        (directory / "marker").write_bytes(b"complete verified baseline")

    def verify(directory, _pin):
        assert (directory / "marker").read_bytes() == b"complete verified baseline"
        return {"manifest.json": b'{"documents":[]}'}

    monkeypatch.setattr(
        "html_to_markdown.collection_service.shutil.which", lambda _: "/existing/gh"
    )
    monkeypatch.setattr("html_to_markdown.collection_service.subprocess.run", download)
    monkeypatch.setattr("html_to_markdown.collection_service._verified_baseline", verify)
    with ThreadPoolExecutor(max_workers=8) as pool:
        outputs = list(
            pool.map(
                lambda _: verified_baseline_files(tmp_path, "content-20261110T194938Z", "a" * 64),
                range(16),
            )
        )
    assert len(calls) == 1 and len(outputs) == 16
    assert (tmp_path / ".baselines/content-20261110T194938Z/marker").exists()


def test_failed_baseline_download_stays_private_without_becoming_active(tmp_path, monkeypatch):
    def download(args, **_kwargs):
        directory = Path(args[args.index("--dir") + 1])
        (directory / "partial").write_bytes(b"failed downloaded bytes")
        raise OSError("simulated transport failure")

    monkeypatch.setattr(
        "html_to_markdown.collection_service.shutil.which", lambda _: "/existing/gh"
    )
    monkeypatch.setattr("html_to_markdown.collection_service.subprocess.run", download)
    with pytest.raises(OSError):
        verified_baseline_files(tmp_path, "content-20261110T194938Z", "a" * 64)
    assert not (tmp_path / ".baselines/content-20261110T194938Z").exists()
    assert (
        next((tmp_path / ".baselines").glob(".download-*/partial")).read_bytes()
        == b"failed downloaded bytes"
    )
