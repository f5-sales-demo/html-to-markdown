"""Daily recaptures replay bound prior decisions instead of buying fresh reviews."""

import io
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from test_source_review_jobs import LocalClient
from test_worker_admission_roundtrip import _source_capture

import html_to_markdown.cross_queue_reviews as reuse
import html_to_markdown.source_review_jobs as jobs
from html_to_markdown.content_inventory import atomic_private
from html_to_markdown.cross_queue_reviews import _snapshot, seed_review_batch
from html_to_markdown.curation import json_bytes
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.enrichment.media import all_frame_inputs
from html_to_markdown.mac_worker import worker_once
from html_to_markdown.policy_transport import installed_policy_pins
from html_to_markdown.source_inputs import verify_source
from html_to_markdown.source_review_jobs import prepare_reviews, run_reviews, verify_reviews
from html_to_markdown.work_queue import pack_private_tree, receive_source


def test_unchanged_daily_capture_uses_no_additional_model_calls(
    capture: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests = []

    class CountingClient(LocalClient):
        def execute(self, body):
            requests.append(body)
            return super().execute(body)

    monkeypatch.setattr(jobs, "CodexClient", CountingClient)
    original_inventory = json.loads((capture / "inventory.json").read_bytes())
    original_item = original_inventory["documents"][0]
    original_html = (
        b"<main>" + (capture / original_item["source_html_path"]).read_bytes() + b"</main>"
    )
    original_item["source_html_path"] = "captures/" + sha(original_html) + ".html"
    atomic_private(capture / original_item["source_html_path"], original_html)
    original_item["source_html_sha256"] = sha(original_html)
    (capture / "inventory.json").write_bytes(json_bytes(original_inventory))
    old = tmp_path / "worker/day-1/my-f5-com/review"
    prepare_reviews(capture, old)
    assert not run_reviews(capture, old, phase="all", journal=tmp_path / "journal")["exceptions"]
    assert len(requests) == 3
    record = next((old / "documents").glob("*.json"))
    original = record.read_bytes()
    fresh = tmp_path / "fresh-capture"
    shutil.copytree(capture, fresh)
    inventory = json.loads((fresh / "inventory.json").read_bytes())
    inventory["documents"][0]["captured_at"] = "2026-11-17T00:00:00Z"
    raw_source = b"<html>" + original_html + b"<footer>Fresh navigation</footer></html>"
    inventory["documents"][0]["source_html_path"] = "captures/" + sha(raw_source) + ".html"
    atomic_private(fresh / inventory["documents"][0]["source_html_path"], raw_source)
    inventory["documents"][0]["source_html_sha256"] = sha(raw_source)
    (fresh / "inventory.json").write_bytes(json_bytes(inventory))
    current = tmp_path / "worker/day-2/my-f5-com/review"
    prepare_reviews(fresh, current)
    seed_review_batch(fresh, current, tmp_path / "worker", limit=8)
    assert not verify_reviews(fresh, current)["accepted_files"]
    result = run_reviews(fresh, current, phase="all", journal=tmp_path / "journal")
    assert not result["exceptions"]
    assert len(requests) == 3
    saved = json.loads(next((current / "documents").glob("*.json")).read_bytes())
    assert saved["item"]["captured_at"] == "2026-11-17T00:00:00Z"
    assert record.read_bytes() == original
    assert (
        verify_reviews(fresh, current)["accepted_files"]
        == verify_reviews(capture, old)["accepted_files"]
    )


def _review(capture, output, monkeypatch, client=LocalClient):
    monkeypatch.setattr(jobs, "CodexClient", client)
    prepare_reviews(capture, output)
    return run_reviews(capture, output, phase="all", journal=output.parent / "journal")


def test_changed_source_receives_fresh_independent_review(capture, tmp_path, monkeypatch):

    old = tmp_path / "worker/day-1/my-f5-com/review"
    _review(capture, old, monkeypatch)
    original = next((old / "documents").glob("*.json")).read_bytes()
    inventory = json.loads((capture / "inventory.json").read_bytes())
    item = inventory["documents"][0]
    article = capture / item["article_path"]
    article.write_bytes(article.read_bytes().replace(b"30 seconds", b"10 seconds"))
    item["article_sha256"] = sha(article.read_bytes())
    (capture / "inventory.json").write_bytes(json_bytes(inventory))
    current = tmp_path / "worker/day-2/my-f5-com/review"
    prepare_reviews(capture, current)
    result = seed_review_batch(capture, current, tmp_path / "worker", limit=8)
    assert not result["seeded"]
    assert not verify_reviews(capture, current)["accepted_files"]
    requests = []

    class Counting(LocalClient):
        def execute(self, body):
            requests.append(body)
            return super().execute(body)

    assert not _review(capture, current, monkeypatch, Counting)["exceptions"]
    assert len(requests) == 3
    assert next((old / "documents").glob("*.json")).read_bytes() == original


def test_strongest_prior_privacy_ledger_cannot_be_reset(capture, tmp_path, monkeypatch):
    old = tmp_path / "worker/day-1/my-f5-com/review"
    _review(capture, old, monkeypatch)
    weak = next((old / "documents").glob("*.json"))
    strong_output = tmp_path / "worker/day-0/my-f5-com/review"
    prepare_reviews(capture, strong_output)
    strong = next((strong_output / "documents").glob("*.json"))
    saved = json.loads(strong.read_bytes())
    saved.update(
        privacy_feedback=["first rejection", "second rejection"],
        privacy_exhausted=True,
        error_class="ValueError",
        attempted_binding_sha256=saved["binding_sha256"],
        retryable=False,
    )
    strong.write_bytes(json_bytes(saved))
    originals = [weak.read_bytes(), strong.read_bytes()]
    current = tmp_path / "worker/day-2/my-f5-com/review"
    prepare_reviews(capture, current)
    result = seed_review_batch(capture, current, tmp_path / "worker", limit=8)
    assert len(result["seeded"]) == 1
    seeded = json.loads(next((current / "documents").glob("*.json")).read_bytes())
    assert seeded["privacy_feedback"] == saved["privacy_feedback"] and seeded["privacy_exhausted"]

    class NoInference(LocalClient):
        def execute(self, body):
            pytest.fail("another capture cannot grant a fresh privacy correction")

    report = _review(capture, current, monkeypatch, NoInference)
    assert report["documents"][0]["disposition"] == "queued" and report["exceptions"]
    assert [weak.read_bytes(), strong.read_bytes()] == originals


def test_current_transaction_and_seed_checkpoint_are_preserved(capture, tmp_path, monkeypatch):
    old = tmp_path / "worker/day-1/my-f5-com/review"
    _review(capture, old, monkeypatch)
    current = tmp_path / "worker/day-2/my-f5-com/review"
    prepare_reviews(capture, current)
    first = seed_review_batch(capture, current, tmp_path / "worker", limit=8)
    path = next((current / "documents").glob("*.json"))
    original = path.read_bytes()
    second = seed_review_batch(capture, current, tmp_path / "worker", limit=8)
    assert len(first["seeded"]) == 1 and not second["seeded"]
    assert path.read_bytes() == original
    assert not verify_reviews(capture, current)["accepted_files"]


@pytest.mark.parametrize("bad", ["symlink", "wrong_identity", "wrong_binding", "corrupt_json"])
def test_invalid_prior_evidence_remains_a_visible_exception(capture, tmp_path, monkeypatch, bad):
    old = tmp_path / "worker/day-1/my-f5-com/review"
    _review(capture, old, monkeypatch)
    path = next((old / "documents").glob("*.json"))
    raw = path.read_bytes()
    if bad == "symlink":
        target = tmp_path / "private-record.json"
        target.write_bytes(raw)
        path.unlink()
        path.symlink_to(target)
    elif bad == "corrupt_json":
        path.write_bytes(b"incomplete")
    else:
        saved = json.loads(raw)
        if bad == "wrong_identity":
            saved["item"]["identity"] = "different article"
        else:
            saved["binding_sha256"] = "0" * 64
        path.write_bytes(json_bytes(saved))
    current = tmp_path / "worker/day-2/my-f5-com/review"
    prepare_reviews(capture, current)
    result = seed_review_batch(capture, current, tmp_path / "worker", limit=8)
    assert result["exceptions"] and not result["seeded"]
    assert not verify_reviews(capture, current)["accepted_files"]

    class NoInference(LocalClient):
        def execute(self, body):
            pytest.fail("unknown prior budgets require repair")

    report = _review(capture, current, monkeypatch, NoInference)
    assert report["exceptions"] and report["documents"][0]["disposition"] == "queued"


def test_policy_context_change_does_not_reuse_an_approval(capture, tmp_path, monkeypatch):
    old = tmp_path / "worker/day-1/my-f5-com/review"
    _review(capture, old, monkeypatch)
    monkeypatch.setattr(
        "html_to_markdown.source_review.review_context_digest", lambda: "changed-controller"
    )
    current = tmp_path / "worker/day-2/my-f5-com/review"
    prepare_reviews(capture, current)
    seed_review_batch(capture, current, tmp_path / "worker", limit=8)
    assert not verify_reviews(capture, current)["accepted_files"]
    saved = json.loads(next((current / "documents").glob("*.json")).read_bytes())
    assert "validation" not in saved["phases"]
    assert saved["disposition"] == "queued"


def _add_media(capture: Path, animated: bool) -> tuple[bytes, str]:
    inventory = json.loads((capture / "inventory.json").read_bytes())
    item = inventory["documents"][0]
    raw = io.BytesIO()
    image = Image.new("RGB", (40, 30), "white")
    if animated:
        image.save(
            raw,
            format="GIF",
            save_all=True,
            append_images=[Image.new("RGB", (40, 30), "blue")],
            duration=[50, 100],
            loop=0,
        )
    else:
        image.save(raw, format="PNG")
    data = raw.getvalue()
    digest = sha(data)
    media_path = "articles/content/my-f5-com/K000123456/assets/" + digest
    atomic_private(capture / media_path, data)
    item["media"] = [
        {
            "path": media_path,
            "sha256": digest,
            "url": "https://my.f5.com/example.png",
            "media_type": "image/gif" if animated else "image/png",
        }
    ]
    article = capture / item["article_path"]
    article.write_bytes(
        article.read_bytes() + ("\n![Complete original](assets/" + digest + ")\n").encode()
    )
    item["article_sha256"] = sha(article.read_bytes())
    atomic_private(capture / "inventory.json", json_bytes(inventory))
    return data, media_path


@pytest.mark.parametrize("animated", [False, True])
def test_complete_native_cache_replays_without_new_inference(
    capture, tmp_path, monkeypatch, animated
):

    data, media_path = _add_media(capture, animated)
    monkeypatch.setattr(
        jobs, "all_frame_inputs", lambda path, **kwargs: all_frame_inputs(path, perform_ocr=False)
    )
    requests = []

    class Counting(LocalClient):
        def execute(self, body):
            requests.append(body)
            return super().execute(body)

    old = tmp_path / "worker/day-1/my-f5-com/review"
    assert not _review(capture, old, monkeypatch, Counting)["exceptions"]
    calls = len(requests)
    cache = next((old / "media").glob("*-native-2.json"))
    original_cache = cache.read_bytes()
    current = tmp_path / "worker/day-2/my-f5-com/review"
    prepare_reviews(capture, current)
    seed_review_batch(capture, current, tmp_path / "worker", limit=8)
    assert (current / "media" / cache.name).read_bytes() == original_cache
    assert not verify_reviews(capture, current)["accepted_files"]
    assert not _review(capture, current, monkeypatch, Counting)["exceptions"]
    assert len(requests) == calls
    assert (
        verify_reviews(capture, current)["accepted_files"]
        == verify_reviews(capture, old)["accepted_files"]
    )
    assert cache.read_bytes() == original_cache and (capture / media_path).read_bytes() == data
    corrupted = json.loads((current / "media" / cache.name).read_bytes())
    corrupted[0][1]["duration"] = 999
    atomic_private(current / "media" / cache.name, json_bytes(corrupted))
    with pytest.raises(ValueError, match="cached"):
        verify_reviews(capture, current)
    assert cache.read_bytes() == original_cache


def test_reuse_destination_parent_link_is_rejected(capture, tmp_path, monkeypatch):
    old = tmp_path / "worker/day-1/my-f5-com/review"
    _review(capture, old, monkeypatch)
    current = tmp_path / "worker/day-2/my-f5-com/review"
    prepare_reviews(capture, current)
    target = tmp_path / "outside"
    target.mkdir()
    (current / "reuse-evidence").symlink_to(target, target_is_directory=True)
    result = seed_review_batch(capture, current, tmp_path / "worker", limit=8)
    assert result["exceptions"] and not list(target.iterdir())
    assert not verify_reviews(capture, current)["accepted_files"]


def test_interrupted_copy_resumes_before_new_model_attempts(capture, tmp_path, monkeypatch):

    old = tmp_path / "worker/day-1/my-f5-com/review"
    _review(capture, old, monkeypatch)
    original = next((old / "documents").glob("*.json")).read_bytes()
    current = tmp_path / "worker/day-2/my-f5-com/review"
    prepare_reviews(capture, current)
    snapshot = _snapshot

    def interrupted(*args):
        raise OSError("simulated storage interruption")

    monkeypatch.setattr(reuse, "_snapshot", interrupted)
    failure = seed_review_batch(capture, current, tmp_path / "worker", limit=8)
    assert failure["exceptions"]

    class NoInference(LocalClient):
        def execute(self, body):
            pytest.fail("a storage failure cannot grant new privacy attempts")

    assert _review(capture, current, monkeypatch, NoInference)["exceptions"]
    monkeypatch.setattr(reuse, "_snapshot", snapshot)
    assert seed_review_batch(capture, current, tmp_path / "worker", limit=8)["seeded"]
    assert not _review(capture, current, monkeypatch, NoInference)["exceptions"]
    assert next((old / "documents").glob("*.json")).read_bytes() == original


def test_unknown_prior_budget_stays_blocked_after_controller_upgrade(
    capture, tmp_path, monkeypatch
):
    old = tmp_path / "worker/day-1/my-f5-com/review"
    _review(capture, old, monkeypatch)
    next((old / "documents").glob("*.json")).write_bytes(b"broken evidence")
    current = tmp_path / "worker/day-2/my-f5-com/review"
    prepare_reviews(capture, current)
    seed_review_batch(capture, current, tmp_path / "worker", limit=8)
    monkeypatch.setattr(
        "html_to_markdown.source_review.review_context_digest", lambda: "new-controller"
    )
    prepare_reviews(capture, current)
    saved = json.loads(next((current / "documents").glob("*.json")).read_bytes())
    assert saved["cross_queue_evidence_blocked"]

    class NoInference(LocalClient):
        def execute(self, body):
            pytest.fail("a controller upgrade cannot reset an unknown prior budget")

    assert _review(capture, current, monkeypatch, NoInference)["exceptions"]


@pytest.mark.parametrize(
    "source", ["docs-cloud-f5-com", "community-f5-com", "my-f5-com", "www-f5-com"]
)
def test_worker_reuses_daily_evidence_and_returns_fresh_verified_source(
    capture, tmp_path, monkeypatch, source
):
    if source == "community-f5-com":
        inventory = json.loads((capture / "inventory.json").read_bytes())
        item = inventory["documents"][0]
        raw = json_bytes(
            {
                "post_stream": {
                    "posts": [
                        {
                            "post_number": 1,
                            "cooked": (capture / item["source_html_path"]).read_text(),
                        }
                    ]
                }
            }
        )
        item["source_html_path"] = "captures/" + sha(raw) + ".html"
        atomic_private(capture / item["source_html_path"], raw)
        item["source_html_sha256"] = sha(raw)
        atomic_private(capture / "inventory.json", json_bytes(inventory))
    source_capture = _source_capture(
        capture,
        tmp_path / "source",
        source,
        "https://community.f5.com/t/123456" if source == "community-f5-com" else None,
    )
    monkeypatch.setattr("html_to_markdown.mac_worker.platform.system", lambda: "Darwin")
    requests = []
    returned = []
    day = [1]

    class Counting(LocalClient):
        def execute(self, body):
            requests.append(body)
            return super().execute(body)

    monkeypatch.setattr(jobs, "CodexClient", Counting)
    renewal_inputs = []

    def renewals(*args, **kwargs):
        selected, pin = kwargs["source_inputs"][source]
        renewal_inputs.append(verify_source(selected, pin))
        return {"state": "not_needed"}

    monkeypatch.setattr("html_to_markdown.mac_worker.advance_renewals", renewals)

    def route(args, **kwargs):
        identity = "day-" + str(day[0]) + "/" + source
        if "export-policy-runtime" in args:
            return SimpleNamespace(
                stdout=json_bytes({"state": "policy_current", **installed_policy_pins()})
            )
        if "list-review-queues" in args:
            return SimpleNamespace(
                stdout=json_bytes(
                    [
                        {
                            "queue": identity,
                            "source": source,
                            "fingerprint": str(day[0]) * 64,
                            "captured": 1,
                            "discovered": 1,
                        }
                    ]
                )
            )
        if "export-review-queue" in args:
            pack_private_tree(source_capture, kwargs["stdout"])
        elif "receive-review-input" in args:
            pin = args[args.index("--review-sha256") + 1]
            target = receive_source(
                tmp_path / "ubuntu", identity, pin, io.BytesIO(kwargs["stdin"].read())
            )
            returned.append(verify_source(target, pin))
        return SimpleNamespace(stdout=b'{"state":"waiting_for_source_reviews"}')

    monkeypatch.setattr("html_to_markdown.mac_worker.subprocess.run", route)

    def run_worker():
        return worker_once(
            host="test.invalid",
            remote_executable="/private/cli",
            remote_queue="/private/queue",
            output=tmp_path / "worker",
            transport=Path("/private/transport"),
        )

    assert run_worker()["state"] == "idle" and len(requests) == 3
    day[0] = 2
    inventory = json.loads((source_capture / "inventory.json").read_bytes())
    inventory["documents"][0]["captured_at"] = "2026-11-17T00:00:00Z"
    inventory["ended_at"] = "2026-11-17T00:01:00Z"
    atomic_private(source_capture / "inventory.json", json_bytes(inventory))
    assert run_worker()["state"] == "idle" and len(requests) == 3
    assert len(returned) == 2
    assert returned[0]["accepted_files"] == returned[1]["accepted_files"]
    assert returned[0]["inventory_sha256"] != returned[1]["inventory_sha256"]
    assert returned[1]["documents"][0]["captured_at"] == "2026-11-17T00:00:00Z"
    assert len(renewal_inputs) == 2
    assert renewal_inputs[0] == returned[0] and renewal_inputs[1] == returned[1]


def test_empty_newer_inventory_does_not_hide_complete_prior_evidence(
    capture, tmp_path, monkeypatch
):
    old = tmp_path / "worker/day-1/my-f5-com/review"
    _review(capture, old, monkeypatch)
    prepare_reviews(capture, tmp_path / "worker/day-2/my-f5-com/review")
    current = tmp_path / "worker/day-3/my-f5-com/review"
    prepare_reviews(capture, current)
    assert seed_review_batch(capture, current, tmp_path / "worker", limit=8)["seeded"]

    class NoInference(LocalClient):
        def execute(self, body):
            pytest.fail("an empty newer record must not hide complete evidence")

    assert not _review(capture, current, monkeypatch, NoInference)["exceptions"]


def test_pending_third_privacy_proposal_crosses_queues_without_another_editor(
    capture, tmp_path, monkeypatch
):
    validations = []

    class Correcting(LocalClient):
        def execute(self, body):
            response = super().execute(body)
            if body["text"]["format"]["name"] == "admissionvalidation":
                validations.append(body)
                if len(validations) <= 2:
                    raw = response.response
                    result = json.loads(raw["output"][0]["content"][0]["text"])
                    result.update(
                        approved=False,
                        privacy_valid=False,
                        failures=["privacy proposal needs a complete independent review"],
                    )
                    raw["output"][0]["content"][0]["text"] = json.dumps(result)
                    response = response.model_copy(
                        update={
                            "response_sha256": sha(
                                json.dumps(raw, sort_keys=True, separators=(",", ":"))
                            )
                        }
                    )
            return response

    old = tmp_path / "worker/day-1/my-f5-com/review"
    assert not _review(capture, old, monkeypatch, Correcting)["exceptions"]
    path = next((old / "documents").glob("*.json"))
    saved = json.loads(path.read_bytes())
    assert len(saved["privacy_feedback"]) == 2
    saved["phases"].pop("validation")
    saved.update(
        disposition="queued",
        reason="source_curation_requires_review",
        privacy_proposal_pending_validation=True,
    )
    atomic_private(path, json_bytes(saved))
    original = path.read_bytes()
    current = tmp_path / "worker/day-2/my-f5-com/review"
    prepare_reviews(capture, current)
    seed_review_batch(capture, current, tmp_path / "worker", limit=8)
    calls = []

    class NoEditor(LocalClient):
        def execute(self, body):
            assert body["metadata"]["content_review_role"] == "validator"
            calls.append(body)
            return super().execute(body)

    report = _review(capture, current, monkeypatch, NoEditor)
    assert not report["exceptions"] and len(calls) == 1
    assert (
        json.loads(next((current / "documents").glob("*.json")).read_bytes())["privacy_feedback"]
        == saved["privacy_feedback"]
    )
    assert path.read_bytes() == original
