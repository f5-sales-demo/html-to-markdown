"""Synthetic text substitutions require exact reviewed input and output."""

import hashlib

import pytest

from html_to_markdown.community_text import reviewed_text
from html_to_markdown.errors import PublicationBlockedError
from html_to_markdown.render import render_html


def test_reviewed_synthetic_text_and_attributes():
    html = '<p>Synthetic Original</p><a href="https://original.example.com">Docs</a>'
    expected = '<p>Example Corp</p><a href="https://example.com">Docs</a>'
    policy = {
        "text_replacements": {
            "nodes": {
                hashlib.sha256(b"Synthetic Original").hexdigest(): "Example Corp",
                hashlib.sha256(b"https://original.example.com").hexdigest(): "https://example.com",
            },
            "output_sha256": hashlib.sha256(expected.encode()).hexdigest(),
        }
    }
    assert reviewed_text(html, policy) == expected
    assert reviewed_text(html, {}) == html
    with pytest.raises(PublicationBlockedError, match="target changed"):
        reviewed_text(html.replace("Original", "Changed"), policy)
    policy["text_replacements"]["output_sha256"] = "0" * 64
    with pytest.raises(PublicationBlockedError, match="reviewed output"):
        reviewed_text(html, policy)


@pytest.mark.parametrize(
    "policy", [[], {}, {"nodes": {}}, {"nodes": {hashlib.sha256(b"Original").hexdigest(): 3}}]
)
def test_invalid_replacements(policy):
    with pytest.raises(PublicationBlockedError):
        reviewed_text("<p>Original</p>", {"text_replacements": policy})


def test_reviewed_placeholder_repair_survives_fenced_markdown():
    from bs4 import BeautifulSoup

    broken = 'certificate_url = "string:///<base64 encoding="" of="" public="" key="">"\n}</base64>'
    fixed = 'certificate_url = "string:///<base64-encoded-public-key>"\n}'
    soup = BeautifulSoup("<pre><code class='language-hcl'></code></pre>", "html.parser")
    soup.code.string = broken
    html = str(soup)
    soup.code.string = fixed
    review = {
        "text_replacements": {
            "nodes": {hashlib.sha256(broken.encode()).hexdigest(): fixed},
            "output_sha256": hashlib.sha256(str(soup).encode()).hexdigest(),
        }
    }
    repaired = reviewed_text(html, review)
    assert render_html(repaired, "https://community.f5.com/t/67372").body == (
        '```hcl\ncertificate_url = "string:///<base64-encoded-public-key>"\n}\n```\n'
    )
