"""Planner prerequisites advance through source scheduling without changing approvals."""

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from typer.testing import CliRunner

import html_to_markdown.dependency_source_selection as selection
import html_to_markdown.mac_worker as worker
import html_to_markdown.planner_priorities as scheduling
from html_to_markdown import cli
from html_to_markdown.curation import json_bytes
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.review_priorities import FILENAME, load_priorities


def test_actual_planner_paths_cover_all_sources_and_ignore_diagnostics():
    payload = json_bytes(
        {
            "documents": [
                "content/community-f5-com/t/76323/index.md",
                "content/docs-cloud-f5-com/reference/api-ref/index.md",
                "content/my-f5-com/K000147795/index.md",
                "content/www-f5-com/products/distributed-cloud-services/dns/index.md",
            ],
            "diagnostic": "This untrusted diagnostic must never enter the priority input.",
        }
    )
    assert scheduling.dependency_identities(payload) == [
        "community-f5-com:t/76323",
        "docs-cloud-f5-com:reference/api-ref",
        "my-f5-com:K000147795",
        "www-f5-com:products/distributed-cloud-services/dns",
    ]


@pytest.mark.parametrize(
    "path",
    [
        "content/unknown-source/t/1/index.md",
        "content/community-f5-com/../private/index.md",
        "content/community-f5-com/t/%2e%2e/private/index.md",
        "content/community-f5-com/t//1/index.md",
        "content/community-f5-com/t/./1/index.md",
        "content/community-f5-com/t/1?token=value/index.md",
        "content/community-f5-com/t/1\n/index.md",
        "/content/community-f5-com/t/1/index.md",
        "content/community-f5-com/t/1/assets/image.png",
        12,
    ],
)
def test_invalid_dependency_paths_never_enter_priority_input(path):
    with pytest.raises(ValueError):
        scheduling.dependency_identities(json_bytes({"documents": [path]}))


@pytest.mark.parametrize(
    "payload",
    [
        b'{"documents": [], "documents": []}',
        b'{"documents": "article"}',
        b"[]",
        b"not json",
        b" " * (1024 * 1024 + 1),
    ],
)
def test_invalid_report_is_visible(payload):
    with pytest.raises(ValueError):
        scheduling.dependency_identities(payload)


def test_concurrent_registration_preserves_manual_priorities_and_original_bytes(tmp_path):
    original = json_bytes(
        {
            "schema_version": 1,
            "identities": ["community-f5-com:t/76269"],
            "urls": ["https://docs.cloud.f5.com/docs-v2/reference/api-ref"],
        }
    )
    path = tmp_path / FILENAME
    path.write_bytes(original)
    additions = [f"my-f5-com:K000{index}" for index in range(12)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        receipts = list(
            pool.map(
                lambda identity: scheduling.register_priorities(tmp_path, [identity], "a" * 64),
                additions,
            )
        )
    final = load_priorities(path)
    assert final.identities == frozenset(["community-f5-com:t/76269", *additions])
    assert final.urls == frozenset(["https://docs.cloud.f5.com/docs-v2/reference/api-ref"])
    assert all(receipt["publication_approved"] is False for receipt in receipts)
    assert (
        tmp_path / ".review-priority-updates" / (sha(original) + ".input.json")
    ).read_bytes() == original
    unchanged = path.read_bytes()
    replay = scheduling.register_priorities(tmp_path, additions, "a" * 64)
    assert replay["added"] == 0 and replay["before_sha256"] == replay["after_sha256"]
    assert path.read_bytes() == unchanged


def test_missing_priority_file_is_created_under_original_schema(tmp_path):
    receipt = scheduling.register_priorities(tmp_path, ["community-f5-com:t/76323"], "a" * 64)
    assert receipt["before_sha256"] is None and receipt["added"] == 1
    assert set(json.loads((tmp_path / FILENAME).read_bytes())) == {
        "schema_version",
        "identities",
        "urls",
    }
    assert load_priorities(tmp_path / FILENAME).identities == frozenset(
        {"community-f5-com:t/76323"}
    )


@pytest.mark.parametrize("digest", ["unknown", "a" * 63, "G" * 64])
def test_bad_digest_cannot_mutate_queue(tmp_path, digest):
    with pytest.raises(ValueError, match="digest"):
        scheduling.register_priorities(tmp_path, ["community-f5-com:t/76323"], digest)
    assert not list(tmp_path.iterdir())


def test_invalid_existing_priority_and_links_are_rejected(tmp_path):
    path = tmp_path / FILENAME
    invalid = b'{"schema_version":1,"schema_version":1,"identities":[],"urls":[]}'
    path.write_bytes(invalid)
    with pytest.raises(ValueError):
        scheduling.register_priorities(tmp_path, ["community-f5-com:t/76323"], "a" * 64)
    assert path.read_bytes() == invalid
    path.unlink()
    target = tmp_path / "target"
    target.write_text("{}")
    path.symlink_to(target)
    with pytest.raises(ValueError, match="link"):
        scheduling.register_priorities(tmp_path, ["community-f5-com:t/76323"], "a" * 64)
    assert target.read_text() == "{}"


def test_cli_registers_safe_identities_without_model_work(tmp_path):
    result = CliRunner().invoke(
        cli.app,
        [
            "register-review-priorities",
            "--queue-root",
            str(tmp_path),
            "--dependency-report-sha256",
            "a" * 64,
            "--identity",
            "community-f5-com:t/76323",
            "--identity",
            "my-f5-com:K000147795",
        ],
    )
    assert result.exit_code == 0, result.output
    receipt = json.loads(result.output)
    assert receipt["added"] == 2 and receipt["publication_approved"] is False


def test_transport_uses_exact_arguments_and_requires_matching_receipt(tmp_path, monkeypatch):
    output = tmp_path / "renewal"
    output.mkdir()
    payload = json_bytes({"documents": ["content/community-f5-com/t/76323/index.md"]})
    (output / "dependency-exceptions.json").write_bytes(payload)
    calls = []

    def execute(args, **kwargs):
        calls.append((args, kwargs))
        receipt = {"dependency_report_sha256": sha(payload), "publication_approved": False}
        return subprocess.CompletedProcess(args, 0, json_bytes(receipt), b"")

    monkeypatch.setattr(scheduling.subprocess, "run", execute)
    scheduling.prioritize_dependencies(output, Path("/transport"), "ubuntu", "/runtime", "/queue")
    args, options = calls[0]
    assert args[-2:] == ["--identity", "community-f5-com:t/76323"]
    assert args[:5] == ["/transport", "--host", "ubuntu", "--", "/runtime"]
    assert options["timeout"] == 60 and options["check"] is True
    assert (output / "priority-registration.private.json").exists()
    monkeypatch.setattr(
        scheduling.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            [], 0, b'{"publication_approved":true}', b""
        ),
    )
    with pytest.raises(ValueError, match="receipt"):
        scheduling.prioritize_dependencies(
            output, Path("/transport"), "ubuntu", "/runtime", "/queue"
        )


def test_missing_or_empty_dependencies_need_no_remote_operation(tmp_path, monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("no dependency registration expected")

    monkeypatch.setattr(scheduling.subprocess, "run", unexpected)
    assert (
        scheduling.prioritize_dependencies(
            tmp_path, Path("/transport"), "ubuntu", "/runtime", "/queue"
        )
        is None
    )
    (tmp_path / "dependency-exceptions.json").write_bytes(json_bytes({"documents": []}))
    assert (
        scheduling.prioritize_dependencies(
            tmp_path, Path("/transport"), "ubuntu", "/runtime", "/queue"
        )
        is None
    )


def test_pending_curation_registers_priorities_and_transport_failure_keeps_other_sources_moving(
    tmp_path, monkeypatch
):
    queues = []
    for name in ["first", "second"]:
        directory = tmp_path / name
        (directory / "curation-renewal").mkdir(parents=True)
        (directory / "curation-renewal/dependency-exceptions.json").write_bytes(
            json_bytes({"documents": ["content/community-f5-com/t/76323/index.md"]})
        )
        queues.append((directory, {}))
    monkeypatch.setattr(selection, "selected_inputs", lambda _output: {})
    monkeypatch.setattr(
        worker, "advance_renewals", lambda *args, **kwargs: {"state": "dependency_review_pending"}
    )
    calls = []

    def register(output, *args):
        calls.append(output)
        if len(calls) == 1:
            raise subprocess.CalledProcessError(1, ["register"], stderr=b"private failure")
        return {"publication_approved": False}

    monkeypatch.setattr(worker, "prioritize_dependencies", register)
    status = {}
    worker._advance_renewal_pass(
        queues, status, tmp_path, Path("/transport"), "ubuntu", "/runtime", "/queue"
    )
    assert len(calls) == 2
    assert queues[0][1]["curation"]["review_priorities"]["state"] == "queued"
    assert queues[1][1]["curation"]["state"] == "dependency_review_pending"
    assert queues[1][1]["curation"]["review_priorities"]["publication_approved"] is False
    assert (
        queues[0][0] / "priority-registration-error.stderr.private.bin"
    ).read_bytes() == b"private failure"
