"""Reviewed synthetic replacements without storing original identity values."""

from __future__ import annotations

import hashlib
from typing import Any

from bs4 import BeautifulSoup, NavigableString

from .errors import PublicationBlockedError


def reviewed_text(html: str, review: dict[str, Any]) -> str:
    policy = review.get("text_replacements")
    if policy is None:
        return html
    if not isinstance(policy, dict) or not policy.get("nodes"):
        raise PublicationBlockedError("community text replacements are malformed")
    soup = BeautifulSoup(html, "html.parser")
    applied: set[str] = set()
    for node in list(soup.find_all(string=True)):
        digest = hashlib.sha256(str(node).encode()).hexdigest()
        replacement = policy["nodes"].get(digest)
        if replacement is not None:
            if not isinstance(replacement, str):
                raise PublicationBlockedError("community text replacement must be synthetic text")
            node.replace_with(NavigableString(replacement))
            applied.add(digest)
    for node in soup.find_all(True):
        for key in ("href", "src", "alt", "title"):
            if node.get(key) is None:
                continue
            digest = hashlib.sha256(str(node[key]).encode()).hexdigest()
            replacement = policy["nodes"].get(digest)
            if replacement is not None:
                if not isinstance(replacement, str):
                    raise PublicationBlockedError("community attribute replacement must be text")
                node[key] = replacement
                applied.add(digest)
    if applied != set(policy["nodes"]):
        raise PublicationBlockedError("community text replacement target changed")
    result = str(soup)
    if hashlib.sha256(result.encode()).hexdigest() != policy.get("output_sha256"):
        raise PublicationBlockedError("community synthetic text differs from reviewed output")
    return result
