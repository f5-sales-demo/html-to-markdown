"""Failed transport receipts must remain retryable and all attempts stay private."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from html_to_markdown.enrichment.codex_client import CodexClient
from html_to_markdown.enrichment.gates import request_hash


def client(tmp_path: Path) -> CodexClient:
    worker = object.__new__(CodexClient)
    worker.executable = "codex"
    worker.route = {
        "name": "LiteLLM",
        "base_url": "https://provider.invalid/v1",
        "env_key": "PRIVATE_EXISTING_KEY",
        "wire_api": "responses",
    }
    worker.version = "test"
    worker.journal = tmp_path
    worker.concurrency = 4
    return worker


def body() -> dict:
    return {
        "model": "gpt-6.1-sol",
        "instructions": "Safe reviewer\nPrompt version: article-enrichment/1",
        "input": [{"content": [{"type": "input_text", "text": "untrusted article"}]}],
        "text": {
            "format": {
                "schema": {
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"],
                    "additionalProperties": False,
                }
            }
        },
    }


def test_transient_failure_retries_without_losing_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    def run(args: list[str], **kwargs: object):
        calls.append(args)
        if len(calls) == 1:
            return SimpleNamespace(
                returncode=1,
                stdout='{"type":"error","message":"429 Too Many Requests"}\n',
                stderr="transient",
            )
        Path(args[args.index("--output-last-message") + 1]).write_text('{"ok":true}')
        return SimpleNamespace(
            returncode=0,
            stdout='{"type":"turn.completed","usage":{"input_tokens":10}}\n',
            stderr="",
        )

    monkeypatch.setattr("html_to_markdown.enrichment.codex_client.subprocess.run", run)
    monkeypatch.setattr("html_to_markdown.enrichment.codex_client.time.sleep", lambda delay: None)
    evidence = client(tmp_path).execute(body())
    assert evidence.error is None and len(calls) == 2
    assert len(list(tmp_path.glob("*.attempt-*.json"))) == 2
    assert all(path.stat().st_mode & 0o077 == 0 for path in tmp_path.iterdir())


def test_failed_cache_is_not_permanent_and_malformed_output_is_not_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    def run(args: list[str], **kwargs: object):
        calls.append(args)
        Path(args[args.index("--output-last-message") + 1]).write_text('{"ok":"wrong type"}')
        return SimpleNamespace(returncode=0, stdout='{"type":"turn.completed"}\n', stderr="")

    monkeypatch.setattr("html_to_markdown.enrichment.codex_client.subprocess.run", run)
    worker = client(tmp_path)
    assert worker.execute(body()).error
    assert worker.execute(body()).error
    assert len(calls) == 2
    assert not (tmp_path / (request_hash(body()) + ".codex.json")).exists()


def test_compaction_and_tools_cannot_be_cached_as_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run(args: list[str], **kwargs: object):
        Path(args[args.index("--output-last-message") + 1]).write_text('{"ok":true}')
        return SimpleNamespace(
            returncode=0,
            stdout='{"type":"context.compacted"}\n{"type":"turn.completed"}\n',
            stderr="",
        )

    monkeypatch.setattr("html_to_markdown.enrichment.codex_client.subprocess.run", run)
    evidence = client(tmp_path).execute(body())
    assert evidence.error
    assert not (tmp_path / (request_hash(body()) + ".codex.json")).exists()


def test_malformed_success_cache_is_preserved_and_replaced_by_valid_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / (request_hash(body()) + ".codex.json")
    cache.write_text('{"incomplete":true}')
    invalid = cache.read_bytes()

    def run(args: list[str], **kwargs: object):
        Path(args[args.index("--output-last-message") + 1]).write_text('{"ok":true}')
        return SimpleNamespace(returncode=0, stdout='{"type":"turn.completed"}\n', stderr="")

    monkeypatch.setattr("html_to_markdown.enrichment.codex_client.subprocess.run", run)
    worker = client(tmp_path)
    assert worker.execute(body()).error is None
    rejected = list(tmp_path.glob("*.rejected-*.json"))
    assert len(rejected) == 1 and rejected[0].read_bytes() == invalid
    monkeypatch.setattr(
        "html_to_markdown.enrichment.codex_client.subprocess.run",
        lambda *args, **kwargs: pytest.fail("valid cache must avoid transport"),
    )
    assert worker.execute(body()).error is None
