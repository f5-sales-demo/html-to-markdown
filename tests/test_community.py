"""Community discovery and first-post publication contracts."""

import json

import httpx
import pytest

from html_to_markdown.adapters.community import (
    BASE,
    CommunityAdapter,
    CommunityClient,
    article_hash,
    clean_html,
    image_urls,
    inventory_pass,
    privacy_findings,
    relevance,
    validate_topic,
)
from html_to_markdown.errors import AllowlistError, PublicationBlockedError, ScrapeError
from html_to_markdown.models import FetchResult
from html_to_markdown.render import render_html
from html_to_markdown.urls import stable_path, validate_source_url


def topic(title="F5 Distributed Cloud routing", body="<p>Configure Distributed Cloud routing.</p>"):
    return {
        "id": 70152,
        "slug": "routing",
        "title": title,
        "category_id": 21,
        "tags": ["f5-distributed-cloud"],
        "visible": True,
        "post_stream": {
            "posts": [
                {
                    "post_number": 1,
                    "cooked": body,
                    "created_at": "2025-01-02T00:00:00Z",
                    "updated_at": "2026-01-03T00:00:00Z",
                },
                {"post_number": 2, "cooked": "<p>Private reply</p>"},
            ]
        },
    }


@pytest.mark.parametrize("path", ["/t/70152", "/t/old-slug/70152", "/t/new-slug/70152"])
def test_numeric_identity(path):
    assert stable_path("community-f5-com", BASE + path).as_posix() == "t/70152"
    assert validate_source_url("community-f5-com", BASE + path) == BASE + "/t/70152"


@pytest.mark.parametrize(
    "path",
    [
        "/search?q=xc",
        "/t/a/70152/2",
        "/t/a/70152.json",
        "/t/a/0",
        "/t/%2e%2e/70152",
        "/c/forums/12",
        "/t/70152?api_key=a",
    ],
)
def test_source_boundary(path):
    with pytest.raises(AllowlistError):
        validate_source_url("community-f5-com", BASE + path)


def test_first_post_and_dates():
    value = topic(
        body='<h2>Routing</h2><p>Distributed Cloud with incidental BIG-IP integration.</p><pre><code class="language-sh">echo example</code></pre><img class="emoji" src="/emoji/a.png">'
    )
    page = FetchResult(
        url=BASE + "/t/70152", final_url=BASE + "/t/70152", status_code=200, html=json.dumps(value)
    )
    result = CommunityAdapter().extract(page)
    rendered = render_html(result.html, page.url)
    assert "Private reply" not in rendered.body
    assert "```sh\necho example\n```" in rendered.body
    assert rendered.assets == []
    assert result.metadata.category == "F5 Technical Articles"
    assert result.metadata.publication_date == "2025-01-02"
    assert result.metadata.modification_date == "2026-01-03"


def test_relevance():
    assert relevance(topic())[0] == "include"
    assert relevance(topic("BIG-IP tuning", "<p>BIG-IP configuration.</p>"))[0] == "exclude"
    assert (
        relevance(topic("BIG-IP tuning", "<p>Also available in Distributed Cloud.</p>"))[0]
        == "review"
    )
    assert relevance(topic("Networking guide", "<p>Volterra routing.</p>"))[0] == "review"


@pytest.mark.parametrize(
    "change",
    [
        {"id": 5},
        {"category_id": 12},
        {"visible": False},
        {"post_stream": {"posts": [{"post_number": 2, "cooked": "reply"}]}},
    ],
)
def test_topic_validation(change):
    value = topic()
    value.update(change)
    with pytest.raises(ScrapeError):
        validate_topic(value, 70152)


def test_ambiguous_extraction_is_blocked():
    page = FetchResult(
        url=BASE + "/t/70152",
        final_url=BASE + "/t/70152",
        status_code=200,
        html=json.dumps(topic("Mixed products", "<p>BIG-IP and F5 XC.</p>")),
    )
    with pytest.raises(PublicationBlockedError):
        CommunityAdapter().extract(page)


@pytest.fixture
def no_wait(monkeypatch):
    async def immediate(seconds):
        pass

    monkeypatch.setattr("html_to_markdown.adapters.community.asyncio.sleep", immediate)


async def client_for(handler):
    return CommunityClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)))


@pytest.mark.asyncio
async def test_category_and_tag_pagination(no_wait):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if request.url.params.get("page"):
            return httpx.Response(
                200,
                json={
                    "topic_list": {
                        "topics": [
                            {"id": 70152, "category_id": 21},
                            {"id": 73730, "category_id": 21},
                        ]
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "topic_list": {
                    "topics": [{"id": 70152, "category_id": 21}],
                    "more_topics_url": request.url.path.removesuffix(".json") + "?page=1",
                }
            },
        )

    client = await client_for(handler)
    for path in ["/c/articles/technical-articles/21.json", "/tag/f5-distributed-cloud.json"]:
        assert set(await client.listing(BASE + path)) == {70152, 73730}
    assert len(calls) == 4
    await client.http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "listing",
    [
        {},
        {"topic_list": {"topics": []}},
        {"topic_list": {"topics": [{"id": "70152", "category_id": 21}]}},
        {"topic_list": {"topics": [{"id": 70152, "category_id": 12}]}},
        {
            "topic_list": {
                "topics": [{"id": 70152, "category_id": 21}],
                "more_topics_url": "https://example.com/c/articles/technical-articles/21?page=1",
            }
        },
        {
            "topic_list": {
                "topics": [],
                "more_topics_url": "/c/articles/technical-articles/21?page=1",
            }
        },
        {
            "topic_list": {
                "topics": [{"id": 70152, "category_id": 21}],
                "more_topics_url": "/c/articles/technical-articles/21",
            }
        },
    ],
)
async def test_listing_fails_closed(no_wait, listing):
    client = await client_for(lambda request: httpx.Response(200, json=listing))
    with pytest.raises(ScrapeError):
        await client.listing(BASE + "/c/articles/technical-articles/21.json", 21)
    await client.http.aclose()


@pytest.mark.asyncio
async def test_retry_after_transient_and_same_id_redirect(no_wait):
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        if count == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        if count == 2:
            return httpx.Response(503, headers={"Retry-After": "Fri, 02 Oct 2026 00:00:00 GMT"})
        if count == 3:
            return httpx.Response(302, headers={"Location": "/t/new-slug/70152.json"})
        return httpx.Response(200, json=topic())

    client = await client_for(handler)
    assert (await client.topic(70152))["id"] == 70152
    assert count == 4
    await client.http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "location",
    ["https://example.com/t/70152.json", "/t/70153.json", "/search?q=xc", "/u/profile.json"],
)
async def test_redirect_escape(no_wait, location):
    client = await client_for(lambda request: httpx.Response(302, headers={"location": location}))
    with pytest.raises((ScrapeError, AllowlistError)):
        await client.topic(70152)
    await client.http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [404, 403, 429, 503, 302])
async def test_http_failures(no_wait, status):
    client = await client_for(lambda request: httpx.Response(status))
    with pytest.raises(ScrapeError):
        await client.topic(70152)
    await client.http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [b"not json", b"[]"])
async def test_json_validation(no_wait, content):
    client = await client_for(lambda request: httpx.Response(200, content=content))
    with pytest.raises(ScrapeError):
        await client.json(BASE + "/t/70152.json")
    await client.http.aclose()


@pytest.mark.asyncio
async def test_sitemap_reconciliation(no_wait):
    ns = 'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'

    def handler(request):
        if request.url.path == "/sitemap.xml":
            return httpx.Response(
                200,
                text=f"<sitemapindex {ns}><sitemap><loc>{BASE}/sitemap_2.xml</loc></sitemap></sitemapindex>",
            )
        return httpx.Response(
            200,
            text=f"<urlset {ns}><url><loc>{BASE}/t/old/70152</loc></url><url><loc>{BASE}/c/forums/12</loc></url></urlset>",
        )

    client = await client_for(handler)
    assert await client.sitemap_ids() == {70152}
    await client.http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "xml",
    [
        "<bad/>",
        "<urlset><url/></urlset>",
        "<urlset/>",
        f"<sitemapindex><sitemap><loc>{BASE}/sitemap.xml</loc></sitemap></sitemapindex>",
        "<sitemapindex><sitemap><loc>https://example.com/sitemap.xml</loc></sitemap></sitemapindex>",
    ],
)
async def test_sitemap_failures(no_wait, xml):
    client = await client_for(lambda request: httpx.Response(200, text=xml))
    with pytest.raises(ScrapeError):
        await client.sitemap_ids()
    await client.http.aclose()


def test_privacy_and_meaningful_images():
    value = topic(
        body='<p>Contact synthetic@example.com; token=aaaaaaaaaaaaaaaaaaaa; address 192.0.2.1</p><a class="lightbox" href="https://d20hrnpixdzcsd.cloudfront.net/original/diagram.png"><img src="/small.png" alt="Routing diagram"></a><img class="avatar" src="/profile.png"><span class="mention">@profile</span>'
    )
    html = clean_html(value)
    assert privacy_findings(html) == ["credential", "email", "ipv4"]
    assert image_urls(html) == ["https://d20hrnpixdzcsd.cloudfront.net/original/diagram.png"]
    assert "profile" not in html
    page = FetchResult(
        url=BASE + "/t/70152", final_url=BASE + "/t/70152", status_code=200, html=json.dumps(value)
    )
    with pytest.raises(PublicationBlockedError):
        CommunityAdapter().extract(page)
    value["post_stream"]["posts"][0]["cooked"] = value["post_stream"]["posts"][0]["cooked"].replace(
        "token=aaaaaaaaaaaaaaaaaaaa", "token=EXAMPLE"
    )
    page.html = json.dumps(value)
    sanitized = validate_topic(value, 70152)
    page.headers["community-review"] = json.dumps(
        {
            "article_hash": article_hash(sanitized),
            "decision": "include",
            "privacy": "approved",
            "reason": "Reviewed synthetic documentation examples and diagram",
        }
    )
    result = CommunityAdapter().extract(page)
    assert len(render_html(result.html, page.url).assets) == 1
    value["post_stream"]["posts"][0]["cooked"] += "<p>Changed</p>"
    page.html = json.dumps(value)
    with pytest.raises(PublicationBlockedError):
        CommunityAdapter().extract(page)


class InventoryClient:
    async def listing(self, url, category_id=None):
        if category_id == 21 or category_id is None:
            return {
                identifier: {"id": identifier, "category_id": 21}
                for identifier in [73730, 72834, 72617, 70152, 71412, 72266, 70153]
            }
        return {category_id: {"id": category_id, "category_id": category_id}}

    async def sitemap_ids(self):
        return {73730, 72834, 72617, 70152, 71412, 72266, 70153, 8, 15}

    async def topic(self, identifier):
        value = topic()
        value["id"] = identifier
        if identifier in [8, 15]:
            value["category_id"] = identifier
            value["title"] = "BIG-IP reference"
            value["post_stream"]["posts"][0]["cooked"] = "<p>BIG-IP tuning</p>"
        return validate_topic(value, identifier)


@pytest.mark.asyncio
async def test_complete_inventory_and_review_gate(tmp_path):
    client = InventoryClient()
    report = await inventory_pass(client, tmp_path)
    assert report["complete"] and report["topic_count"] == 9
    assert len([item for item in report["topics"] if item["decision"] == "include"]) == 7
    assert len(list((tmp_path / "topics").glob("*.json"))) == 9
    original = client.topic

    async def mixed(identifier):
        value = await original(identifier)
        if identifier == 8:
            value["title"] = "Mixed products"
            value["post_stream"]["posts"][0]["cooked"] = "<p>F5 XC and BIG-IP reference</p>"
        return value

    client.topic = mixed
    (tmp_path / "topics/8.json").unlink()
    with pytest.raises(PublicationBlockedError, match="unresolved"):
        await inventory_pass(client, tmp_path)
    value = await mixed(8)
    (tmp_path / "reviews.json").write_text(
        json.dumps(
            {
                "8": {
                    "article_hash": article_hash(value),
                    "decision": "exclude",
                    "reason": "BIG-IP focused body; XC comparison only",
                }
            }
        )
    )
    assert (await inventory_pass(client, tmp_path))["unresolved"] == []

    async def failure(identifier):
        raise ScrapeError("unavailable public first post")

    client.topic = failure
    (tmp_path / "topics/8.json").unlink()
    with pytest.raises(ScrapeError):
        await inventory_pass(client, tmp_path)
    assert (tmp_path / "incomplete.json").is_file()


@pytest.mark.asyncio
async def test_network_retry_and_exhaustion(no_wait):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise httpx.ReadTimeout("transient", request=request)
        return httpx.Response(200, json=topic())

    client = await client_for(handler)
    assert (await client.topic(70152))["id"] == 70152
    client.retries = 1
    calls = 0
    with pytest.raises(ScrapeError):
        await client.topic(70152)
    await client.http.aclose()


@pytest.mark.asyncio
async def test_bad_retry_date_and_redirect_limit(no_wait):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, headers={"Retry-After": "invalid"})
        return httpx.Response(302, headers={"Location": "/t/70152.json"})

    client = await client_for(handler)
    with pytest.raises(ScrapeError, match="redirect limit"):
        await client.topic(70152)
    await client.http.aclose()


@pytest.mark.parametrize("change", [{"slug": None}, {"tags": [3]}, {"tags": "tag"}])
def test_invalid_metadata(change):
    value = topic()
    value.update(change)
    with pytest.raises(ScrapeError):
        validate_topic(value, 70152)


def test_invalid_date_and_object_tag():
    value = topic()
    value["tags"] = [{"name": "f5-distributed-cloud"}]
    assert validate_topic(value, 70152)["tags"] == ["f5-distributed-cloud"]
    value["post_stream"]["posts"][0]["created_at"] = "invalid"
    with pytest.raises(ScrapeError):
        validate_topic(value, 70152)


@pytest.mark.asyncio
async def test_adapter_discovery_and_http_only(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import html_to_markdown.adapters.community as module

    async def inventory(client, output):
        return {
            "topics": [
                {"id": 70152, "decision": "include", "updated_at": "2026-01-03"},
                {"id": 8, "decision": "exclude", "updated_at": "2026-01-03"},
            ]
        }

    monkeypatch.setattr(module, "inventory_pass", inventory)
    adapter = CommunityAdapter()
    pages = await adapter.discover(
        SimpleNamespace(community_client=InventoryClient(), community_inventory_dir=tmp_path)
    )
    assert len(pages) == 1 and pages[0].url == BASE + "/t/70152"
    assert not adapter.needs_browser("")
    assert adapter.readiness_script() is None
    with pytest.raises(ScrapeError):
        await adapter.rendered_html(None)
    page = FetchResult(
        url=BASE + "/t/70152",
        final_url=BASE + "/t/70153",
        status_code=200,
        html=json.dumps(topic()),
    )
    with pytest.raises(ScrapeError):
        adapter.classify_page(page)


@pytest.mark.asyncio
async def test_inventory_unknown_media_and_required_examples(tmp_path):
    client = InventoryClient()
    original = client.topic

    async def media(identifier):
        value = await original(identifier)
        if identifier == 70152:
            value["post_stream"]["posts"][0]["cooked"] += (
                '<img src="https://example.com/diagram.png">'
            )
        return value

    client.topic = media
    with pytest.raises(PublicationBlockedError):
        await inventory_pass(client, tmp_path)
    report = json.loads((tmp_path / "inventory.json").read_text())
    record = next(x for x in report["topics"] if x["id"] == 70152)
    assert record["unknown_image_hosts"] == ["example.com"]
    value = await media(70152)
    (tmp_path / "reviews.json").write_text(
        json.dumps(
            {
                "70152": {
                    "article_hash": article_hash(value),
                    "decision": "exclude",
                    "privacy": "approved",
                    "reason": "Excluded for this test",
                }
            }
        )
    )
    with pytest.raises(PublicationBlockedError, match="required example"):
        await inventory_pass(client, tmp_path)


@pytest.mark.asyncio
async def test_publication_cannot_drop_or_carry_forward_accepted_articles(tmp_path):
    from html_to_markdown.adapters.community import validate_community_publication
    from html_to_markdown.models import DiscoveredPage, PageStatus
    from html_to_markdown.state import StateStore

    store = StateStore(tmp_path / "state.sqlite")
    validate_community_publication(tmp_path, store)
    url = BASE + "/t/70152"
    store.discover([DiscoveredPage(source_id="community-f5-com", url=url)])
    with pytest.raises(PublicationBlockedError, match="complete reviewed"):
        validate_community_publication(tmp_path, store)
    root = tmp_path / "community-inventory"
    root.mkdir()
    inventory = {
        "complete": True,
        "unresolved": [],
        "topics": [{"url": url, "decision": "include"}],
    }
    target = root / "inventory.json"
    target.write_text(json.dumps(inventory))
    with pytest.raises(PublicationBlockedError, match="every accepted"):
        validate_community_publication(tmp_path, store)
    store.mark(url, PageStatus.CARRIED_FORWARD)
    with pytest.raises(PublicationBlockedError):
        validate_community_publication(tmp_path, store)
    store.mark(url, PageStatus.SUCCESS)
    with pytest.raises(PublicationBlockedError, match="identity set"):
        validate_community_publication(tmp_path, store)
    document = tmp_path / "content/community-f5-com/t/70152/index.md"
    document.parent.mkdir(parents=True)
    document.write_text("placeholder")
    validate_community_publication(tmp_path, store)
    inventory["unresolved"] = [70152]
    target.write_text(json.dumps(inventory))
    with pytest.raises(PublicationBlockedError, match="unresolved"):
        validate_community_publication(tmp_path, store)
    store.close()


@pytest.mark.asyncio
async def test_fetcher_reads_only_topic_json_and_asset_redirect_policy(tmp_path, no_wait):
    from html_to_markdown.fetcher import Fetcher

    calls = []

    def handler(request):
        calls.append(str(request.url))
        if request.url.path.endswith(".json"):
            return httpx.Response(200, json=topic())
        return httpx.Response(302, headers={"Location": "https://example.com/private-image.png"})

    async with Fetcher() as fetcher:
        await fetcher._http.aclose()
        fetcher._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        fetcher.community_client = CommunityClient(fetcher._http)
        fetcher.community_inventory_dir = tmp_path
        result = await fetcher.fetch(CommunityAdapter(), BASE + "/t/old/70152")
        assert result.final_url == BASE + "/t/70152"
        assert result.headers["community-review"] == "{}"
        assert calls == [BASE + "/t/70152.json"]
        assert (
            await fetcher.resolve_redirect("community-f5-com", BASE + "/t/old/70152")
            == BASE + "/t/70152"
        )
        with pytest.raises(AllowlistError):
            await fetcher.fetch_asset(BASE + "/uploads/diagram.png")
        assert "https://example.com/private-image.png" not in calls


def test_community_golden_markdown():
    from pathlib import Path

    root = Path(__file__).parent
    value = json.loads((root / "fixtures/community_article.json").read_text())
    approved = validate_topic(value, 70152)
    page = FetchResult(
        url=BASE + "/t/70152",
        final_url=BASE + "/t/70152",
        status_code=200,
        html=json.dumps(value),
        headers={
            "community-review": json.dumps(
                {
                    "article_hash": article_hash(approved),
                    "decision": "include",
                    "reason": "Reviewed synthetic fixture",
                    "privacy": "approved",
                }
            )
        },
    )
    extracted = CommunityAdapter().extract(page)
    rendered = render_html(extracted.html, page.url)
    assert rendered.body == (root / "golden/community-article.md").read_text()
    assert len(rendered.assets) == 1
    assert rendered.assets[0].alt == "Routing diagram"


@pytest.mark.asyncio
@pytest.mark.parametrize("approved,expected", [(True, 1), (False, 0)])
async def test_media_review_binds_exact_image_bytes(tmp_path, approved, expected):
    import hashlib

    from html_to_markdown.models import DiscoveredPage
    from html_to_markdown.pipeline import Pipeline

    value = validate_topic(
        json.loads(
            (
                __import__("pathlib").Path(__file__).parent / "fixtures/community_article.json"
            ).read_text()
        ),
        70152,
    )
    image = "https://d20hrnpixdzcsd.cloudfront.net/original/routing-diagram.png"
    content = b"synthetic diagram bytes"
    review = {
        "article_hash": article_hash(value),
        "decision": "include",
        "privacy": "approved",
        "assets": {image: hashlib.sha256(content).hexdigest() if approved else "0" * 64},
    }

    class Fetch:
        async def fetch(self, adapter, url):
            return FetchResult(
                url=url,
                final_url=url,
                status_code=200,
                html=json.dumps(value),
                headers={"community-review": json.dumps(review)},
            )

        async def fetch_asset(self, url):
            return content, "image/png"

        async def resolve_redirect(self, source, url):
            return url

    pipeline = Pipeline(tmp_path)
    await pipeline.fetcher.close()
    pipeline.fetcher = Fetch()
    pipeline.store.discover([DiscoveredPage(source_id="community-f5-com", url=BASE + "/t/70152")])
    records = await pipeline.scrape("community-f5-com")
    assert len(records) == expected
    if approved:
        assert "assets/" in records[0].output_path.read_text()
    else:
        assert pipeline.store.rows()[0]["error_class"] == "PublicationBlockedError"
    pipeline.store.close()


def test_privacy_scans_code_link_targets_and_image_labels():
    from html_to_markdown.adapters.community import privacy_findings

    html = '<a href="https://user@example.com/path">Link</a><img src="https://community.f5.com/image.png" alt="user@example.com"><pre>token=aaaaaaaaaaaaaaaaaaaaaaaa</pre>'
    assert privacy_findings(html) == ["credential", "email"]


def test_author_and_lightbox_metadata_are_removed():
    value = topic(
        body='<p>Co-Author: Synthetic Contributor</p><p>Distributed Cloud routing prose.</p><div class="lightbox-wrapper"><a class="lightbox" href="https://community.f5.com/original.png"><img src="/small.png"></a><div class="meta">Download 1000x1000 50 KB</div></div>'
    )
    html = clean_html(value)
    assert "Synthetic Contributor" not in html
    assert "Download" not in html
    assert "Distributed Cloud routing prose" in html
    assert image_urls(html) == [BASE + "/original.png"]


@pytest.mark.parametrize(
    "url",
    [
        "https://user@community.f5.com/t/70152",
        "https://community.f5.com:443/t/70152",
        "http://community.f5.com/t/70152",
        "https://example.com/t/70152",
    ],
)
def test_exact_community_host(url):
    with pytest.raises(AllowlistError):
        validate_source_url("community-f5-com", url)


@pytest.mark.asyncio
async def test_recent_sitemap_page_query_is_identity_only(no_wait):
    def handler(request):
        return httpx.Response(
            200, text=f"<urlset><url><loc>{BASE}/t/old/70152?page=3</loc></url></urlset>"
        )

    client = await client_for(handler)
    assert await client.sitemap_ids() == {70152}
    await client.http.aclose()


@pytest.mark.asyncio
async def test_tag_article_missing_category_pass_blocks(tmp_path):
    client = InventoryClient()
    original = client.listing

    async def listing(url, category_id=None):
        values = await original(url, category_id)
        if category_id is None:
            values[99999] = {"id": 99999, "category_id": 21}
        return values

    client.listing = listing
    with pytest.raises(ScrapeError, match="absent"):
        await inventory_pass(client, tmp_path)


def test_small_unlabelled_logo_is_decoration_but_diagram_is_preserved():
    value = topic(
        body='<p>Distributed Cloud routing.</p><img src="/logo.png" width="32" height="32" alt=""><img src="/diagram.png" width="32" height="32" alt="Routing diagram"><img src="/large.png" width="500" height="300" alt="">'
    )
    assert image_urls(clean_html(value)) == [BASE + "/diagram.png", BASE + "/large.png"]


@pytest.mark.asyncio
async def test_inventory_refreshes_existing_first_posts_and_listings(tmp_path):
    client = InventoryClient()
    await inventory_pass(client, tmp_path)
    original = client.topic
    checked = []

    async def changed(identifier):
        checked.append(identifier)
        value = await original(identifier)
        if identifier == 70152:
            value["post_stream"]["posts"][0]["cooked"] += "<p>New routing detail.</p>"
        return value

    client.topic = changed
    report = await inventory_pass(client, tmp_path)
    assert len(checked) == 9
    updated = next(item for item in report["topics"] if item["id"] == 70152)
    assert updated["article_hash"] == article_hash(await changed(70152))

    async def unavailable_listing(url, category_id=None):
        raise ScrapeError("listing unavailable")

    client.listing = unavailable_listing
    with pytest.raises(ScrapeError, match="listing unavailable"):
        await inventory_pass(client, tmp_path)
    assert not (tmp_path / "inventory.json").exists()


@pytest.mark.asyncio
async def test_inventory_resume_is_explicit_and_reuses_same_pass(tmp_path):
    client = InventoryClient()
    first = await inventory_pass(client, tmp_path)

    async def unavailable(*args):
        raise ScrapeError("network unavailable")

    client.topic = unavailable
    client.listing = unavailable
    resumed = await inventory_pass(client, tmp_path, resume=True)
    assert resumed["started_at"] == first["started_at"]
    assert resumed["topics"] == first["topics"]


@pytest.mark.asyncio
async def test_failed_refresh_cannot_resume_previous_topic_bytes(tmp_path):
    client = InventoryClient()
    await inventory_pass(client, tmp_path)

    async def failure(identifier):
        raise ScrapeError("first post unavailable")

    client.topic = failure
    with pytest.raises(ScrapeError):
        await inventory_pass(client, tmp_path)
    assert not list((tmp_path / "topics").glob("*.json"))
    with pytest.raises(ScrapeError):
        await inventory_pass(client, tmp_path, resume=True)


@pytest.mark.parametrize(
    "body",
    [
        "<p>Distributed Cloud token=aaaaaaaaaaaaaaaaaaaa</p>",
        "<pre>-----BEGIN PRIVATE KEY-----\nSYNTHETIC_TEST_MATERIAL\n-----END PRIVATE KEY-----</pre>",
        "<p>Authorization: Bearer aaaaaaaaaaaaaaaaaaaa</p>",
        "<pre>Set-Cookie: session=aaaaaaaaaaaaaaaaaaaa; Secure; HttpOnly</pre>",
        "<pre>Cookie: session=aaaaaaaaaaaaaaaaaaaa; theme=light</pre>",
    ],
)
def test_privacy_approval_never_overrides_remaining_credential_material(body):
    value = topic(body=body)
    review = {
        "article_hash": article_hash(validate_topic(value, 70152)),
        "decision": "include",
        "privacy": "approved",
        "reason": "Synthetic test approval",
    }
    page = FetchResult(
        url=BASE + "/t/70152",
        final_url=BASE + "/t/70152",
        status_code=200,
        html=json.dumps(value),
        headers={"community-review": json.dumps(review)},
    )
    with pytest.raises(PublicationBlockedError, match="credential"):
        CommunityAdapter().extract(page)
