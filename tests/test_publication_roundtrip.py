"""Admission, complete enrichment, private return, packaging and draft form one chain."""

import io
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_combined_sources import admitted
from test_generate_v2 import ModelClient
from test_source_refresh import prepare

from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.enrichment.generate_v2 import generate_v2
from html_to_markdown.package import RELEASE_ASSET_NAMES, verify_publication_receipt
from html_to_markdown.publication_queue import prepare_publication, stage_publication
from html_to_markdown.work_queue import export_queue, list_queues, receive_enrichment, unpack_queue


@pytest.mark.parametrize("interrupted", [False, True])
# Preserve all stage evidence and checks in one transaction.
# pylint: disable-next=too-many-locals,too-many-statements
def test_full_private_publication_chain_replays_and_packages_identically(
    capture: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interrupted: bool
) -> None:
    bundle, pin = admitted(capture, tmp_path, monkeypatch)
    queues = tmp_path / "queues"
    shutil.copytree(bundle, queues / ".reviewed/run/my-f5-com" / pin)
    baseline, _, receipt = prepare(tmp_path / "baseline-input")
    monkeypatch.setattr("html_to_markdown.publication_queue.platform.system", lambda: "Linux")
    monkeypatch.setattr(
        "html_to_markdown.publication_queue.deployed_revision",
        lambda: {"snapshot_tag": "content-20261002T000100Z", "publication_sha256": receipt},
    )
    monkeypatch.setattr(
        "html_to_markdown.publication_queue.shutil.which", lambda name: "/usr/bin/gh"
    )
    calls = []

    def run(args: list[str], **kwargs: object):
        calls.append(args)
        if args[1:3] == ["release", "view"]:
            return SimpleNamespace(returncode=1, stderr="release not found", stdout="", args=args)
        if args[1:3] == ["release", "download"]:
            destination = Path(args[args.index("--dir") + 1])
            source = baseline if args[3] == "content-20261002T000100Z" else job / "combined"
            for name in (*RELEASE_ASSET_NAMES, "publication.json"):
                shutil.copy2(source / name, destination / name)
        return SimpleNamespace(stdout=b"{}")

    monkeypatch.setattr("html_to_markdown.publication_queue.subprocess.run", run)
    job = queues / ".publication/job-test"
    prepared = prepare_publication(queues, job)
    assert prepared["selected_sources"] == {"my-f5-com": pin}
    listed = list_queues(queues)
    assert listed == [
        {
            "queue": ".publication/job-test",
            "source": "all",
            "kind": "enrichment",
            "fingerprint": prepared["curated_sha256"],
            "captured": 0,
            "discovered": 0,
        }
    ]
    stream = io.BytesIO()
    export_queue(queues, ".publication/job-test", stream)
    transfer = tmp_path / "transfer.tar.gz"
    transfer.write_bytes(stream.getvalue())
    mac = tmp_path / "mac"
    unpack_queue(transfer, mac)
    monkeypatch.setattr("html_to_markdown.enrichment.generate_v2.CodexClient", ModelClient)
    monkeypatch.setattr(ModelClient, "calls", [])
    monkeypatch.setattr(ModelClient, "validations", 0)
    monkeypatch.setattr(ModelClient, "fail_until", 0)
    decisions = mac / "enrichment-decisions.json"
    assert (
        generate_v2(mac / "combined", decisions, phase="all", journal=mac / "journal")["validated"]
        == 3
    )
    raw = decisions.read_bytes()
    if interrupted:
        rename = Path.rename

        def interrupt_rename(path: Path, target: Path):
            if (
                path.parent.name.startswith("enrichment-staging-")
                and Path(target) == job / "combined"
            ):
                raise OSError("interruption between directory promotions")
            return rename(path, target)

        monkeypatch.setattr(Path, "rename", interrupt_rename)
        with pytest.raises(OSError, match="interruption"):
            receive_enrichment(queues, ".publication/job-test", sha(raw), io.BytesIO(raw))
        assert not (job / "validated-enrichment.json").exists()
        assert (job / "enrichment-promotion.json").exists()
        monkeypatch.setattr(Path, "rename", rename)
    receive_enrichment(queues, ".publication/job-test", sha(raw), io.BytesIO(raw))
    enrichment = json.loads((job / "combined/enrichment-state.json").read_bytes())
    assert set(enrichment["report"]["model_usage"]) == {"gpt-6.1-sol", "gpt-6-astra"}
    assert sum(value["input_tokens"] for value in enrichment["report"]["model_usage"].values()) > 0
    assert enrichment["report"]["cost_status"] == "unpriced"
    if not interrupted:
        second = queues / ".publication/job-second"
        second_prepared = prepare_publication(queues, second)
        assert second_prepared["curated_sha256"] == prepared["curated_sha256"]
        receive_enrichment(queues, ".publication/job-second", sha(raw), io.BytesIO(raw))
        for name in (
            *RELEASE_ASSET_NAMES,
            "curation-audit.json",
            "enrichment-state.json",
            "refresh-state.json",
        ):
            assert (job / "combined" / name).read_bytes() == (
                second / "combined" / name
            ).read_bytes()
        for file in (job / "combined/content").rglob("*"):
            if file.is_file():
                assert (
                    file.read_bytes()
                    == (
                        second / "combined/content" / file.relative_to(job / "combined/content")
                    ).read_bytes()
                )
        (second / "superseded.json").write_text("{}")
    first = {name: (job / "combined" / name).read_bytes() for name in RELEASE_ASSET_NAMES}
    receive_enrichment(queues, ".publication/job-test", sha(raw), io.BytesIO(raw))
    assert first == {name: (job / "combined" / name).read_bytes() for name in RELEASE_ASSET_NAMES}
    staged = stage_publication(job, source_commit="a" * 40)
    assert staged["baseline_receipt_sha256"] == receipt
    verify = tmp_path / "downloaded-release"
    verify.mkdir()
    for name in (*RELEASE_ASSET_NAMES, "publication.json"):
        shutil.copy2(job / "combined" / name, verify / name)
    verify_publication_receipt(
        verify,
        json.loads((verify / "publication.json").read_bytes()),
        expected_tag=staged["tag"],
        actual_asset_names={path.name for path in verify.iterdir()},
    )
    assert any(args[1:3] == ["release", "create"] and "--draft" in args for args in calls)
    assert not list_queues(queues)


def test_failed_preparation_preserves_evidence_without_blocking_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(root: Path, output: Path):
        (output / "private-error.json").write_text("captured failure evidence")
        raise ValueError("partial input")

    monkeypatch.setattr("html_to_markdown.publication_queue._prepare_publication", fail)
    output = tmp_path / "job"
    with pytest.raises(ValueError, match="partial"):
        prepare_publication(tmp_path / "queues", output)
    assert not output.exists()
    assert (
        next(tmp_path.glob("failed-*/private-error.json")).read_text()
        == "captured failure evidence"
    )
