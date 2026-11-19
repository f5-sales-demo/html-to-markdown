"""Mac keeps bounded current spools until independently verified Ubuntu acknowledgment."""

import json
import subprocess
from pathlib import Path
from threading import Event
from types import SimpleNamespace

from test_codex_worker import body, client

from html_to_markdown import mac_inference
from html_to_markdown.curation import json_bytes
from html_to_markdown.enrichment.queued_client import executor_context
from html_to_markdown.inference_queue import claim, receive, result, submit
from html_to_markdown.mac_inference import inference_once, monitored_inference


def harness(tmp_path, monkeypatch, *, lose_ack=False, corrupt_ack=False):
    queue = tmp_path / "ubuntu"
    output = tmp_path / "mac"
    counts = {"inference": 0, "transfers": 0}
    request = body()
    identity = submit(queue, request, executor_context())
    monkeypatch.setattr("html_to_markdown.mac_inference.platform.system", lambda: "Darwin")
    monkeypatch.setattr(
        "html_to_markdown.mac_inference.synchronize_policy", lambda *_: {"state": "policy_current"}
    )
    monkeypatch.setattr("html_to_markdown.mac_inference.require_active_cohort", lambda *_: None)
    monkeypatch.setattr("html_to_markdown.private_storage._compress", lambda *_: None)
    monkeypatch.setattr("html_to_markdown.private_storage.STORAGE_EVENTS_ROOT", tmp_path / "events")

    def run(args, **kwargs):
        if args[0] == "codex":
            counts["inference"] += 1
            Path(args[args.index("--output-last-message") + 1]).write_text('{"ok":true}')
            return SimpleNamespace(returncode=0, stdout='{"type":"turn.completed"}\n', stderr="")
        if "claim-inference-jobs" in args:
            owner = args[args.index("--owner") + 1]
            items = claim(queue, owner, args[args.index("--executor-sha256") + 1])
            return SimpleNamespace(returncode=0, stdout=json_bytes(items), stderr=b"")
        if "receive-inference-result" in args:
            counts["transfers"] += 1
            receipt = receive(
                queue,
                args[args.index("--job") + 1],
                args[args.index("--owner") + 1],
                args[args.index("--archive-sha256") + 1],
                kwargs["stdin"],
            )
            if lose_ack and counts["transfers"] == 1:
                raise subprocess.CalledProcessError(1, args)
            if corrupt_ack:
                receipt["evidence_file_sha256"] = {}
            return SimpleNamespace(returncode=0, stdout=json_bytes(receipt), stderr=b"")
        raise AssertionError(args)

    monkeypatch.setattr("html_to_markdown.mac_inference.subprocess.run", run)
    return queue, output, identity, request, counts


def invoke(output):
    return inference_once(
        host="ubuntu",
        remote_executable="/runtime/tool",
        remote_queue="/queue",
        output=output,
        transport=Path("/transport"),
        client_factory=client,
    )


def test_completed_mac_job_is_deleted_only_after_full_ubuntu_replay(tmp_path, monkeypatch):
    queue, output, identity, request, counts = harness(tmp_path, monkeypatch)
    report = invoke(output)
    assert report["completed"] == 1 and report["pending"] == 0
    assert result(queue, identity).request == request
    assert counts == {"inference": 1, "transfers": 1}
    assert not list((output / "jobs").glob("*/*/claim.json"))
    receipt = json.loads(next((output / "receipts").glob("*.json")).read_bytes())
    assert receipt["all_evidence_files_verified"] and not receipt["article_approved"]


def test_lost_ack_preserves_spool_and_resumes_without_new_inference(tmp_path, monkeypatch):
    queue, output, identity, request, counts = harness(tmp_path, monkeypatch, lose_ack=True)
    first = invoke(output)
    assert first["pending"] == 1 and first["completed"] == 0
    spool = next((output / "jobs").glob("*/*/claim.json")).parent
    assert (spool / "manifest.json").exists()
    archive = next((output / "transfers").glob("*.tar.gz"))
    before = archive.read_bytes()
    assert result(queue, identity).request == request
    second = invoke(output)
    assert second["completed"] == 1 and second["pending"] == 0
    assert counts == {"inference": 1, "transfers": 2}
    assert not spool.exists()
    assert before


def test_wrong_ack_never_authorizes_local_cleanup(tmp_path, monkeypatch):
    _, output, _, _, counts = harness(tmp_path, monkeypatch, corrupt_ack=True)
    report = invoke(output)
    assert report["pending"] == 1
    assert list((output / "jobs").glob("*/*/claim.json"))
    assert counts["inference"] == 1


def test_changed_local_spool_after_failed_ack_cannot_be_returned_as_same_result(
    tmp_path, monkeypatch
):
    _, output, _, _, counts = harness(tmp_path, monkeypatch, lose_ack=True)
    invoke(output)
    spool = next((output / "jobs").glob("*/*/claim.json")).parent
    (spool / "envelope.json").write_bytes(b"{}")
    report = invoke(output)
    assert report["pending"] == 1 and spool.exists()
    assert counts["inference"] == 1


def test_interrupted_archive_is_preserved_on_ubuntu_and_removed_after_ack(tmp_path, monkeypatch):

    queue, output, identity, _, counts = harness(tmp_path, monkeypatch)
    original = mac_inference.pack_private_tree
    attempts = []

    def pack(job, stream):
        attempts.append(True)
        if len(attempts) == 1:
            stream.write(b"preserved interrupted representation")
            raise OSError("simulated archive interruption")
        original(job, stream)

    monkeypatch.setattr(mac_inference, "pack_private_tree", pack)
    assert invoke(output)["pending"] == 1
    assert invoke(output)["completed"] == 1
    assert counts["inference"] == 1
    state = json.loads((queue / identity / "state.json").read_bytes())
    history = queue / identity / state["payload"] / "transfer-history"
    assert (
        next(history.glob("*/interrupted.tar.gz")).read_bytes()
        == b"preserved interrupted representation"
    )
    assert not list((output / "transfers").glob("*.tar.gz"))


def test_mac_claims_at_most_four_jobs_while_extra_requests_remain_on_ubuntu(tmp_path, monkeypatch):
    queue, output, _, _, counts = harness(tmp_path, monkeypatch)
    for i in range(5):
        request = body()
        request["input"][0]["content"][0]["text"] += str(i)
        submit(queue, request, executor_context())
    report = invoke(output)
    assert report["completed"] == 4 and counts["inference"] == 4
    states = [json.loads(p.read_bytes()) for p in queue.glob("*/*/state.json")]
    assert sum(s["state"] == "queued" for s in states) == 2


def test_inference_queue_is_polled_while_a_model_job_remains_live(tmp_path, monkeypatch):
    done = Event()
    (tmp_path / "owner.json").write_bytes(json_bytes({"owner": "a" * 32}))

    def busy(**_kwargs):
        assert done.wait(5)
        return {"state": "idle"}

    monkeypatch.setattr("html_to_markdown.mac_inference.inference_once", busy)
    ticks = iter([0, 900, 900, 900, 900])
    monkeypatch.setattr("html_to_markdown.mac_inference.time.monotonic", lambda: next(ticks, 900))

    def poll(args, **_kwargs):
        assert "inference-queue-status" in args
        done.set()
        return SimpleNamespace(stdout=b'{"queued":3,"owned":4}')

    monkeypatch.setattr("html_to_markdown.mac_inference.subprocess.run", poll)
    report = monitored_inference(
        host="ubuntu",
        remote_executable="/runtime/tool",
        remote_queue="/inference",
        output=tmp_path,
        transport=Path("/transport"),
    )
    assert report["state"] == "idle"
    assert json.loads((tmp_path / "queue-heartbeat.json").read_bytes())["queue"]["queued"] == 3
