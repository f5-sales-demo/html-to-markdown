"""Dependency selection follows acknowledged pins, preserving invalid-input visibility."""

import json
import shutil

from test_combined_sources import admitted

from html_to_markdown.curation import json_bytes
from html_to_markdown.dependency_source_selection import selected_inputs


def test_selection_uses_checkpoint_pin_and_ignores_unacknowledged_larger_receipts(
    capture, tmp_path, monkeypatch
):
    bundle, pin = admitted(capture, tmp_path, monkeypatch)
    output = tmp_path / "worker"
    queue = output / "capture-20261110T000000Z/my-f5-com"
    selected = queue / "sealed/selected"
    shutil.copytree(bundle, selected)
    (queue / "checkpoint.json").write_bytes(json_bytes({"returned_admission_sha256": pin}))
    old = queue / "sealed/unacknowledged"
    shutil.copytree(bundle, old)
    receipt = json.loads((old / "source-review-receipt.json").read_bytes())
    receipt["documents"] *= 10
    (old / "source-review-receipt.json").write_bytes(json_bytes(receipt))
    assert selected_inputs(output) == {"my-f5-com": (selected, pin)}
    (queue / "checkpoint.json").write_bytes(json_bytes({"returned_admission_sha256": "f" * 64}))
    assert selected_inputs(output) == {}
    report = json.loads((output / "dependency-source-inputs.json").read_bytes())
    assert len(report["errors"]) == 1 and not report["sources"]
