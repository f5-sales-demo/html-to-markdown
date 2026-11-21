"""Finished results and dependent jobs advance while a slower peer is still live."""

import json
from threading import Event, Lock

from test_codex_worker import body
from test_mac_inference import Clock, harness, monitor

from html_to_markdown import mac_inference
from html_to_markdown.enrichment.queued_client import executor_context
from html_to_markdown.inference_queue import result, submit


def test_fast_receipt_and_followup_advance_before_slow_peer_finishes(tmp_path, monkeypatch):
    queue, output, fast, _, counts = harness(tmp_path, monkeypatch)
    roles = {}
    for role in ["slow", "peer-a", "peer-b"]:
        request = body()
        request["input"][0]["content"][0]["text"] += " " + role
        roles[role] = submit(queue, request, executor_context())
    slow_started, fast_received, followup_started = Event(), Event(), Event()
    timed_out = Event()
    guard = Lock()
    activity = {"live": 0, "peak": 0, "claimed_peak": 0}
    followups = []
    original_execute, original_run = mac_inference._execute, mac_inference.subprocess.run

    def execute(job, client):
        identity = json.loads((job / "claim.json").read_bytes())["job"]
        with guard:
            activity["live"] += 1
            activity["peak"] = max(activity["peak"], activity["live"])
        try:
            if identity == roles["slow"]:
                slow_started.set()
                if not followup_started.wait(5):
                    timed_out.set()
                    raise AssertionError("slow peer blocked the completed result and follow-up")
            elif identity == fast:
                assert slow_started.wait(5)
            elif identity in followups:
                assert fast_received.is_set()
                followup_started.set()
            original_execute(job, client)
        finally:
            with guard:
                activity["live"] -= 1

    def transport(args, **kwargs):
        response = original_run(args, **kwargs)
        if "receive-inference-result" in args and args[args.index("--job") + 1] == fast:
            assert result(queue, fast) is not None
            request = body()
            request["input"][0]["content"][0]["text"] += " dependent follow-up"
            followups.append(submit(queue, request, executor_context()))
            fast_received.set()
        if "claim-inference-jobs" in args:
            with guard:
                claimed = sum(
                    json.loads(path.read_bytes())["state"] == "claimed"
                    for path in queue.glob("*/*/state.json")
                )
                activity["claimed_peak"] = max(activity["claimed_peak"], claimed)
        return response

    clock = Clock()
    monkeypatch.setattr(mac_inference, "_execute", execute)
    monkeypatch.setattr(mac_inference.subprocess, "run", transport)
    monkeypatch.setattr(mac_inference.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(mac_inference.time, "sleep", clock.sleep)
    report = monitor(output)
    assert not timed_out.is_set()
    assert fast_received.is_set() and followup_started.is_set()
    assert report["completed"] == 5 and report["pending"] == 0
    assert not report["errors"]
    assert 2 <= activity["peak"] <= 4 and activity["claimed_peak"] <= 4
    assert counts == {"inference": 5, "transfers": 5}
    assert not list((output / "jobs").glob("*/*/claim.json"))
    assert result(queue, followups[0]) is not None


def test_duplicate_claims_are_rejected_before_spool_mutation(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import pytest

    from html_to_markdown.curation import json_bytes
    from html_to_markdown.enrichment.gates import request_hash

    owner = "a" * 32
    request = body()
    claim = {
        "job": request_hash(request) + "/000001",
        "owner": owner,
        "executor_sha256": executor_context(),
        "request": request,
    }
    monkeypatch.setattr(
        mac_inference.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=json_bytes([claim, claim])),
    )
    with pytest.raises(ValueError, match="duplicate job"):
        mac_inference._load_jobs(
            tmp_path,
            owner,
            host="ubuntu",
            executable="/runtime/tool",
            queue="/queue",
            transport=tmp_path / "transport",
        )
    assert not list((tmp_path / "jobs").glob("*/*/claim.json"))


def test_oversized_refill_is_rejected_before_any_new_spool_is_written(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import pytest

    from html_to_markdown.curation import json_bytes
    from html_to_markdown.enrichment.gates import request_hash

    owner = "a" * 32
    claims = []
    for index in range(4):
        request = body()
        request["input"][0]["content"][0]["text"] += " new " + str(index)
        claims.append(
            {
                "job": request_hash(request) + "/000001",
                "owner": owner,
                "executor_sha256": executor_context(),
                "request": request,
            }
        )
    in_flight = frozenset(tmp_path / "jobs" / (str(index) * 64) / "000001" for index in range(3))

    def malformed(args, **_kwargs):
        assert args[args.index("--new-limit") + 1] == "1"
        return SimpleNamespace(stdout=json_bytes(claims))

    monkeypatch.setattr(mac_inference.subprocess, "run", malformed)
    with pytest.raises(ValueError, match="four active jobs"):
        mac_inference._load_jobs(
            tmp_path,
            owner,
            host="ubuntu",
            executable="/runtime/tool",
            queue="/queue",
            transport=tmp_path / "transport",
            in_flight=in_flight,
        )
    assert not list((tmp_path / "jobs").glob("*/*/claim.json"))
