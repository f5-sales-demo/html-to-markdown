"""Media relocation preserves each placement and rejects missing candidate assets."""

import pytest

from html_to_markdown.source_media_references import media_destination_map, rewrite_media_references


def test_exact_relocation_does_not_cascade_or_match_a_filename_prefix() -> None:
    body = "![A](assets/a)\n![B](assets/b)\n![Longer](assets/ab)\n"
    assert (
        rewrite_media_references(
            body,
            {"assets/a": "assets/b", "assets/b": "assets/c"},
            {"assets/b", "assets/c", "assets/ab"},
        )
        == "![A](assets/b)\n![B](assets/c)\n![Longer](assets/ab)\n"
    )


def test_repeated_markdown_and_html_placements_keep_surrounding_bytes() -> None:
    body = '!\\[literal\\](assets/a)\n![First](<assets/a> "caption")\n<img alt="Second" src="assets/a" width="30">\n![Third](assets/a)\n'
    expected = '!\\[literal\\](assets/a)\n![First](<assets/a.png> "caption")\n<img alt="Second" src="assets/a.png" width="30">\n![Third](assets/a.png)\n'
    assert (
        rewrite_media_references(body, {"assets/a": "assets/a.png"}, {"assets/a.png"}) == expected
    )


def test_literal_image_examples_and_comments_are_not_asset_placements() -> None:
    body = '\\![Escaped](assets/a)\n`![Inline](assets/a)`\n```markdown\n![Fenced](assets/a)\n```\n    ![Indented](assets/a)\n<!-- <img src="assets/a"> -->\n'
    assert rewrite_media_references(body, {"assets/a": "assets/a.png"}, set()) == body


def test_html_data_src_is_not_an_active_src_reference() -> None:
    body = '<img data-src="assets/a" src = "assets/b">'
    assert (
        rewrite_media_references(body, {"assets/b": "assets/b.png"}, {"assets/b.png"})
        == '<img data-src="assets/a" src = "assets/b.png">'
    )


def test_unavailable_local_image_rejects_the_source_candidate() -> None:
    with pytest.raises(ValueError, match="unavailable local media references"):
        rewrite_media_references("![Missing](assets/missing.png)", {}, set())


def test_repeated_source_placements_cannot_select_conflicting_destinations() -> None:
    assert media_destination_map([("assets/a", "assets/b"), ("assets/a", "assets/b")]) == {
        "assets/a": "assets/b"
    }
    with pytest.raises(ValueError, match="conflicting source media destinations"):
        media_destination_map([("assets/a", "assets/b"), ("assets/a", "assets/c")])
