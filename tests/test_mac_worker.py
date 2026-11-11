"""Awake worker scheduling, single-worker lock and credential-free configuration."""

import fcntl
import json
import plistlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from html_to_markdown.mac_worker import install_launch_agent, worker_once


def test_idle_worker_checks_private_route_and_preserves_safe_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("html_to_markdown.mac_worker.platform.system", lambda: "Darwin")
    calls = []

    def run(args: list[str], **kwargs: object):
        calls.append(args)
        return SimpleNamespace(
            stdout=b"[]"
            if "list-review-queues" in args
            else b'{"state":"waiting_for_source_reviews"}'
        )

    monkeypatch.setattr("html_to_markdown.mac_worker.subprocess.run", run)
    status = worker_once(
        host="worker.example.invalid",
        remote_executable="/private/bin/html-to-markdown",
        remote_queue="/private/queue",
        output=tmp_path / "worker",
        transport=Path("/private/safe-ssh-exec"),
    )
    assert status["state"] == "idle" and status["concurrency"] == 4
    assert calls[0][-3:] == ["list-review-queues", "--queue-root", "/private/queue"]
    assert json.loads((tmp_path / "worker/worker-status.json").read_bytes())["state"] == "idle"


def test_existing_worker_lock_prevents_overlapping_inference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("html_to_markdown.mac_worker.platform.system", lambda: "Darwin")
    with (tmp_path / "worker.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = worker_once(
            host="unused",
            remote_executable="unused",
            remote_queue="unused",
            output=tmp_path,
            transport=Path("unused"),
        )
        assert result == {"state": "already_running"}


def test_launch_agent_checks_every_fifteen_minutes_without_persisting_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("html_to_markdown.mac_worker.platform.system", lambda: "Darwin")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    calls = []
    monkeypatch.setattr(
        "html_to_markdown.mac_worker.subprocess.run", lambda args, **kwargs: calls.append(args)
    )
    path = install_launch_agent(
        host="worker.example.invalid",
        remote_executable="/private/bin/html-to-markdown",
        remote_queue="/private/queue",
        output=tmp_path / "state",
        transport=Path("/private/safe-ssh-exec"),
    )
    definition = plistlib.loads(path.read_bytes())
    assert definition["StartInterval"] == 900 and definition["RunAtLoad"]
    assert definition["Umask"] == 0o077
    assert "EnvironmentVariables" not in definition
    assert definition["ProgramArguments"][:3] == ["/bin/zsh", "-l", "-c"]
    assert calls[0][1] == "bootstrap"


def test_linux_cannot_run_content_inference_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("html_to_markdown.mac_worker.platform.system", lambda: "Linux")
    with pytest.raises(ValueError, match="MacBook"):
        worker_once(
            host="unused",
            remote_executable="unused",
            remote_queue="unused",
            output=tmp_path,
            transport=Path("unused"),
        )
