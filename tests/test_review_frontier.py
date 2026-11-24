"""Current inventories progress without losing historical identities or evidence."""

import json
import shutil
import sqlite3

import pytest
from test_baseline_source_review import Client

from html_to_markdown import baseline_global_ledgers as ledger
from html_to_markdown import baseline_source_review as source_review
from html_to_markdown.baseline_late_validation import _execution_digest
from html_to_markdown.curation import json_bytes
from html_to_markdown.work_queue import list_queues

pytest_plugins = ["test_baseline_source_review"]


def queue(root, name, source, identities, started_at, *, complete=True):
    path = root / name / source
    path.mkdir(parents=True)
    (path / "capture-binding.json").write_bytes(
        json_bytes({"started_at": started_at, "request": {"source": source}})
    )
    con = sqlite3.connect(path / "inventory.sqlite")
    con.executescript(
        "CREATE TABLE documents(url TEXT,data TEXT);CREATE TABLE phases(source TEXT,data TEXT);"
    )
    for identity in identities:
        item = {
            "identity": source + ":" + identity,
            "url": "https://my.f5.com/manage/s/article/" + identity,
            "capture_complete": complete,
            "article_sha256": "a" * 64,
            "disposition": "queued",
        }
        con.execute("INSERT INTO documents VALUES (?,?)", (item["url"], json.dumps(item)))
    con.execute(
        "INSERT INTO phases VALUES (?,?)", (source, json.dumps({"enumeration_complete": complete}))
    )
    con.commit()
    con.close()
    return path


def test_latest_inventory_covers_old_but_default_listing_preserves_both(tmp_path):
    root = tmp_path / "queue"
    older = queue(root, "z-old", "my-f5-com", ["one"], "2026-11-19T00:00:00Z")
    queue(root, "a-new", "my-f5-com", ["one", "two"], "2026-11-20T00:00:00Z")
    old_bytes = (older / "inventory.sqlite").read_bytes()
    assert len(list_queues(root)) == 2
    selected = list_queues(root, review_frontier=True)
    assert [r["queue"] for r in selected] == ["a-new/my-f5-com"]
    assert (older / "inventory.sqlite").read_bytes() == old_bytes
    evidence = json.loads((root / "review-frontier.private.json").read_bytes())
    assert evidence["deferred"][0]["queue"] == "z-old/my-f5-com"
    assert evidence["deferred"][0]["covered_identities"] == 1
    assert not evidence["source_approval_granted"]


def test_older_only_identity_remains_in_frontier_with_partial_capture(tmp_path):
    root = tmp_path / "queue"
    queue(root, "old", "my-f5-com", ["one", "missing"], "2026-11-19T00:00:00Z")
    queue(root, "new", "my-f5-com", ["one"], "2026-11-20T00:00:00Z", complete=False)
    selected = list_queues(root, review_frontier=True)
    assert [r["queue"] for r in selected] == ["new/my-f5-com", "old/my-f5-com"]
    assert selected[0]["captured"] == 0 and selected[1]["captured"] == 2
    assert json.loads((root / "review-frontier.private.json").read_bytes())["deferred"] == []


@pytest.mark.parametrize("epoch", [None, "invalid", "2026-11-19"])
def test_unknown_or_ambiguous_epoch_cannot_claim_supersession(tmp_path, epoch):
    root = tmp_path / "queue"
    queue(root, "unknown", "my-f5-com", ["one"], epoch)
    queue(root, "new", "my-f5-com", ["one"], "2026-11-20T00:00:00Z")
    selected = list_queues(root, review_frontier=True)
    assert len(selected) == 2
    evidence = json.loads((root / "review-frontier.private.json").read_bytes())
    assert evidence["deferred"] == []
    assert evidence["uncertain"] == ["unknown/my-f5-com"]


def test_coverage_is_source_specific_and_pending_enrichment_is_preserved(tmp_path):
    root = tmp_path / "queue"
    queue(root, "old", "my-f5-com", ["one"], "2026-11-19T00:00:00Z")
    queue(root, "new", "my-f5-com", ["one"], "2026-11-20T00:00:00Z")
    queue(root, "old", "www-f5-com", ["one"], "2026-11-19T00:00:00Z")
    job = root / ".publication" / ("a" * 64)
    job.mkdir(parents=True)
    (job / "publication-job.json").write_bytes(json_bytes({"curated_sha256": "b" * 64}))
    selected = list_queues(root, review_frontier=True)
    assert {r["queue"] for r in selected} == {
        "new/my-f5-com",
        "old/www-f5-com",
        ".publication/" + "a" * 64,
    }
    assert selected[-1]["kind"] == "enrichment"


def test_equal_capture_epochs_do_not_guess_which_source_is_current(tmp_path):
    root = tmp_path / "queue"
    queue(root, "one", "my-f5-com", ["same"], "2026-11-20T00:00:00Z")
    queue(root, "two", "my-f5-com", ["same"], "2026-11-20T00:00:00Z")
    selected = list_queues(root, review_frontier=True)
    assert len(selected) == 2
    assert json.loads((root / "review-frontier.private.json").read_bytes())["deferred"] == []


def test_changed_body_and_failed_current_capture_keep_current_dispositions(tmp_path):
    root = tmp_path / "queue"
    older = queue(root, "old", "my-f5-com", ["same"], "2026-11-19T00:00:00Z")
    latest = queue(root, "new", "my-f5-com", ["same"], "2026-11-20T00:00:00Z", complete=False)
    con = sqlite3.connect(latest / "inventory.sqlite")
    data = json.loads(con.execute("SELECT data FROM documents").fetchone()[0])
    data.update(article_sha256="b" * 64, disposition="capture_error")
    con.execute("UPDATE documents SET data=?", (json.dumps(data),))
    con.commit()
    con.close()
    before = (older / "inventory.sqlite").read_bytes()
    assert [r["queue"] for r in list_queues(root, review_frontier=True)] == ["new/my-f5-com"]
    con = sqlite3.connect(latest / "inventory.sqlite")
    assert (
        json.loads(con.execute("SELECT data FROM documents").fetchone()[0])["disposition"]
        == "capture_error"
    )
    con.close()
    assert (older / "inventory.sqlite").read_bytes() == before


def test_deferred_source_still_supplies_strongest_retained_privacy_ledger(prepared, tmp_path):
    _, prepared_output, prepared_record, _ = prepared
    captures = tmp_path / "queue"
    queue(captures, "old", "docs-cloud-f5-com", ["same"], "2026-11-19T00:00:00Z")
    queue(captures, "new", "docs-cloud-f5-com", ["same"], "2026-11-20T00:00:00Z")
    worker = tmp_path / "worker"
    old = worker / "old/docs-cloud-f5-com/curation-renewal/dependency-privacy"
    new = worker / "new/docs-cloud-f5-com/curation-renewal/dependency-privacy"
    shutil.copytree(prepared_output, old)
    shutil.copytree(prepared_output, new)
    previous_record = old / "documents" / prepared_record.name
    current_record = new / "documents" / prepared_record.name
    with pytest.raises(ValueError, match="exhausted"):
        source_review.review_baseline_privacy(old, previous_record, Client(reject=True))
    previous = previous_record.read_bytes()
    assert [entry["queue"] for entry in list_queues(captures, review_frontier=True)] == [
        "new/docs-cloud-f5-com"
    ]
    result = ledger.reconcile_record(new, current_record, worker, _execution_digest())
    renewed = json.loads(current_record.read_bytes())
    assert result["strongest_feedback_entries"] == 3
    assert renewed["existing_proposal_pending"]["new_editor_proposals"] == 0
    assert previous_record.read_bytes() == previous
    assert not result["article_approved"]


def capture_state(path, identity, *, complete, disposition="queued"):
    con = sqlite3.connect(path / "inventory.sqlite")
    rows = con.execute("SELECT rowid,data FROM documents").fetchall()
    for rowid, raw in rows:
        item = json.loads(raw)
        if item["identity"].endswith(":" + identity):
            item.update(capture_complete=complete, disposition=disposition)
            con.execute("UPDATE documents SET data=? WHERE rowid=?", (json.dumps(item), rowid))
    con.commit()
    con.close()


def test_matching_pending_inventory_cannot_hide_completed_capture(tmp_path):
    root = tmp_path / "queue"
    older = queue(root, "old", "my-f5-com", ["same"], "2026-11-19T00:00:00Z")
    queue(root, "new", "my-f5-com", ["same"], "2026-11-20T00:00:00Z", complete=False)
    before = (older / "inventory.sqlite").read_bytes()
    selected = list_queues(root, review_frontier=True)
    assert [entry["queue"] for entry in selected] == ["new/my-f5-com", "old/my-f5-com"]
    assert (older / "inventory.sqlite").read_bytes() == before
    assert json.loads((root / "review-frontier.private.json").read_bytes())["deferred"] == []


def test_partial_capture_completion_releases_older_queue(tmp_path):
    root = tmp_path / "queue"
    older = queue(root, "old", "my-f5-com", ["one", "two"], "2026-11-19T00:00:00Z")
    latest = queue(root, "new", "my-f5-com", ["one", "two"], "2026-11-20T00:00:00Z", complete=False)
    before = (older / "inventory.sqlite").read_bytes()
    capture_state(latest, "one", complete=True)
    partial = list_queues(root, review_frontier=True)
    assert [entry["queue"] for entry in partial] == ["new/my-f5-com", "old/my-f5-com"]
    capture_state(latest, "two", complete=True)
    complete = list_queues(root, review_frontier=True)
    assert [entry["queue"] for entry in complete] == ["new/my-f5-com"]
    assert partial[0]["fingerprint"] != complete[0]["fingerprint"]
    assert (older / "inventory.sqlite").read_bytes() == before


@pytest.mark.parametrize("disposition", ["capture_error", "access_queue", "policy_excluded"])
def test_explicit_new_capture_outcomes_keep_current_failure_or_policy_state(tmp_path, disposition):
    root = tmp_path / "queue"
    queue(root, "old", "my-f5-com", ["same"], "2026-11-19T00:00:00Z")
    latest = queue(root, "new", "my-f5-com", ["same"], "2026-11-20T00:00:00Z", complete=False)
    capture_state(latest, "same", complete=False, disposition=disposition)
    assert [entry["queue"] for entry in list_queues(root, review_frontier=True)] == [
        "new/my-f5-com"
    ]


@pytest.mark.parametrize("disposition", ["capture_error", "access_queue"])
def test_pending_new_inventory_preserves_older_capture_failure(tmp_path, disposition):
    root = tmp_path / "queue"
    older = queue(root, "old", "my-f5-com", ["same"], "2026-11-19T00:00:00Z", complete=False)
    capture_state(older, "same", complete=False, disposition=disposition)
    queue(root, "new", "my-f5-com", ["same"], "2026-11-20T00:00:00Z", complete=False)
    assert [entry["queue"] for entry in list_queues(root, review_frontier=True)] == [
        "new/my-f5-com",
        "old/my-f5-com",
    ]


def test_pending_only_old_inventory_can_defer_without_losing_capture(tmp_path):
    root = tmp_path / "queue"
    queue(root, "old", "my-f5-com", ["same"], "2026-11-19T00:00:00Z", complete=False)
    queue(root, "new", "my-f5-com", ["same"], "2026-11-20T00:00:00Z", complete=False)
    assert [entry["queue"] for entry in list_queues(root, review_frontier=True)] == [
        "new/my-f5-com"
    ]


@pytest.mark.parametrize("flag", [1, "true", {"done": True}])
def test_non_boolean_complete_claim_cannot_hide_old_capture(tmp_path, flag):
    root = tmp_path / "queue"
    queue(root, "old", "my-f5-com", ["same"], "2026-11-19T00:00:00Z")
    latest = queue(root, "new", "my-f5-com", ["same"], "2026-11-20T00:00:00Z", complete=False)
    capture_state(latest, "same", complete=flag)
    assert [entry["queue"] for entry in list_queues(root, review_frontier=True)] == [
        "new/my-f5-com",
        "old/my-f5-com",
    ]


def test_capture_outcomes_can_compose_across_newer_selected_queues(tmp_path):
    root = tmp_path / "queue"
    queue(root, "old", "my-f5-com", ["one", "two"], "2026-11-19T00:00:00Z")
    queue(root, "middle", "my-f5-com", ["two"], "2026-11-20T00:00:00Z")
    latest = queue(root, "new", "my-f5-com", ["one", "two"], "2026-11-21T00:00:00Z", complete=False)
    capture_state(latest, "one", complete=True)
    selected = list_queues(root, review_frontier=True)
    assert [entry["queue"] for entry in selected] == ["new/my-f5-com", "middle/my-f5-com"]
    proof = json.loads((root / "review-frontier.private.json").read_bytes())["deferred"][0]
    assert proof["queue"] == "old/my-f5-com"
    assert proof["covering_queues"] == ["middle/my-f5-com", "new/my-f5-com"]
    assert proof["capture_outcomes"] == 2
