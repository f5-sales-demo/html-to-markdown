"""Awake worker scheduling, single-worker lock and credential-free configuration."""

import fcntl
import json
import plistlib
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from html_to_markdown.curation import json_bytes
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.mac_worker import install_launch_agent, monitored_worker, worker_once
from html_to_markdown.policy_transport import installed_policy_pins
from html_to_markdown.review_priorities import FILENAME


def test_idle_worker_checks_private_route_and_preserves_safe_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("html_to_markdown.mac_worker.platform.system", lambda: "Darwin")
    calls = []

    def run(args: list[str], **kwargs: object):
        calls.append(args)
        if "export-policy-runtime" in args:
            return SimpleNamespace(
                stdout=json_bytes({"state": "policy_current", **installed_policy_pins()})
            )
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
    assert "export-policy-runtime" in calls[0]
    assert calls[1][-4:] == [
        "list-review-queues",
        "--review-frontier",
        "--queue-root",
        "/private/queue",
    ]
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


def test_enrichment_batches_preserve_progress_and_return_only_after_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("html_to_markdown.mac_worker.platform.system", lambda: "Darwin")
    captures = []
    returned = []
    batches: list[Path] = []

    def receive(*args: object) -> None:
        captures.append(args)
        directory = args[-1]
        assert isinstance(directory, Path)
        directory.mkdir(parents=True)
        (directory / "enrichment-decisions.json").write_text("prepared")

    def generate(curated: Path, decisions: Path, **kwargs: object) -> dict:
        assert kwargs["limit"] == 8
        assert decisions.read_text() == ("prepared" if not batches else "partial")
        batches.append(decisions)
        decisions.write_text("partial" if len(batches) == 1 else "complete")
        return {"pending": int(len(batches) == 1)}

    def run(args: list[str], **kwargs: object):
        if "export-policy-runtime" in args:
            return SimpleNamespace(
                stdout=json_bytes({"state": "policy_current", **installed_policy_pins()})
            )
        return SimpleNamespace(
            stdout=json.dumps(
                [
                    {
                        "queue": ".publication/job-test",
                        "fingerprint": "a" * 64,
                        "kind": "enrichment",
                        "captured": 0,
                        "discovered": 0,
                    }
                ]
            ).encode()
            if "list-review-queues" in args
            else b'{"state":"waiting_for_mac_enrichment"}'
        )

    monkeypatch.setattr("html_to_markdown.mac_worker.subprocess.run", run)
    monkeypatch.setattr("html_to_markdown.mac_worker._receive_capture", receive)
    monkeypatch.setattr("html_to_markdown.mac_worker.generate_v2", generate)
    monkeypatch.setattr(
        "html_to_markdown.mac_worker._return_enrichment",
        lambda *args: returned.append(args[-1].read_text()),
    )

    def run_worker() -> dict:
        return worker_once(
            host="test.invalid",
            remote_executable="/private/cli",
            remote_queue="/private/queue",
            output=tmp_path,
            transport=Path("/private/transport"),
        )

    run_worker()
    assert not returned
    run_worker()
    assert returned == ["complete"] and len(captures) == 1
    run_worker()
    assert len(batches) == 2 and len(returned) == 1


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
    assert "--monitor" in definition["ProgramArguments"][3]


def test_queue_heartbeat_continues_during_long_running_inference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    completed = Event()

    def busy(**kwargs: object) -> dict:
        assert completed.wait(5)
        return {"state": "idle"}

    monkeypatch.setattr("html_to_markdown.mac_worker.worker_once", busy)
    ticks = iter([0, 900, 900, 900, 900])
    monkeypatch.setattr("html_to_markdown.mac_worker.time.monotonic", lambda: next(ticks, 900))

    def check(args: list[str], **kwargs: object):
        assert "list-review-queues" in args
        completed.set()
        return SimpleNamespace(stdout=b"[]")

    monkeypatch.setattr("html_to_markdown.mac_worker.subprocess.run", check)
    status = monitored_worker(
        host="test.invalid",
        remote_executable="/private/cli",
        remote_queue="/private/queue",
        output=tmp_path,
        transport=Path("/private/transport"),
    )
    assert status["state"] == "idle"
    assert (
        json.loads((tmp_path / "queue-heartbeat.json").read_bytes())["state"] == "inference_active"
    )


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


def test_worker_passes_one_pinned_priority_snapshot_to_reuse_and_review(tmp_path, monkeypatch):
    monkeypatch.setattr("html_to_markdown.mac_worker.platform.system", lambda: "Darwin")
    priority_bytes = json_bytes(
        {"schema_version": 1, "identities": ["my-f5-com:K000200"], "urls": []}
    )
    (tmp_path / FILENAME).write_bytes(priority_bytes)
    choices = []

    def command(args, **_kwargs):
        if "export-policy-runtime" in args:
            return SimpleNamespace(
                stdout=json_bytes({"state": "policy_current", **installed_policy_pins()})
            )
        if "list-review-queues" in args:
            return SimpleNamespace(
                stdout=json_bytes(
                    [
                        {
                            "queue": "run/my-f5-com",
                            "fingerprint": "a" * 64,
                            "captured": 1,
                            "discovered": 1,
                        }
                    ]
                )
            )
        return SimpleNamespace(stdout=b'{"state":"waiting_for_source_reviews"}')

    def receive(*args):
        path = args[-1]
        path.mkdir(parents=True)
        (path / "inventory.json").write_text("{}")

    def review(*_args, **kwargs):
        if kwargs["phase"] == "all":
            choices.append(kwargs["priorities"])
        return {"exceptions": [], "pending": 0}

    def seal(_capture, _review, path):
        path.mkdir(parents=True)
        (path / "source-review-receipt.json").write_text("{}")

    monkeypatch.setattr("html_to_markdown.mac_worker.subprocess.run", command)
    monkeypatch.setattr("html_to_markdown.mac_worker._receive_capture", receive)
    monkeypatch.setattr("html_to_markdown.mac_worker.run_reviews", review)
    monkeypatch.setattr(
        "html_to_markdown.mac_worker.seed_review_batch",
        lambda *_args, **kwargs: choices.append(kwargs["priorities"]),
    )
    monkeypatch.setattr("html_to_markdown.mac_worker.seal_source", seal)
    monkeypatch.setattr("html_to_markdown.mac_worker._return_source", lambda *_args: None)
    monkeypatch.setattr("html_to_markdown.mac_worker._advance_renewal_pass", lambda *_args: None)
    status = worker_once(
        host="test.invalid",
        remote_executable="/runtime",
        remote_queue="/queue",
        output=tmp_path,
        transport=Path("/transport"),
    )
    assert len(choices) == 2 and choices[0] is choices[1]
    assert choices[0].identities == frozenset({"my-f5-com:K000200"})
    assert status["review_priorities"]["sha256"] == sha(priority_bytes)


def test_ubuntu_orchestration_uses_local_transport_and_explicit_mac_queue(tmp_path, monkeypatch):
    monkeypatch.setattr("html_to_markdown.mac_worker.platform.system", lambda: "Linux")
    monkeypatch.setenv("HTML_TO_MARKDOWN_INFERENCE_QUEUE", str(tmp_path / "inference"))
    calls = []

    def run(args, **_kwargs):
        calls.append(args)
        assert args[:3] == ["/transport", "--host", "local"]
        if "export-policy-runtime" in args:
            return SimpleNamespace(stdout=b'{"state":"policy_current"}')
        return SimpleNamespace(
            stdout=b"[]"
            if "list-review-queues" in args
            else b'{"state":"waiting_for_source_reviews"}'
        )

    monkeypatch.setattr("html_to_markdown.mac_worker.subprocess.run", run)
    report = worker_once(
        host="local",
        remote_executable="/runtime/tool",
        remote_queue="/private/captures",
        output=tmp_path / "worker",
        transport=Path("/transport"),
    )
    assert report["state"] == "idle"
    assert len(calls) == 3
