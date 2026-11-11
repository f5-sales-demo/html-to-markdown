"""Authenticated CI artifacts contain only allowlisted queue provenance."""

import json
from pathlib import Path

from typer.testing import CliRunner

from html_to_markdown.cli import app
from html_to_markdown.safe_reports import sanitize_inventory


def test_ci_receipt_preserves_dispositions_and_excludes_identifying_evidence(
    tmp_path: Path,
) -> None:
    source = tmp_path / "private.json"
    private = "unpublished identifying value"
    source.write_text(
        json.dumps(
            {
                "documents": [
                    {
                        "identity": private,
                        "url": "https://docs.cloud.f5.com/docs-v2/platform/how-to/test",
                        "disposition": "queued",
                        "reason": private,
                        "body": private,
                        "metadata": {"owner": private},
                        "media": [{"ocr": private, "bytes": private}],
                    },
                    {
                        "url": "https://user:private-credential@docs.cloud.f5.com/docs-v2/test",
                        "disposition": "policy_excluded",
                    },
                    {
                        "url": "https://my.f5.com/manage/s/article/K000123456?token=private-credential",
                        "disposition": "access_queue",
                    },
                ],
                "completeness": {"enumeration": True, "capture": False},
            }
        )
    )
    output = tmp_path / "safe.json"
    report = sanitize_inventory(source, output)
    text = output.read_text()
    assert private not in text and "private-credential" not in text
    assert report["counts"] == {"access_queue": 1, "policy_excluded": 1, "queued": 1}
    assert report["completeness"]["enumeration"] and not report["completeness"]["capture"]
    assert "url" not in report["documents"][2]
    assert report["documents"][1]["url"] == "https://docs.cloud.f5.com/docs-v2/test"
    result = CliRunner().invoke(
        app,
        ["sanitize-inventory", "--input-path", str(source), "--output", str(tmp_path / "cli.json")],
    )
    assert result.exit_code == 0 and (tmp_path / "cli.json").read_bytes() == output.read_bytes()


def test_legacy_workflows_switch_only_after_private_queue_activation() -> None:
    root = Path(__file__).parents[1] / ".github/workflows"
    official = (root / "inventory-docs.yml").read_text()
    community = (root / "release-content.yml").read_text()
    for workflow in (official, community):
        assert "vars.PRIVATE_CONTENT_QUEUE_ENABLED != 'true'" in workflow
        assert "sanitize-inventory" in workflow and "retention-days: 90" in workflow
    paths = official.split("path: |", 1)[1]
    assert "capture/captures/" not in paths and "state.sqlite" not in paths
    assert "build/community-inventory/review-queue.json" not in community
