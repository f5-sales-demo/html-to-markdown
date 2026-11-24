"""Manual refresh survives upgrades without interrupting owned active work."""

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from html_to_markdown import cli
from html_to_markdown.collection_service import install_collectors
from html_to_markdown.inference_services import install_processing_service
from html_to_markdown.operation_mode import (
    FILENAME,
    MARKER,
    TIMERS,
    apply_timer_mode,
    configure_operation_mode,
    operation_mode,
)


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("platform.system", lambda: "Linux")
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda args, **_: calls.append(args))
    return tmp_path / "queue", tmp_path / ".config/systemd/user", calls


def processing(root):
    return install_processing_service(
        queue_root=root,
        inference_queue=root.parent / "inference",
        output=root.parent / "worker",
        executable="/runtime/tool",
        transport=Path("/transport"),
    )


def test_fresh_install_and_reinstall_stay_manual_with_legacy_upgrade_guards(host):
    root, units, calls = host
    for _ in range(2):
        install_collectors(root)
        processing(root)
    assert operation_mode(root) == "manual"
    assert not (root / MARKER).exists()
    assert all("enable" not in call and "start" not in call for call in calls)
    assert {call[-1] for call in calls if "disable" in call} >= {
        "html-to-markdown-full-sunday.timer",
        "html-to-markdown-processing.timer",
    }
    for name in TIMERS:
        assert (units / name).is_file()
        guard = units / (name + ".d") / "90-manual-operation.conf"
        assert 'ConditionPathExists="' + str(root / MARKER) + '"' in guard.read_text()
        assert guard.stat().st_mode & 0o077 == 0


def test_switch_existing_schedule_to_manual_stops_only_owned_timers_and_keeps_evidence(host):
    root, units, calls = host
    configure_operation_mode(root, "scheduled")
    assert not calls and not (root / MARKER).exists()
    install_collectors(root)
    processing(root)
    assert (root / MARKER).is_file() and any("enable" in call for call in calls)
    before = {p.name: p.read_bytes() for p in units.glob("*.service")}
    checkpoint = root / "checkpoint.json"
    checkpoint.write_bytes(b'{"original":"preserved"}')
    unrelated = units / "unrelated.timer"
    unrelated.write_text("existing unrelated timer")
    calls.clear()
    receipt = configure_operation_mode(root, "manual")
    configure_operation_mode(root, "manual")
    assert receipt == {"mode": "manual", "schedules_started": False, "services_stopped": False}
    assert operation_mode(root) == "manual" and not (root / MARKER).exists()
    assert {p.name: p.read_bytes() for p in units.glob("*.service")} == before
    assert checkpoint.read_bytes() == b'{"original":"preserved"}'
    assert unrelated.read_text() == "existing unrelated timer"
    assert all(".service" not in arg for call in calls for arg in call)
    assert all("unrelated.timer" not in call and "enable" not in call for call in calls)
    assert calls[-1][4:] == list(TIMERS)
    assert (root / FILENAME).stat().st_mode & 0o077 == 0


@pytest.mark.parametrize(
    "payload",
    [
        b"[]",
        b"not json",
        b'{"schema_version":true,"mode":"scheduled"}',
        b'{"schema_version":1,"mode":"unknown"}',
        b'{"schema_version":2,"mode":"scheduled"}',
        b'{"schema_version":1,"mode":"manual","mode":"scheduled"}',
        b'{"schema_version":1,"mode":"scheduled","extra":true}',
        b" " * 4097,
    ],
)
def test_invalid_explicit_mode_cannot_activate_either_installer(host, payload):
    root, units, calls = host
    root.mkdir()
    (root / FILENAME).write_bytes(payload)
    for install in (install_collectors, processing):
        with pytest.raises(ValueError):
            install(root)
    assert not calls and not units.exists()
    assert (root / FILENAME).read_bytes() == payload


def test_config_symlink_cannot_be_rewritten_or_used_to_enable_schedules(host):
    root, _, calls = host
    root.mkdir()
    target = root.parent / "original"
    original = b'{"schema_version":1,"mode":"scheduled"}'
    target.write_bytes(original)
    (root / FILENAME).symlink_to(target)
    with pytest.raises(ValueError, match="link"):
        configure_operation_mode(root, "manual")
    with pytest.raises(ValueError, match="link"):
        install_collectors(root)
    assert target.read_bytes() == original and not calls


def test_marker_and_guard_links_fail_visibly_without_scheduler_activation(host):
    root, units, calls = host
    root.mkdir()
    target = root / "untouched"
    target.write_bytes(b"original")
    (root / MARKER).symlink_to(target)
    with pytest.raises(ValueError, match="marker"):
        apply_timer_mode(root, [TIMERS[0]])
    assert not calls and target.read_bytes() == b"original"
    (root / MARKER).unlink()
    units.mkdir(parents=True)
    (units / (TIMERS[0] + ".d")).symlink_to(root)
    with pytest.raises(ValueError, match="directory"):
        apply_timer_mode(root, [TIMERS[0]])
    assert not calls
    configure_operation_mode(root, "scheduled")
    with pytest.raises(ValueError, match="directory"):
        apply_timer_mode(root, [TIMERS[0]])
    assert not (root / MARKER).exists() and not calls


def test_scheduler_failure_retains_manual_intent_and_can_retry(host, monkeypatch):
    root, units, calls = host
    units.mkdir(parents=True)
    (units / TIMERS[0]).write_text("installed timer")

    def fail(args, **kwargs):
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        configure_operation_mode(root, "manual")
    assert operation_mode(root) == "manual" and not (root / MARKER).exists()
    monkeypatch.setattr(subprocess, "run", lambda args, **_: calls.append(args))
    configure_operation_mode(root, "manual")
    assert calls[-1][2:4] == ["disable", "--now"]


def test_unknown_timers_and_mode_cannot_mutate_or_control_host(host):
    root, _, calls = host
    with pytest.raises(ValueError, match="unowned"):
        apply_timer_mode(root, ["unrelated.timer"])
    with pytest.raises(ValueError, match="unknown"):
        configure_operation_mode(root, "unexpected")
    assert not root.exists() and not calls


def test_systemd_guard_escapes_spaces_quotes_and_percent_specifiers(host):
    root, units, _ = host
    root = root.parent / 'private queue%"quoted'
    apply_timer_mode(root, [TIMERS[0]])
    guard = (units / (TIMERS[0] + ".d") / "90-manual-operation.conf").read_text()
    assert 'private queue%%\\"quoted' in guard
    assert not (root / MARKER).exists()


def test_cli_sets_manual_mode_and_exposes_collection_alias(host):
    root, _, calls = host
    runner = CliRunner()
    result = runner.invoke(cli.app, ["configure-operation-mode", "--queue-root", str(root)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["mode"] == "manual" and not calls
    for command in ["collect-content", "scheduled-collection"]:
        result = runner.invoke(cli.app, [command, "--help"])
        assert result.exit_code == 0 and "--source" in result.output
