from pathlib import Path

from markdown_it import MarkdownIt

from html_to_markdown.adapters.docs_cloud import DocsCloudAdapter
from html_to_markdown.adapters.my_f5 import MyF5Adapter
from html_to_markdown.models import FetchResult, PageMetadata
from html_to_markdown.render import content_hash, render_html, serialize_document, split_document

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN = Path(__file__).parent / "golden"


def test_adapters_match_pinned_prototype_capture_goldens() -> None:
    cases = (
        (
            DocsCloudAdapter(),
            "docs_page.html",
            "docs-cloud-prototype.md",
            "https://docs.cloud.f5.com/docs-v2/platform/how-to/configure",
        ),
        (
            MyF5Adapter(),
            "my_f5_page.html",
            "my-f5-prototype.md",
            "https://my.f5.com/manage/s/article/K000123456",
        ),
    )
    for adapter, fixture, golden, url in cases:
        fetched = FetchResult(
            url=url,
            final_url=url,
            status_code=200,
            html=(FIXTURES / fixture).read_text(encoding="utf-8"),
        )
        extracted = adapter.extract(fetched)
        rendered = render_html(extracted.html, url)
        assert rendered.body == (GOLDEN / golden).read_text(encoding="utf-8")


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
    assert keys[:10] == [
        "metadata_schema",
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


def test_source_code_widget_line_boundaries_preserve_commands_and_order() -> None:
    html = '<pre><code class="language-bash"><div class="ec-line"><span class="code">export MODE=example</span></div><div class="ec-line"><span class="code">curl --retry 3 https://example.com</span></div></code></pre>'
    body = render_html(html, "https://docs.cloud.f5.com/docs-v2/example").body
    assert "export MODE=example\ncurl --retry 3 https://example.com" in body
    assert body.startswith("```bash\n")


def test_visible_angle_brackets_remain_text_after_markdown_rendering() -> None:
    html = """<h2>Support for &lt;legacy&gt;</h2>
<p>Keep the &lt;legacy&gt; qualifier and C:\\work path.</p>
<table><tr><th>Schema</th></tr><tr><td>&lt;schema&gt;</td></tr></table>
<p><code>curl &lt;source&gt; &amp;&amp; echo done</code></p>
<pre><code>&lt;schema&gt;\\payload&lt;/schema&gt;</code></pre>"""
    body = render_html(html, "https://example.com").body
    rendered = MarkdownIt("commonmark", {"html": True}).enable("table").render(body)
    assert "Support for &lt;legacy&gt;" in rendered
    assert "Keep the &lt;legacy&gt; qualifier and C:\\work path." in rendered
    assert "<td>&lt;schema&gt;</td>" in rendered
    assert "<legacy>" not in rendered and "<schema>" not in rendered
    assert "`curl <source> && echo done`" in body
    assert "```\n<schema>\\payload</schema>\n```" in body


def test_capability_cards_preserve_title_and_description_boundaries() -> None:
    html = """<main><div class="cards">
<div class="card"><div>HTTP checks</div><div>Verify expected status.</div></div>
<div class="card"><div>DNS checks</div><div>Verify expected records.</div></div>
</div><table><tr><th>Capability</th></tr>
<tr><td><div>Latency</div><div>Measure milliseconds.</div></td></tr></table>
<div class="alert">Preserve the warning.</div></main>"""
    body = render_html(html, "https://example.com").body
    rendered = MarkdownIt("commonmark", {"html": True}).enable("table").render(body)
    assert "<p>HTTP checks</p>" in rendered
    assert "<p>Verify expected status.</p>" in rendered
    assert "<p>DNS checks</p>" in rendered
    assert "<p>Verify expected records.</p>" in rendered
    assert "Latency Measure milliseconds." in " ".join(rendered.split())
    assert "> Preserve the warning." in body
