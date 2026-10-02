"""Synthetic text substitutions require exact reviewed input and output."""

import hashlib

import pytest

from html_to_markdown.community_text import reviewed_text
from html_to_markdown.errors import PublicationBlockedError


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
