"""Collapsible source steps retain block boundaries, descendants and technical meaning."""

import pytest
from markdown_it import MarkdownIt

from html_to_markdown.render import render_html


@pytest.mark.parametrize("list_tag", ["ul", "ol"])
def test_adjacent_step_summary_is_not_part_of_the_preceding_action(list_tag):
    html = f"<details><summary><b>Step 1:</b> Configure the service.</summary><{list_tag}><li>Save and Exit.</li></{list_tag}></details><details><summary><b>Step 2:</b> Verify the origin servers.</summary><p>Expect HTTP 200 within 30 seconds.</p></details>"
    rendered = render_html(html, "https://docs.cloud.f5.com/")
    assert "Save and Exit.\n\n**Step 2:**" in rendered.body
    depth = 0
    found = False
    for token in MarkdownIt("commonmark").parse(rendered.body):
        if token.type == "list_item_open":
            depth += 1
        elif token.type == "list_item_close":
            depth -= 1
        elif token.type == "inline" and "Step 2:" in token.content:
            assert depth == 0
            assert "Save and Exit" not in token.content
            found = True
    assert found


def test_nested_details_preserve_code_warning_links_and_media_deterministically():
    html = '<details><summary>Configure the origin.</summary><p>Use port 8443.</p><details><summary>Check health.</summary><pre><code>curl --retry 3 https://origin.example/health</code></pre><blockquote>Do not reset before saving.</blockquote><p><a href="/next">Next procedure</a></p><img src="/status.png" alt="Healthy origins"></details></details>'
    first = render_html(html, "https://docs.cloud.f5.com/")
    assert first == render_html(html, "https://docs.cloud.f5.com/")
    assert "Configure the origin.\n\nUse port 8443." in first.body
    assert "Check health.\n\n```" in first.body
    assert "curl --retry 3 https://origin.example/health" in first.body
    assert "> Do not reset before saving." in first.body
    assert "[Next procedure](https://docs.cloud.f5.com/next)" in first.body
    assert "![Healthy origins](asset://00000000)" in first.body
    assert len(first.assets) == 1
