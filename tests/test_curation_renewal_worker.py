"""Private queue transport replays the actual planner before linked catalog delivery."""

import io
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_curation_renewal_publication import prepared

from html_to_markdown.curation import json_bytes
from html_to_markdown.curation_renewal_delivery import submit_policy
from html_to_markdown.curation_renewal_publication import stage_policy, validate_planner
from html_to_markdown.curation_renewal_worker import advance_renewals, receive_renewals
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.work_queue import pack_private_tree


def test_private_return_replays_actual_planner_before_submission(tmp_path, monkeypatch):
    output, source, _, baseline, pin, client = prepared(tmp_path)
    validated = validate_planner(output, [source], baseline, pin, client)
    shutil.copytree(baseline, output / "baseline" / pin)
    package = {
        "contexts": [source["binding_sha256"]],
        "baseline": pin,
        "qualification_sha256": validated["qualification_sha256"],
    }
    (output / "renewal-package.json").write_bytes(json_bytes(package))
    archive = io.BytesIO()
    pack_private_tree(output, archive)
    calls = []
    monkeypatch.setattr("html_to_markdown.curation_renewal_worker.platform.system", lambda: "Linux")

    def submit(repository, staged, private):
        receipt = json.loads((staged / "curation-renewal-receipt.json").read_bytes())
        calls.append(receipt)
        assert receipt["policy_sha256"] == validated["policy_sha256"]
        return {"issue": 123, "pr": "https://github.com/f5-sales-demo/html-to-markdown/pull/124"}

    monkeypatch.setattr("html_to_markdown.curation_renewal_worker.submit_policy", submit)
    args = [
        tmp_path / "ubuntu",
        sha(json_bytes(package)),
        io.BytesIO(archive.getvalue()),
        tmp_path / "repository",
    ]
    assert receive_renewals(*args)["issue"] == 123
    args[2] = io.BytesIO(archive.getvalue())
    assert receive_renewals(*args)["issue"] == 123
    assert len(calls) == 2
    args[1] = "f" * 64
    args[2] = io.BytesIO(archive.getvalue())
    with pytest.raises(ValueError, match="digest mismatch"):
        receive_renewals(*args)
    assert len(calls) == 2


def test_worker_catalog_transport_uses_verified_baseline_and_sanitized_status(
    tmp_path, monkeypatch
):
    output, source, _, baseline, pin, client = prepared(tmp_path)
    inventory = {
        "documents": [
            {
                "identity": source["identity"],
                "context_path": source["binding_sha256"] + "/context.json",
            }
        ]
    }
    monkeypatch.setattr(
        "html_to_markdown.curation_renewal_worker.prepare_renewals", lambda *args: inventory
    )
    monkeypatch.setattr(
        "html_to_markdown.curation_renewal_worker.review_batch",
        lambda *args: {
            "documents": [
                {"identity": source["identity"], "disposition": "independently_validated"}
            ],
            "exceptions": 0,
        },
    )
    monkeypatch.setattr(
        "html_to_markdown.curation_renewal_worker.deployed_revision",
        lambda: {"publication_sha256": pin, "snapshot_tag": "content-20261002T000100Z"},
    )
    client.close = lambda: None
    monkeypatch.setattr(
        "html_to_markdown.curation_renewal_worker.CodexClient", lambda *args, **kwargs: client
    )
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if Path(args[0]).name == "gh":
            destination = Path(args[args.index("--dir") + 1])
            shutil.copytree(baseline, destination, dirs_exist_ok=True)
            return SimpleNamespace(stdout=b"")
        archive = kwargs["stdin"].read()
        assert b"curl --fail" not in json.dumps(args).encode()
        assert len(archive) > 0 and args[args.index("--package-sha256") + 1] == sha(
            (output / "renewal-package.json").read_bytes()
        )
        return SimpleNamespace(
            stdout=b'{"issue":123,"pr":"https://github.com/f5-sales-demo/html-to-markdown/pull/124"}'
        )

    monkeypatch.setattr("html_to_markdown.curation_renewal_worker.subprocess.run", run)
    result = advance_renewals(
        tmp_path / "capture",
        tmp_path / "review",
        output,
        tmp_path / "journal",
        Path("/private/transport"),
        "worker.example.invalid",
        "/private/executable",
        "/private/queue",
    )
    assert result["state"] == "policy_pr_pending" and result["exceptions"] == 0
    assert Path(calls[0][0]).name == "gh" and calls[0][1:3] == ["release", "download"]
    assert "receive-curation-renewal" in calls[1]


def test_catalog_delivery_creates_linked_pr_once_across_interruption(tmp_path, monkeypatch):
    output, source, _, baseline, pin, client = prepared(tmp_path)
    validate_planner(output, [source], baseline, pin, client)
    staged = tmp_path / "staged"
    stage_policy(output, [source], baseline, pin, staged)
    repo = tmp_path / "repo"
    repo.mkdir()
    shutil.copytree(Path(__file__).parents[1] / "src", repo / "src")
    (repo / "engineering").mkdir()
    actual_run = subprocess.run
    for args in [
        ["git", "init", "--initial-branch=main"],
        ["git", "config", "user.name", "Test"],
        ["git", "config", "user.email", "test@example.invalid"],
        ["git", "remote", "add", "origin", "https://github.com/f5-sales-demo/html-to-markdown.git"],
        ["git", "add", "src"],
        ["git", "commit", "-m", "test: baseline"],
        ["git", "update-ref", "refs/remotes/origin/main", "HEAD"],
    ]:
        actual_run(args, cwd=repo, check=True, capture_output=True)
    gh_calls = []
    fail = True

    def run(root, args):
        nonlocal fail
        if args[:2] in [["git", "fetch"], ["git", "push"]] or args[0] == "bash":
            return b""
        if args[0] != "gh":
            return actual_run(args, cwd=root, check=True, capture_output=True).stdout
        gh_calls.append(args)
        if args[1:3] == ["issue", "create"]:
            return b"https://github.com/f5-sales-demo/html-to-markdown/issues/123\n"
        if args[1:3] == ["pr", "list"]:
            return b"[]"
        if args[1:3] == ["pr", "create"]:
            body = Path(args[args.index("--body-file") + 1]).read_text()
            assert "Closes #123" in body and "#155" in body
            if fail:
                fail = False
                raise subprocess.CalledProcessError(1, args)
            return b"https://github.com/f5-sales-demo/html-to-markdown/pull/124\n"
        assert "--auto" in args and "--squash" in args
        return b""

    monkeypatch.setattr("html_to_markdown.curation_renewal_delivery._run", run)
    monkeypatch.setattr(
        "html_to_markdown.curation_renewal_delivery.deployed_revision",
        lambda: {"publication_sha256": pin},
    )
    monkeypatch.setattr(
        "html_to_markdown.curation_renewal_delivery.platform.system", lambda: "Linux"
    )
    private = tmp_path / "private-delivery"
    with pytest.raises(subprocess.CalledProcessError):
        submit_policy(repo, staged, private)
    assert submit_policy(repo, staged, private)["issue"] == 123
    assert submit_policy(repo, staged, private)["pr"].endswith("/124")
    assert sum(args[1:3] == ["issue", "create"] for args in gh_calls) == 1


@pytest.mark.parametrize("change", ["host", "baseline", "approval", "allowlist"])
def test_invalid_catalog_delivery_prerequisites_cannot_write_github(tmp_path, monkeypatch, change):
    output, source, _, baseline, pin, client = prepared(tmp_path)
    validate_planner(output, [source], baseline, pin, client)
    staged = tmp_path / "staged"
    stage_policy(output, [source], baseline, pin, staged)
    monkeypatch.setattr(
        "html_to_markdown.curation_renewal_delivery.platform.system",
        lambda: "Darwin" if change == "host" else "Linux",
    )
    monkeypatch.setattr(
        "html_to_markdown.curation_renewal_delivery.deployed_revision",
        lambda: {"publication_sha256": "f" * 64 if change == "baseline" else pin},
    )
    calls = []
    monkeypatch.setattr(
        "html_to_markdown.curation_renewal_delivery._run",
        lambda root, args: (
            calls.append(args)
            or (
                b"https://github.com/f5-sales-demo/html-to-markdown.git"
                if args[:3] == ["git", "remote", "get-url"]
                else b"Test"
            )
        ),
    )
    monkeypatch.setattr(
        "html_to_markdown.curation_renewal_delivery._main_policy",
        lambda root: source["policy_sha256"],
    )
    receipt_path = staged / "curation-renewal-receipt.json"
    receipt = json.loads(receipt_path.read_bytes())
    if change == "approval":
        receipt["enforcement_findings"] = 1
    elif change == "allowlist":
        receipt["files"]["../private-evidence.json"] = "a" * 64
    receipt_path.write_bytes(json_bytes(receipt))
    with pytest.raises(ValueError):
        submit_policy(tmp_path / "repo", staged, tmp_path / "delivery")
    assert all(args[0] != "gh" for args in calls)
