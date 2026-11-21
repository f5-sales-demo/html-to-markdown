"""Pending exact inference jobs must survive unrelated protected-main code upgrades."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from html_to_markdown import inference_executor_contract as executor


def test_sync_checks_actual_installed_executor_and_model_policy_without_policy_export(monkeypatch):
    contract = {"schema_version": 1, "executor_sha256": "a" * 64, "model_policy_sha256": "b" * 64}
    calls = []
    monkeypatch.setattr(executor, "contract", lambda: contract)
    monkeypatch.setattr(executor.platform, "system", lambda: "Darwin")

    def run(args, **kwargs):
        calls.append(args)
        assert kwargs["check"] and kwargs["capture_output"]
        return SimpleNamespace(stdout=json.dumps(contract).encode())

    monkeypatch.setattr(executor.subprocess, "run", run)
    assert (
        executor.synchronize(Path("/trusted/transport"), "Ubuntu", "/trusted/runtime") == contract
    )
    assert calls == [
        [
            "/trusted/transport",
            "--host",
            "Ubuntu",
            "--",
            "/trusted/runtime",
            "export-inference-executor",
        ]
    ]


@pytest.mark.parametrize("change", ["executor_sha256", "model_policy_sha256", "schema_version"])
def test_changed_or_unknown_contract_blocks_inference(monkeypatch, change):
    contract = {"schema_version": 1, "executor_sha256": "a" * 64, "model_policy_sha256": "b" * 64}
    bad = {**contract, change: 2 if change == "schema_version" else "c" * 64}
    monkeypatch.setattr(executor, "contract", lambda: contract)
    monkeypatch.setattr(executor.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        executor.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout=json.dumps(bad).encode()),
    )
    with pytest.raises(ValueError, match="inference executor"):
        executor.synchronize(Path("/trusted/transport"), "Ubuntu", "/trusted/runtime")


def test_unknown_extra_fields_are_rejected(monkeypatch):
    contract = {"schema_version": 1, "executor_sha256": "a" * 64, "model_policy_sha256": "b" * 64}
    monkeypatch.setattr(executor, "contract", lambda: contract)
    monkeypatch.setattr(executor.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        executor.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout=json.dumps({**contract, "tools": ["execution"]}).encode()
        ),
    )
    with pytest.raises(ValueError, match="inference executor"):
        executor.synchronize(Path("/trusted/transport"), "Ubuntu", "/trusted/runtime")


def test_boolean_schema_version_is_not_an_executor_contract(monkeypatch):
    expected = {"schema_version": 1, "executor_sha256": "a" * 64, "model_policy_sha256": "b" * 64}
    monkeypatch.setattr(executor, "contract", lambda: expected)
    monkeypatch.setattr(executor.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        executor.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout=json.dumps({**expected, "schema_version": True}).encode()
        ),
    )
    with pytest.raises(ValueError, match="inference executor"):
        executor.synchronize(Path("/trusted/transport"), "Ubuntu", "/trusted/runtime")
