"""Host services activate only after real quiescence and keep credentials external."""

import plistlib
from pathlib import Path
from types import SimpleNamespace

from html_to_markdown.inference_services import (
    activate_inference_agent,
    install_processing_service,
    stage_inference_agent,
)


def test_staged_mac_agent_polls_every_fifteen_minutes_and_stores_no_credentials(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("html_to_markdown.inference_services.platform.system", lambda: "Darwin")
    path = stage_inference_agent(
        host="ubuntu",
        remote_executable="/installed/tool",
        remote_queue="/private/inference",
        output=tmp_path,
        transport=Path("/transport"),
    )
    definition = plistlib.loads(path.read_bytes())
    assert definition["StartInterval"] == 900 and definition["Umask"] == 0o077
    assert "mac-inference-worker" in definition["ProgramArguments"][-1]
    assert "EnvironmentVariables" not in definition
    assert path.stat().st_mode & 0o077 == 0


def test_active_legacy_worker_is_preserved_during_activation(tmp_path, monkeypatch):
    monkeypatch.setattr("html_to_markdown.inference_services.platform.system", lambda: "Darwin")
    calls = []

    def run(args, **_kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="pid = 321\n")

    monkeypatch.setattr("html_to_markdown.inference_services.subprocess.run", run)
    assert activate_inference_agent(tmp_path)["state"] == "waiting_for_live_worker"
    assert len(calls) == 1 and calls[0][1] == "print"


def test_ubuntu_processing_install_preserves_existing_collector_clocks(tmp_path, monkeypatch):
    monkeypatch.setattr("html_to_markdown.inference_services.platform.system", lambda: "Linux")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    units = tmp_path / ".config/systemd/user"
    units.mkdir(parents=True)
    prior = units / "html-to-markdown-official-daily.timer"
    prior.write_bytes(b"existing fixed capture clock")
    calls = []
    monkeypatch.setattr(
        "html_to_markdown.inference_services.subprocess.run", lambda args, **_: calls.append(args)
    )
    result = install_processing_service(
        queue_root=tmp_path / "captures",
        inference_queue=tmp_path / "inference",
        output=tmp_path / "worker",
        executable="/runtime/tool",
        transport=Path("/transport"),
    )
    assert prior.read_bytes() == b"existing fixed capture clock"
    assert "processing-worker" in (units / result["service"]).read_text()
    assert "--inference-queue" in (units / result["service"]).read_text()
    assert "UMask=0077" in (units / result["service"]).read_text()
    assert "OnUnitInactiveSec=900" in (units / result["timer"]).read_text()
    assert calls[-1][-1] == result["timer"]
