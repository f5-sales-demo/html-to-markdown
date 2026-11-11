"""Collection clocks run on Ubuntu while MacBook inference is unavailable."""

from pathlib import Path

import pytest

from html_to_markdown.collection_service import install_collectors


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
