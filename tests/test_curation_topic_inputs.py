"""Preserved original topic inputs reject changed bytes, policy and path substitutions."""

from types import SimpleNamespace

import pytest

from html_to_markdown.curation_topic_chain import preserve_inputs, verify_inputs


def inputs(tmp_path):
    article = tmp_path / "content/my-f5-com/K000123456/index.md"
    article.parent.mkdir(parents=True)
    article.write_text("# Cloud routes\n\nExpect healthy origins within 30 seconds.\n")
    image = article.parent / "assets/diagram.png"
    image.parent.mkdir()
    image.write_bytes(b"original diagram bytes")
    policy = SimpleNamespace(sha256="a" * 64)
    api = SimpleNamespace(policy_digest="b" * 64, catalog_digest="c" * 64)
    return article, image, policy, api


def test_topic_originals_preserve_complete_metadata_body_and_media(tmp_path):
    article, image, policy, api = inputs(tmp_path)
    record = preserve_inputs(tmp_path, policy, api)
    original = {
        article.relative_to(tmp_path).as_posix(): article.read_bytes(),
        image.relative_to(tmp_path).as_posix(): image.read_bytes(),
    }
    assert verify_inputs(tmp_path, record, policy, api) == original
    assert preserve_inputs(tmp_path, policy, api) == record


@pytest.mark.parametrize("fault", ["article", "media", "extra", "symlink", "policy", "catalog"])
def test_preserved_topic_input_substitutions_reject(tmp_path, fault):
    article, image, policy, api = inputs(tmp_path)
    record = preserve_inputs(tmp_path, policy, api)
    root = tmp_path / ".curation-topic-inputs" / record["identity"] / "source"
    if fault in {"article", "media"}:
        path = root / (article if fault == "article" else image).relative_to(tmp_path)
        path.write_bytes(b"changed original input")
    elif fault == "extra":
        (root / article.relative_to(tmp_path)).parent.joinpath("extra.md").write_text(
            "unregistered"
        )
    elif fault == "symlink":
        path = root / image.relative_to(tmp_path)
        path.unlink()
        path.symlink_to(image)
    elif fault == "policy":
        policy.sha256 = "d" * 64
    else:
        api.catalog_digest = "d" * 64
    with pytest.raises(ValueError):
        verify_inputs(tmp_path, record, policy, api)
