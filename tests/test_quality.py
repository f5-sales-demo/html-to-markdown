import json
from pathlib import Path

from html_to_markdown.models import PageMetadata
from html_to_markdown.quality import analyze_quality, write_quality_reports
from html_to_markdown.render import serialize_document


def write_page(
    root: Path, slug: str, body: str, *, title: str | None = None, description: str | None = None
) -> None:
    target = root / "content" / "docs-cloud-f5-com" / slug / "index.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    metadata = PageMetadata(
        sourceId="docs-cloud-f5-com",
        title=title or slug.title(),
        slug=slug,
        url=f"https://docs.cloud.f5.com/docs-v2/{slug}",
        category="guide",
        description=description,
        tags=[],
    )
    target.write_text(serialize_document(metadata, body), encoding="utf-8")


def test_quality_reports_recall_structure_chrome_and_metadata(tmp_path: Path) -> None:
    reference = tmp_path / "reference"
    candidate = tmp_path / "candidate"
    write_page(
        reference,
        "configure",
        """# Configure Secure Service

Important service configuration instructions and policy details.

| Name | Value |
| --- | --- |
| mode | secure |

```json
{"mode": "secure"}
```

> Important: preserve this callout.

![Architecture](assets/architecture.png)
""",
        title="Configure Secure Service",
        description="Secure service configuration and policy details",
    )
    write_page(
        candidate,
        "configure",
        """# Configure Secure Service

Important service configuration instructions.

Return to Top
""",
        title="Unrelated Metadata",
        description="Marketing promotion",
    )
    report = analyze_quality(candidate, reference)
    page = report["pages"][0]
    assert page["relevant_text_recall"] is None
    assert "lost_tables" not in page
    assert "lost_code_blocks" not in page
    assert "lost_callouts" not in page
    assert "lost_figures" not in page
    assert page["recognized_chrome"] == ["Return to Top"]
    assert page["metadata_relevance"]["weak_title"] is True
    assert report["summary"]["quality_status"] == "regressed"


def test_repeated_boilerplate_and_promotional_content_are_reported(tmp_path: Path) -> None:
    for index in range(3):
        write_page(
            tmp_path,
            f"page-{index}",
            f"""# Page {index}

Unique technical instructions number {index} for service configuration.

Search the documentation and choose a product from this navigation menu.

Learn More about our exciting product offerings.
""",
        )
    report = analyze_quality(tmp_path)
    assert report["summary"]["repeated_boilerplate_blocks"] == 1
    assert report["summary"]["promotional_fragments"] == 3
    assert report["repeated_boilerplate"][0]["document_count"] == 3


def test_instructional_learn_more_links_are_not_promotional(tmp_path: Path) -> None:
    write_page(
        tmp_path,
        "guide",
        """# Guide

To learn more about behavioral analysis, see [Behavioral Firewall](https://example.test).

Click each concept to learn more about the available configuration.

Select **Azure** in **Select Service Provider** in the pop-up window.

The service taxonomy aligns with the available product offerings.
""",
    )

    report = analyze_quality(tmp_path)

    assert report["summary"]["promotional_fragments"] == 0
    assert report["summary"]["recognized_chrome_fragments"] == 0


def test_quality_output_is_deterministic_and_honors_benchmark(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    write_page(candidate, "included", "# Included\n\nDeterministic body terms for comparison.")
    write_page(candidate, "excluded", "# Excluded\n\nThis page is outside the pinned corpus.")
    benchmark = tmp_path / "benchmark.json"
    benchmark.write_text(
        json.dumps(
            {
                "version": 1,
                "name": "fixture",
                "urls": ["https://docs.cloud.f5.com/docs-v2/included"],
            }
        ),
        encoding="utf-8",
    )
    first = write_quality_reports(candidate, benchmark=benchmark)
    json_bytes = first[0].read_bytes()
    markdown_bytes = first[1].read_bytes()
    second = write_quality_reports(candidate, benchmark=benchmark)
    assert second[0].read_bytes() == json_bytes
    assert second[1].read_bytes() == markdown_bytes
    assert json.loads(json_bytes)["summary"]["page_count"] == 1


def test_missing_benchmark_urls_are_explicit_regressions(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    write_page(candidate, "present", "# Present\n\nTechnical body content.")
    missing = "https://docs.cloud.f5.com/docs-v2/missing"
    benchmark = tmp_path / "benchmark.json"
    benchmark.write_text(json.dumps({"version": 1, "urls": [missing]}), encoding="utf-8")
    report = analyze_quality(candidate, benchmark=benchmark)
    assert report["summary"]["missing_benchmark_pages"] == 0
    assert report["summary"]["quality_status"] == "not_compared"
    assert "missing_benchmark_urls" not in report
