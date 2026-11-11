import pytest

from html_to_markdown.errors import AllowlistError
from html_to_markdown.urls import (
    canonicalize_url,
    stable_path,
    validate_asset_url,
    validate_source_url,
)


def test_canonicalization_and_stable_paths() -> None:
    assert canonicalize_url("HTTPS://DOCS.CLOUD.F5.COM/docs-v2//a/?z=2&a=1#x") == (
        "https://docs.cloud.f5.com/docs-v2/a?a=1&z=2"
    )
    assert str(stable_path("docs-cloud-f5-com", "https://docs.cloud.f5.com/docs-v2/a/b")) == "a/b"
    assert (
        str(stable_path("my-f5-com", "https://my.f5.com/manage/s/article/K000123456"))
        == "K000123456"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://docs.cloud.f5.com/docs-v2/a",
        "https://evil.test/docs-v2/a",
        "https://docs.cloud.f5.com/not-docs-v2/a",
    ],
)
def test_source_allowlist_rejects_escapes(url: str) -> None:
    with pytest.raises(AllowlistError):
        validate_source_url("docs-cloud-f5-com", url)


def test_devcentral_media_bucket_is_accepted_only_as_an_https_asset() -> None:
    host = "assets-devcentralsbx-f5-prod-us.s3.dualstack.us-west-2.amazonaws.com"
    url = "https://" + host + "/diagram.png"
    assert validate_asset_url(url) == url
    for source in ["community-f5-com", "docs-cloud-f5-com", "my-f5-com", "www-f5-com"]:
        with pytest.raises(AllowlistError):
            validate_source_url(source, url)
    for rejected in [
        "http://" + host + "/diagram.png",
        "https://" + host + ".evil.test/diagram.png",
        "https://other-bucket.s3.dualstack.us-west-2.amazonaws.com/diagram.png",
    ]:
        with pytest.raises(AllowlistError):
            validate_asset_url(rejected)
