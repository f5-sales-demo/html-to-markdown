"""Interrupted media seeds cannot erase original correction history or grant admission."""

import copy
import fcntl
import io
import json
import shutil
from dataclasses import replace

import pytest
from PIL import Image
from typer.testing import CliRunner

import html_to_markdown.cross_queue_seed_repair as repair
from html_to_markdown.cli import app
from html_to_markdown.content_inventory import atomic_private
from html_to_markdown.cross_queue_repair_proof import CONFLICT
from html_to_markdown.cross_queue_reviews import _snapshot, seed_review_batch
from html_to_markdown.cross_queue_seed_repair import SeedRepair, repair_seed
from html_to_markdown.curation import json_bytes
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.source_review_jobs import prepare_reviews, run_reviews


def _historical_wrapper(inputs, record, **item_changes):
    wrapper = copy.deepcopy(json.loads(record.read_bytes()))
    wrapper["item"].update(item_changes)
    raw = json_bytes(wrapper)
    path = inputs.review / "history" / (sha(raw) + ".json")
    atomic_private(path, raw)
    return path, wrapper


def test_timestamp_wrapper_requires_exact_proven_blocked_record(failed_seed):
    inputs, record, _, _ = failed_seed
    path, wrapper = _historical_wrapper(inputs, record, captured_at="2026-11-22T04:00:00+00:00")
    raw = path.read_bytes()
    result = repair_seed(inputs)
    aliases = result["history_inventory"]["timestamp_wrapper_aliases"]
    assert aliases == [
        {
            "historical_sha256": sha(raw),
            "proven_blocked_sha256": inputs.current_sha256,
            "differing_fields": ["item.captured_at"],
        }
    ]
    assert path.read_bytes() == raw
    assert wrapper["cross_queue_evidence_blocked"]
    assert json.loads(record.read_bytes())["disposition"] == "queued"
    assert result["inference_calls"] == 0 and not result["publication_approved"]


@pytest.mark.parametrize("mutation", ["budget", "metadata", "missing_timestamp", "seed"])
def test_timestamp_wrapper_cannot_hide_any_other_change(failed_seed, mutation):
    inputs, record, _, _ = failed_seed
    before = record.read_bytes()
    _, wrapper = _historical_wrapper(inputs, record, captured_at="2026-11-22T04:00:00+00:00")
    if mutation == "budget":
        wrapper["privacy_feedback"] = ["unknown correction"]
    elif mutation == "metadata":
        wrapper["item"]["title"] = "Changed title"
    elif mutation == "seed":
        wrapper["cross_queue_seed"]["prior_record"] = "unknown/source/review/documents/x.json"
    else:
        wrapper["item"].pop("captured_at")
    raw = json_bytes(wrapper)
    path = inputs.review / "history" / (sha(raw) + ".json")
    atomic_private(path, raw)
    with pytest.raises(ValueError, match="unproven blocked history"):
        repair_seed(inputs)
    assert record.read_bytes() == before and path.read_bytes() == raw


def test_repair_retains_all_original_attempt_and_feedback_fields(failed_seed):
    inputs, record, original_path, _ = failed_seed
    original = json.loads(original_path.read_bytes())
    fields = {
        "media_observation_feedback": ["first observation requires correction"],
        "media_observation_regions": {"digest": [{"x": 1, "y": 2}]},
        "relevance_feedback": ["original relevance validation failure"],
        "editor_attempts": [{"request_sha256": "a" * 64, "result": "failed"}],
        "privacy_exhausted": True,
    }
    original.update(fields)
    atomic_private(original_path, json_bytes(original))
    pin = sha(original_path.read_bytes())
    custody = inputs.worker / "day-2/my-f5-com/review/reuse-evidence"
    atomic_private(custody / (pin + ".json"), original_path.read_bytes())
    result = repair_seed(replace(inputs, prior_sha256=pin))
    saved = json.loads(record.read_bytes())
    assert {key: saved[key] for key in fields} == fields
    assert saved["privacy_feedback"] == original["privacy_feedback"]
    assert result["editorial_allowances_reset"] is False
    assert (
        saved["item"]
        == json.loads((inputs.capture / "inventory.json").read_bytes())["documents"][0]
    )


def _media_capture(capture):
    inventory = json.loads((capture / "inventory.json").read_bytes())
    item = inventory["documents"][0]
    item.setdefault("captured_at", "2026-11-10T12:00:00Z")
    buffer = io.BytesIO()
    Image.new("RGBA", (9, 7), (10, 20, 30, 40)).save(buffer, format="PNG")
    data = buffer.getvalue()
    relative = "articles/content/my-f5-com/K000123456/assets/example.png"
    atomic_private(capture / relative, data)
    item["media"] = [
        {"path": relative, "sha256": sha(data), "url": "https://my.f5.com/example.png"}
    ]
    atomic_private(capture / "inventory.json", json_bytes(inventory))
    return item["media"][0]["sha256"]


@pytest.fixture
def failed_seed(capture, tmp_path):
    media = _media_capture(capture)
    worker = tmp_path / "worker"
    original_root = worker / "day-1/my-f5-com/review"
    terminal = worker / "day-2/my-f5-com/review"
    current = worker / "day-3/my-f5-com/review"
    current_capture = worker / "day-3/my-f5-com/capture"
    shutil.copytree(capture, current_capture)
    for root in [original_root, terminal, current]:
        prepare_reviews(current_capture, root)
    original_path = next((original_root / "documents").glob("*.json"))
    original = json.loads(original_path.read_bytes())
    original.update(
        privacy_feedback=["original validation feedback"], privacy_ledger_version="source-privacy/2"
    )
    atomic_private(original_path, json_bytes(original))
    original_raw = original_path.read_bytes()
    unused = f"media/{media}-native-2.json"
    atomic_private(original_root / unused, json_bytes({"preserved": "original unused cache"}))
    atomic_private(terminal / unused, json_bytes({"preserved": "different unused encoding"}))
    atomic_private(current / unused, json_bytes({"preserved": "current unused cache"}))
    _snapshot(original_path, terminal / "reuse-evidence" / (sha(original_raw) + ".json"), terminal)
    terminal_path = terminal / "documents" / original_path.name
    blocked = json.loads(terminal_path.read_bytes())
    blocked.update(
        cross_queue_evidence_blocked=True,
        error_class="CrossQueueEvidenceError",
        attempted_binding_sha256=blocked["binding_sha256"],
    )
    atomic_private(terminal_path, json_bytes(blocked))
    atomic_private(
        terminal / "reuse-errors" / (original_path.name + ".json"), json_bytes({"error": CONFLICT})
    )
    terminal_raw = terminal_path.read_bytes()
    _snapshot(terminal_path, current / "reuse-evidence" / (sha(terminal_raw) + ".json"), current)
    blocked["cross_queue_seed"] = {
        "record_sha256": sha(terminal_raw),
        "prior_record": terminal_path.relative_to(worker).as_posix(),
    }
    current_path = current / "documents" / original_path.name
    atomic_private(current_path, json_bytes(blocked))
    queue = tmp_path / "inference"
    queue.mkdir()
    (worker / "worker.lock").touch()
    (queue / "orchestrator.lock").touch()
    inputs = SeedRepair(
        worker,
        current_capture,
        current,
        queue,
        original_path,
        sha(current_path.read_bytes()),
        sha(original_raw),
    )
    return inputs, current_path, original_path, media


@pytest.mark.parametrize("which", ["worker", "inference"])
def test_missing_existing_lock_rejects_before_repair(failed_seed, which):
    inputs, record, _, _ = failed_seed
    path = (
        inputs.worker / "worker.lock"
        if which == "worker"
        else inputs.inference_queue / "orchestrator.lock"
    )
    path.unlink()
    with pytest.raises(ValueError, match="existing orchestrator lock"):
        repair_seed(inputs)
    assert not path.exists()
    assert sha(record.read_bytes()) == inputs.current_sha256


def test_history_directory_link_cannot_escape_evidence_root(failed_seed, tmp_path):
    inputs, record, _, _ = failed_seed
    outside = tmp_path / "outside"
    outside.mkdir()
    (inputs.review / "history").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="destination link"):
        repair_seed(inputs)
    assert not list(outside.iterdir())
    assert sha(record.read_bytes()) == inputs.current_sha256


def test_unused_generation_is_preserved_without_poisoning_fresh_cache(capture, tmp_path):
    media = _media_capture(capture)
    worker = tmp_path / "worker"
    old = worker / "day-1/my-f5-com/review"
    current = worker / "day-2/my-f5-com/review"
    prepare_reviews(capture, old)
    path = next((old / "documents").glob("*.json"))
    original = json.loads(path.read_bytes())
    original["privacy_feedback"] = ["first failed validation"]
    original["privacy_ledger_version"] = "source-privacy/2"
    atomic_private(path, json_bytes(original))
    raw = path.read_bytes()
    relative = f"media/{media}-native-2.json"
    atomic_private(old / relative, b"original unused encoding")
    prepare_reviews(capture, current)
    atomic_private(current / relative, b"current unused encoding")
    result = seed_review_batch(capture, current, worker, limit=1)
    assert result["seeded"] and not result["exceptions"]
    assert (current / relative).read_bytes() == b"current unused encoding"
    assert (
        current / "reuse-media" / sha(raw) / relative
    ).read_bytes() == b"original unused encoding"
    assert (
        json.loads((current / "documents" / path.name).read_bytes())["privacy_feedback"]
        == original["privacy_feedback"]
    )
    assert path.read_bytes() == raw
    assert result["publication_approved"] is False


def test_required_cache_conflict_remains_blocked(capture, tmp_path):
    media = _media_capture(capture)
    worker = tmp_path / "worker"
    old = worker / "day-1/my-f5-com/review"
    current = worker / "day-2/my-f5-com/review"
    prepare_reviews(capture, old)
    record = next((old / "documents").glob("*.json"))
    original = json.loads(record.read_bytes())
    original["privacy_feedback"] = ["original feedback"]
    atomic_private(record, json_bytes(original))
    relative = f"media/{media}-native-3.json"
    atomic_private(old / relative, b"original required encoding")
    prepare_reviews(capture, current)
    atomic_private(current / relative, b"conflicting required encoding")
    result = seed_review_batch(capture, current, worker, limit=1)
    assert result["exceptions"]
    assert json.loads((current / "documents" / record.name).read_bytes())[
        "cross_queue_evidence_blocked"
    ]
    assert (current / relative).read_bytes() == b"conflicting required encoding"


def test_restore_strongest_original_and_preserve_weaker_history(failed_seed):
    inputs, record, original_path, media = failed_seed
    raw = original_path.read_bytes()
    before = record.read_bytes()
    weaker = copy.deepcopy(json.loads(raw))
    weaker.pop("privacy_feedback")
    weaker["disposition"] = "accepted"
    path = inputs.worker / "weaker/my-f5-com/review/documents" / original_path.name
    atomic_private(path, json_bytes(weaker))
    weak_raw = path.read_bytes()
    result = repair_seed(inputs)
    saved = json.loads(record.read_bytes())
    assert saved["privacy_feedback"] == ["original validation feedback"]
    assert not saved.get("cross_queue_evidence_blocked")
    assert saved["disposition"] == "queued" and not saved["phases"]
    assert result["publication_approved"] is False and result["inference_calls"] == 0
    assert original_path.read_bytes() == raw and path.read_bytes() == weak_raw
    assert (inputs.review / "history" / (sha(before) + ".json")).read_bytes() == before
    assert (inputs.review / f"media/{media}-native-2.json").read_bytes() == json_bytes(
        {"preserved": "current unused cache"}
    )
    assert repair_seed(inputs) == result


@pytest.mark.parametrize(
    "mutation",
    [
        "stronger",
        "ambiguous",
        "opaque",
        "missing_custody",
        "changed_source",
        "pending_phase",
        "unknown_blocked",
    ],
)
def test_unproven_or_weaker_history_cannot_clear_block(failed_seed, mutation):
    inputs, record, original_path, _ = failed_seed
    before = record.read_bytes()
    original = json.loads(original_path.read_bytes())
    if mutation in {"stronger", "ambiguous"}:
        other = copy.deepcopy(original)
        other["privacy_feedback"] = (
            ["different feedback", "second"] if mutation == "stronger" else ["different feedback"]
        )
        path = inputs.worker / "other/my-f5-com/review/documents" / record.name
        atomic_private(path, json_bytes(other))
    elif mutation == "unknown_blocked":
        other = copy.deepcopy(original)
        other.pop("privacy_feedback")
        other["cross_queue_evidence_blocked"] = True
        atomic_private(
            inputs.worker / "unknown/my-f5-com/review/documents" / record.name, json_bytes(other)
        )
    elif mutation == "opaque":
        atomic_private(
            inputs.worker / "other/my-f5-com/review/history" / ("a" * 64 + ".json"),
            b"broken history",
        )
    elif mutation == "missing_custody":
        for p in (inputs.worker / "day-2/my-f5-com/review/reuse-evidence").glob("*.json"):
            p.unlink()
    elif mutation == "changed_source":
        saved = json.loads(before)
        saved["binding"]["article_sha256"] = "b" * 64
        saved["binding_sha256"] = sha(json_bytes(saved["binding"]))
        atomic_private(record, json_bytes(saved))
        before = record.read_bytes()
        inputs = replace(inputs, current_sha256=sha(before))
    else:
        saved = json.loads(before)
        saved["boundary_pending"] = {"relevance": {"pending": True}}
        atomic_private(record, json_bytes(saved))
        before = record.read_bytes()
        inputs = replace(inputs, current_sha256=sha(before))
    with pytest.raises((ValueError, KeyError)):
        repair_seed(inputs)
    assert record.read_bytes() == before
    assert json.loads(before)["cross_queue_evidence_blocked"]


@pytest.mark.parametrize("field", ["current_sha256", "prior_sha256"])
def test_stale_pins_reject_before_record_write(failed_seed, field):
    inputs, record, _, _ = failed_seed
    before = record.read_bytes()
    with pytest.raises(ValueError, match="pin is stale"):
        repair_seed(replace(inputs, **{field: "f" * 64}))
    assert record.read_bytes() == before


@pytest.mark.parametrize("which", ["worker", "inference"])
def test_live_orchestrators_cannot_be_repaired(failed_seed, which):
    inputs, record, _, _ = failed_seed
    root, name = (
        (inputs.worker, "worker.lock")
        if which == "worker"
        else (inputs.inference_queue, "orchestrator.lock")
    )
    with (root / name).open("a+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match="idle"):
            repair_seed(inputs)
    assert sha(record.read_bytes()) == inputs.current_sha256


@pytest.mark.parametrize("state", ["queued", "claimed", "unknown"])
def test_pending_or_unknown_inference_prevents_repair(failed_seed, state):
    inputs, record, _, _ = failed_seed
    atomic_private(inputs.inference_queue / "jobs/job/state.json", json_bytes({"state": state}))
    with pytest.raises(ValueError, match="drained"):
        repair_seed(inputs)
    assert sha(record.read_bytes()) == inputs.current_sha256


def test_committed_record_resumes_after_receipt_interruption(failed_seed, monkeypatch):
    inputs, record, original_path, _ = failed_seed
    write = repair.atomic_private

    def interrupted(path, data):
        if path.name == "receipt.json":
            raise OSError("interrupted checkpoint")
        write(path, data)

    monkeypatch.setattr(repair, "atomic_private", interrupted)
    with pytest.raises(OSError):
        repair_seed(inputs)
    committed = record.read_bytes()
    original_raw = original_path.read_bytes()
    monkeypatch.setattr(repair, "atomic_private", write)
    result = repair_seed(inputs)
    assert result["repaired_sha256"] == sha(committed)
    assert record.read_bytes() == committed
    assert original_path.read_bytes() == original_raw


def test_changed_record_after_interruption_is_never_overwritten(failed_seed, monkeypatch):
    inputs, record, _, _ = failed_seed
    write = repair.atomic_private

    def interrupted(path, data):
        if path.name == "receipt.json":
            raise OSError("interrupted checkpoint")
        write(path, data)

    monkeypatch.setattr(repair, "atomic_private", interrupted)
    with pytest.raises(OSError):
        repair_seed(inputs)
    progressed = json.loads(record.read_bytes())
    progressed["reason"] = "later review progressed"
    atomic_private(record, json_bytes(progressed))
    raw = record.read_bytes()
    monkeypatch.setattr(repair, "atomic_private", write)
    with pytest.raises(ValueError, match="pin is stale"):
        repair_seed(inputs)
    assert record.read_bytes() == raw


def test_exhaustion_survives_repair_and_cannot_buy_inference(failed_seed, monkeypatch, tmp_path):
    inputs, record, original_path, _ = failed_seed
    original = json.loads(original_path.read_bytes())
    original["privacy_feedback"].append("final allowed correction failed")
    original["privacy_exhausted"] = True
    old_pin = inputs.prior_sha256
    atomic_private(original_path, json_bytes(original))
    new_pin = sha(original_path.read_bytes())
    custody = inputs.worker / "day-2/my-f5-com/review/reuse-evidence"
    (custody / (old_pin + ".json")).unlink()
    atomic_private(custody / (new_pin + ".json"), original_path.read_bytes())
    inputs = replace(inputs, prior_sha256=new_pin)
    repair_seed(inputs)
    assert json.loads(record.read_bytes())["privacy_exhausted"]

    class NoInference:
        concurrency = 1

        def __init__(self, *args, **kwargs):
            pass

        def execute(self, body):
            pytest.fail("exhaustion cannot grant another proposal")

        def close(self):
            pass

    monkeypatch.setattr("html_to_markdown.source_review_jobs.CodexClient", NoInference)
    result = run_reviews(
        inputs.capture, inputs.review, phase="all", journal=tmp_path / "journal", limit=1
    )
    assert result["exceptions"]
    assert json.loads(record.read_bytes())["privacy_feedback"] == original["privacy_feedback"]


def test_cli_exposes_manual_pinned_repair(failed_seed):
    inputs, record, _, _ = failed_seed
    result = CliRunner().invoke(
        app,
        [
            "repair-review-seed",
            "--worker-root",
            str(inputs.worker),
            "--capture",
            str(inputs.capture),
            "--review",
            str(inputs.review),
            "--inference-queue",
            str(inputs.inference_queue),
            "--prior-record",
            str(inputs.prior_record),
            "--current-sha256",
            inputs.current_sha256,
            "--prior-sha256",
            inputs.prior_sha256,
        ],
    )
    assert result.exit_code == 0, result.output
    assert "independent replay remain required" in result.output
    assert json.loads(record.read_bytes())["disposition"] == "queued"
