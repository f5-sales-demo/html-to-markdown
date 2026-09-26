from pathlib import Path

from html_to_markdown.models import PageMetadata
from html_to_markdown.render import content_hash, render_html, serialize_document, split_document

FIXTURES = Path(__file__).parent / "fixtures"


def test_markdown_preserves_semantics_and_removes_chrome() -> None:
    rendered = render_html(
        (FIXTURES / "docs_page.html").read_text(),
        "https://docs.cloud.f5.com/docs-v2/platform/how-to/configure",
    )
    assert "## Procedure" in rendered.body
    assert "| Name | Value |" in rendered.body
    assert '```json\n{"enabled": true}\n```' in rendered.body
    assert "> **Warning**" in rendered.body
    assert "Global F5 header" not in rendered.body
    assert "Return to Top" not in rendered.body
    assert rendered.assets[0].placeholder in rendered.body


def test_frontmatter_order_unicode_null_dates_and_hash() -> None:
    metadata = PageMetadata(
        sourceId="docs-cloud-f5-com",
        title='Café: safe "title"',
        slug="cafe",
        url="https://docs.cloud.f5.com/docs-v2/cafe",
        category="guide",
        tags=["Zulu", "alpha", "alpha"],
    )
    document = serialize_document(metadata, "# Café  \r\n\r\nBody\n")
    parsed, body = split_document(document)
    keys = list(parsed)
    assert keys[:9] == [
        "sourceId",
        "title",
        "slug",
        "url",
        "category",
        "publication_date",
        "modification_date",
        "content_hash",
        "tags",
    ]
    assert parsed["publication_date"] is None
    assert parsed["tags"] == ["alpha", "Zulu"]
    assert parsed["content_hash"] == content_hash(body)
    assert "Café" in document
