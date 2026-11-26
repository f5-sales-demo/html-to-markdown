"""Selected original source proofs enter ordinary replay and sealed source inputs."""

import json

import pytest
from test_source_existing_context import prepared
from test_source_existing_selection import history
from test_source_existing_validation import IndependentClient

from html_to_markdown.curation import json_bytes
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.source_existing_delivery import admit_existing_source, prepare_existing_source
from html_to_markdown.source_existing_validation import review_source_selection
from html_to_markdown.source_inputs import MARKER, seal_source, verify_source
from html_to_markdown.source_review_jobs import run_reviews, verify_reviews


def admitted(capture, tmp_path, media=False):
    inventory = json.loads((capture / "inventory.json").read_bytes())
    inventory["binding"]["source"] = "my-f5-com"
    inventory["completeness"]["capture"] = True
    inventory["ended_at"] = "2026-11-01T00:00:00Z"
    (capture / "inventory.json").write_bytes(json_bytes(inventory))
    current, original, _, _ = prepared(capture, tmp_path, media)
    root = tmp_path / "histories"
    donor = history(original, root)
    original_bytes = current.read_bytes(), donor.read_bytes()
    selection = tmp_path / "selection"
    journal = prepare_existing_source(capture, current, [root], selection)
    validation = tmp_path / "validation"
    client = IndependentClient()
    result = review_source_selection(journal, validation, client)
    saved = admit_existing_source(capture, current.parent.parent, current, journal, validation)
    return current, donor, root, journal, validation, saved, original_bytes, client, result


@pytest.mark.parametrize("media", [False, True])
def test_selected_proof_replays_in_ordinary_source_and_sealed_input(capture, tmp_path, media):
    current, donor, _, _, _, saved, original_bytes, client, result = admitted(
        capture, tmp_path, media
    )
    output = current.parent.parent
    assert saved["disposition"] == "accepted" and saved["privacy_exhausted"]
    assert saved["selected_existing_source"]["editor_corrections_consumed"] == 2
    assert len(saved["privacy_feedback"]) == 2
    assert donor.read_bytes() == original_bytes[1]
    assert result["selected_record_sha256"] == sha(donor.read_bytes())
    assert all(body["text"]["format"]["name"] != "privacydecision" for body in client.calls)
    before = current.read_bytes()
    verified = verify_reviews(capture, output)
    assert current.read_bytes() == before
    assert verified["accepted_files"]
    sealed = tmp_path / "sealed"
    receipt = seal_source(capture, output, sealed)
    digest = sha((sealed / MARKER).read_bytes())
    assert receipt["accepted_files"] == verified["accepted_files"]
    assert verify_source(sealed, digest)["accepted_files"] == verified["accepted_files"]


@pytest.mark.parametrize("damage", ["original", "allowance", "new_history", "capture", "output"])
def test_source_verifier_cannot_accept_changed_origin_or_selected_evidence(
    capture, tmp_path, damage
):
    current, donor, root, _, _, saved, _, _, _ = admitted(capture, tmp_path)
    output = current.parent.parent
    if damage == "original":
        donor.write_bytes(donor.read_bytes() + b" ")
    elif damage == "allowance":
        saved["privacy_feedback"] = []
        current.write_bytes(json_bytes(saved))
    elif damage == "new_history":
        extra = json.loads(donor.read_bytes())
        extra["privacy_feedback"].append("New strongest retained failure")
        raw = json_bytes(extra)
        (root / (sha(raw) + ".json")).write_bytes(raw)
    elif damage == "capture":
        (capture / saved["item"]["article_path"]).write_bytes(b"Different source")
    else:
        relative = next(iter(saved["file_hashes"]))
        (output / "reviewed" / relative.removeprefix("articles/")).write_bytes(b"Changed output")
    before = current.read_bytes()
    with pytest.raises((ValueError, FileNotFoundError)):
        verify_reviews(capture, output)
    assert current.read_bytes() == before


def test_selected_receipt_skips_editor_phases_even_with_exhausted_ledger(capture, tmp_path):
    current, _, _, _, _, _, _, _, _ = admitted(capture, tmp_path)
    output = current.parent.parent
    report = run_reviews(capture, output, phase="replay", journal=tmp_path / "journal")
    assert not report["exceptions"]
    assert report["documents"][0]["disposition"] == "accepted"


def test_live_record_cas_rejects_an_intervening_change_before_promotion(capture, tmp_path):
    current, original, _, _ = prepared(capture, tmp_path)
    root = tmp_path / "histories"
    history(original, root)
    journal = prepare_existing_source(capture, current, [root], tmp_path / "selection")
    validation = tmp_path / "validation"
    review_source_selection(journal, validation, IndependentClient())
    current.write_bytes(current.read_bytes() + b" ")
    before = current.read_bytes()
    with pytest.raises(ValueError, match="exact live record"):
        admit_existing_source(capture, current.parent.parent, current, journal, validation)
    assert current.read_bytes() == before
    assert not (current.parent.parent / "reviewed").exists()


def test_missing_candidate_ocr_is_rejected_without_regenerating_or_repairing_it(
    capture, tmp_path, monkeypatch
):
    current, _, _, journal, validation, _, _, _, _ = admitted(capture, tmp_path, media=True)
    cache = next((validation / journal.stem / "media").glob("*-native-*.json"))
    cache.unlink()
    from html_to_markdown import source_candidate  # pylint: disable=import-outside-toplevel

    native = source_candidate.all_frame_inputs

    def no_ocr(path, *, perform_ocr=False):
        assert perform_ocr is False
        return native(path, perform_ocr=False)

    monkeypatch.setattr(source_candidate, "all_frame_inputs", no_ocr)
    before = {
        p: (p.stat().st_mtime_ns, sha(p.read_bytes())) for p in validation.rglob("*") if p.is_file()
    }
    with pytest.raises(ValueError):
        verify_reviews(capture, current.parent.parent)
    assert not cache.exists()
    assert before == {
        p: (p.stat().st_mtime_ns, sha(p.read_bytes())) for p in validation.rglob("*") if p.is_file()
    }
