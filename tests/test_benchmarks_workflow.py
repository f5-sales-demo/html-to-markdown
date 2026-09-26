import json
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]


def test_benchmark_manifests_are_pinned_unique_and_sized() -> None:
    expected = {
        "docs-cloud-12.json": 12,
        "docs-cloud-100.json": 100,
        "my-f5-12.json": 12,
        "my-f5-100.json": 100,
    }
    for name, size in expected.items():
        value = json.loads((ROOT / "benchmarks" / name).read_text(encoding="utf-8"))
        assert value["size"] == size
        assert len(value["urls"]) == size
        assert len(set(value["urls"])) == size
        assert value["selection"] == "lowest sha256(url), ascending"
    full = json.loads(
        (ROOT / "benchmarks/full-prototype-inventory.json").read_text(encoding="utf-8")
    )
    assert len(full["urls"]) > 900
    assert len(set(full["urls"])) == len(full["urls"])


def test_release_workflow_is_artifact_first_and_change_gated() -> None:
    path = ROOT / ".github/workflows/release-content.yml"
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["snapshot"]["steps"]
    upload = next(step for step in steps if step.get("name") == "Upload completed-run artifacts")
    assert "if" not in upload
    uploaded = upload["with"]["path"]
    for name in (
        "html-to-markdown-content.tar.gz",
        "html-to-markdown-content.tar.gz.sha256",
        "manifest.json",
        "quality-report.json",
        "quality-report.md",
    ):
        assert name in uploaded
    publish = next(step for step in steps if step.get("name") == "Publish changed snapshot")
    assert publish["if"] == "steps.changed.outputs.value == 'true'"
    text = path.read_text(encoding="utf-8")
    assert "acknowledge_page_drop" not in text
    assert "full-prototype-inventory.json" in text
