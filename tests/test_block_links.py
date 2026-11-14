"""Linked HTML cards retain valid navigation, descendant facts and necessary media."""

import pytest
from markdown_it import MarkdownIt

from html_to_markdown.render import render_html


def links_and_images(body):
    children = [child for token in MarkdownIt().parse(body) for child in (token.children or [])]
    return (
        [child.attrGet("href") for child in children if child.type == "link_open"],
        [child.attrGet("src") for child in children if child.type == "image"],
        children,
    )


def test_actual_resource_and_community_cards_are_clickable_with_complete_descendants():
    html = """<article><h2>Administration</h2><ul>
    <li><a href="https://www.f5.com/cloud" title="Resources"><div><img src="/doc.svg" alt=""/>
    <div><h2>Product Resources</h2><p>Discover how administration service can serve your needs via these additional resources</p></div></div></a></li>
    <li><a href="https://community.f5.com/t5/distributed-cloud-users/gh-p/DistributedCloudUsers"><div>
    <h2>Interact With Community</h2><p>See what your peers are talking about in F5 DevCentral</p></div></a></li>
    </ul></article>"""
    result = render_html(html, "https://docs.cloud.f5.com/docs-v2/administration")
    links, images, tokens = links_and_images(result.body)
    assert links == [
        "https://www.f5.com/cloud",
        "https://community.f5.com/t5/distributed-cloud-users/gh-p/DistributedCloudUsers",
    ]
    assert images == ["asset://00000000"] and len(result.assets) == 1
    assert "Product Resources" in result.body and "Interact With Community" in result.body
    assert (
        "Discover how administration service can serve your needs via these additional resources"
        in result.body
    )
    assert "See what your peers are talking about in F5 DevCentral" in result.body
    assert any(
        token.type == "link_open" and token.attrGet("title") == "Resources" for token in tokens
    )
    assert "## Product Resources" not in result.body


def test_card_preserves_heading_boundaries_inline_formatting_and_literal_command():
    result = render_html(
        '<a href="/guide"><div><h2>Before starting</h2><h3>Timeout</h3>'
        "<p>Use <strong>30 seconds</strong> and <code>curl --retry 3</code>.</p>"
        "<p>Expect <em>HTTP 200</em>.</p></div></a>",
        "https://docs.cloud.f5.com/docs-v2/",
    )
    links, _, tokens = links_and_images(result.body)
    assert links == ["https://docs.cloud.f5.com/guide"]
    assert "Before starting" in result.body and "Timeout" in result.body
    assert "startingTimeout" not in result.body
    assert "**30 seconds**" in result.body and "*HTTP 200*" in result.body
    assert any(
        token.type == "code_inline" and token.content == "curl --retry 3" for token in tokens
    )


@pytest.mark.parametrize(
    "payload,expected",
    [
        (
            "<pre><code>curl --retry 3\nnext --timeout 30\n</code></pre>",
            "curl --retry 3\nnext --timeout 30",
        ),
        ("<ul><li>First action</li><li>Then verify HTTP 200</li></ul>", "Then verify HTTP 200"),
        ("<table><tr><th>Limit</th><td>30 seconds</td></tr></table>", "30 seconds"),
        (
            "<blockquote>Warning: keep the original order.</blockquote>",
            "Warning: keep the original order.",
        ),
    ],
)
def test_complex_linked_content_preserves_blocks_and_explicit_destination(payload, expected):
    result = render_html(
        '<a href="/reference" title="Reference"><h2>Required configuration</h2>' + payload + "</a>",
        "https://docs.cloud.f5.com/docs-v2/",
    )
    links, _, _ = links_and_images(result.body)
    assert links == ["https://docs.cloud.f5.com/reference"]
    assert expected in result.body and "Required configuration" in result.body
    assert (
        '[Required configuration](https://docs.cloud.f5.com/reference "Reference")' in result.body
    )
    assert (
        render_html(
            '<a href="/reference" title="Reference"><h2>Required configuration</h2>'
            + payload
            + "</a>",
            "https://docs.cloud.f5.com/docs-v2/",
        ).body
        == result.body
    )


def test_ordinary_inline_link_and_linked_image_keep_existing_destinations():
    result = render_html(
        '<p><a href="/guide">Read <em>the guide</em></a> '
        '<a href="/diagram"><img src="/diagram.png" alt="Network relationship"/></a></p>',
        "https://docs.cloud.f5.com/docs-v2/",
    )
    links, images, _ = links_and_images(result.body)
    assert links == ["https://docs.cloud.f5.com/guide", "https://docs.cloud.f5.com/diagram"]
    assert images == ["asset://00000000"]
    assert "[Read *the guide*]" in result.body
