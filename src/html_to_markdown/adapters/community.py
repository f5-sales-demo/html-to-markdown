"""Public article inventory and first-post-only community adapter.

Inventory responses discard identity and discussion fields before persistence.
Review decisions bind to the first-post content hash, so edits reopen review.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup, Tag
from defusedxml import ElementTree

from html_to_markdown.community_text import reviewed_text
from html_to_markdown.errors import AllowlistError, PublicationBlockedError, ScrapeError
from html_to_markdown.models import (
    DiscoveredPage,
    ExtractedPage,
    FetchResult,
    PageMetadata,
    utc_now,
)
from html_to_markdown.urls import validate_asset_url, validate_source_url

from .base import SourceAdapter

BASE = "https://community.f5.com"
CATEGORIES = {21: "F5 Technical Articles", 15: "Community CodeShare", 8: "F5 Security Insights"}
CATEGORY_PATHS = {21: "technical-articles", 15: "codeshare", 8: "security"}
REQUIRED_IDS = frozenset({73730, 72834, 72617, 70152, 71412, 72266, 70153})
FOCUS = re.compile(
    r"\b(?:F5\s+)?Distributed[ -]Cloud\b|\bF5[ -]?XC\b|\bXC (?:console|services|platform)\b|\b(?:customer|regional) edge\b|\bves\.io\b|\bVolterra\b|\bVoltMesh\b|\bVoltStack\b",
    re.I,
)
BIG_IP = re.compile(r"\bBIG[ -]?IP\b|\biRules?\b|\bTMOS\b", re.I)
PRIVACY_PATTERNS = {
    "email": re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I),
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "credential": re.compile(
        r"\b(?:api[_-]?key|password|secret|token)\s*[:=]\s*[\"']?[A-Za-z0-9+/=_-]{16,}", re.I
    ),
    "authorization-header": re.compile(
        r"Authorization[\s\"':]+(?:Bearer|APIToken)\s+(?!EXAMPLE_API_TOKEN)[A-Za-z0-9+/=_-]{16,}",
        re.I,
    ),
    "cloud-account": re.compile(r"\b\d{12}\b"),
    "resource-id": re.compile(r"\b(?:vpc|subnet|rtb|i|sg)-[a-f0-9]{8,}\b", re.I),
    "uuid": re.compile(r"\b[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\b", re.I),
    "tenant-host": re.compile(
        r"https://(?!example-corp\.)[a-z0-9-]+\.console\.(?:ves\.volterra\.io|ves\.io)", re.I
    ),
    "ipv4": re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
}


def topic_id(url: str) -> int:
    canonical = validate_source_url("community-f5-com", url)
    return int(urlsplit(canonical).path.rsplit("/", 1)[-1])


def is_topic_id(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def validate_topic(value: Any, expected_id: int) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or not is_topic_id(value.get("id"))
        or value["id"] != expected_id
    ):
        raise ScrapeError("community topic identity mismatch")
    if (
        value.get("category_id") not in CATEGORIES
        or value.get("visible") is not True
        or value.get("deleted_at")
    ):
        raise ScrapeError("community topic is not a visible article")
    posts = value.get("post_stream", {}).get("posts", [])
    first = [post for post in posts if post.get("post_number") == 1]
    if (
        len(first) != 1
        or not isinstance(first[0].get("cooked"), str)
        or not first[0]["cooked"].strip()
    ):
        raise ScrapeError("community topic has no unique first post")
    if not isinstance(value.get("title"), str) or not isinstance(value.get("slug"), str):
        raise ScrapeError("community topic has invalid article metadata")
    for field in ("created_at", "updated_at"):
        try:
            datetime.fromisoformat(first[0][field].replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise ScrapeError("community first post has invalid dates") from error
    tags = value.get("tags", [])
    if not isinstance(tags, list) or any(not isinstance(tag, (str, dict)) for tag in tags):
        raise ScrapeError("community topic has invalid tags")
    return {
        "id": expected_id,
        "slug": value["slug"],
        "title": value["title"],
        "category_id": value["category_id"],
        "visible": True,
        "tags": [tag if isinstance(tag, str) else str(tag.get("name", "")) for tag in tags],
        "post_stream": {
            "posts": [
                {
                    key: first[0][key]
                    for key in ("post_number", "cooked", "created_at", "updated_at")
                }
            ]
        },
    }


def first_post(value: dict[str, Any]) -> dict[str, Any]:
    return dict(value["post_stream"]["posts"][0])


def article_hash(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def relevance(value: dict[str, Any]) -> tuple[str, str]:
    title = str(value["title"])
    body = BeautifulSoup(first_post(value)["cooked"], "html.parser").get_text(" ", strip=True)
    scan = " ".join([title, str(value.get("excerpt", "")), " ".join(value.get("tags", [])), body])
    if not FOCUS.search(scan):
        return (
            "exclude",
            "No reviewed Distributed Cloud name or alias in title, tags, excerpt or first post",
        )
    if FOCUS.search(title) and not BIG_IP.search(title):
        return (
            "include",
            "Distributed Cloud focused title; BIG-IP body mentions are incidental or integration context",
        )
    if BIG_IP.search(title) and not FOCUS.search(body):
        return "exclude", "BIG-IP focused title and body; community tag alone is insufficient"
    return "review", "Alias-only, mixed-product or body-only relevance requires first-post review"


def clean_html(value: dict[str, Any]) -> str:
    soup = BeautifulSoup(first_post(value)["cooked"], "html.parser")
    for node in soup.select(
        ".emoji, .mention, .avatar, .anchor, .lightbox-wrapper .meta, script, style, button, form, iframe"
    ):
        node.decompose()
    for paragraph in soup.find_all("p"):
        if re.match(
            r"^(?:co[ -]?authors?|authors?|written by|posted by)\s*:",
            paragraph.get_text(" ", strip=True),
            re.I,
        ):
            paragraph.decompose()
    for image in soup.find_all("img"):
        try:
            small = (
                int(str(image.get("width", "0"))) <= 32
                and int(str(image.get("height", "0"))) <= 32
                and image.get("width")
                and image.get("height")
            )
        except ValueError:
            small = False
        if small and not image.get("alt"):
            image.decompose()
    for anchor in soup.select("a.lightbox"):
        image = anchor.find("img")
        if isinstance(image, Tag) and anchor.get("href"):
            image["src"] = str(anchor["href"])
            anchor.unwrap()
    for node in soup.select("[data-user-id], [data-avatar-template]"):
        node.decompose()
    for anchor in soup.find_all("a"):
        if anchor.get("href"):
            anchor["href"] = str(anchor["href"])
        anchor.attrs = {
            key: value for key, value in anchor.attrs.items() if key in {"href", "title"}
        }
    return str(soup)


def privacy_findings(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    text = " ".join(
        [
            soup.get_text(" ", strip=True),
            *[
                str(node.get(field, ""))
                for node in soup.find_all(True)
                for field in ("alt", "title", "href", "src")
            ],
        ]
    )
    return sorted(name for name, pattern in PRIVACY_PATTERNS.items() if pattern.search(text))


def image_urls(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    return sorted(
        {
            urljoin(BASE, str(image.get("src") or image.get("data-src")))
            for image in soup.find_all("img")
            if image.get("src") or image.get("data-src")
        }
    )


class CommunityClient:
    """One global two-request-per-second clock shared by inventory and scrape."""

    def __init__(self, http: httpx.AsyncClient, retries: int = 5) -> None:
        self.http = http
        self.retries = retries
        self.lock = asyncio.Lock()
        self.next_request = 0.0

    @staticmethod
    def validate_request_url(current: str, expected_id: int | None, asset: bool) -> None:
        parts = urlsplit(current)
        if asset:
            validate_asset_url(current)
        elif (
            parts.scheme != "https"
            or parts.netloc != "community.f5.com"
            or parts.path.startswith(("/search", "/admin", "/auth", "/users", "/u/"))
        ):
            raise ScrapeError("community retrieval escaped public host or route")
        if expected_id is not None:
            path = parts.path.removesuffix(".json")
            if topic_id(BASE + path) != expected_id:
                raise ScrapeError("community redirect changed topic identity")

    async def request(
        self, url: str, *, expected_id: int | None = None, asset: bool = False
    ) -> httpx.Response:
        current = url
        for _ in range(10):
            for attempt in range(self.retries):
                self.validate_request_url(current, expected_id, asset)
                async with self.lock:
                    await asyncio.sleep(max(0, self.next_request - time.monotonic()))
                    self.next_request = time.monotonic() + 0.5
                try:
                    response = await self.http.get(
                        current,
                        headers={"User-Agent": "f5-html-to-markdown/1.3"},
                        follow_redirects=False,
                    )
                except (httpx.TimeoutException, httpx.NetworkError):
                    if attempt + 1 == self.retries:
                        raise ScrapeError(
                            "community retrieval exhausted transient retries"
                        ) from None
                    await asyncio.sleep(2**attempt)
                    continue
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt + 1 == self.retries:
                        raise ScrapeError("community retrieval exhausted status retries")
                    retry_after = response.headers.get("retry-after", "")
                    try:
                        delay = float(retry_after)
                    except ValueError:
                        try:
                            delay = (
                                parsedate_to_datetime(retry_after) - datetime.now(UTC)
                            ).total_seconds()
                        except (ValueError, TypeError):
                            delay = 2**attempt
                    async with self.lock:
                        self.next_request = max(self.next_request, time.monotonic() + max(0, delay))
                    continue
                if response.status_code in {301, 302, 303, 307, 308}:
                    if not response.headers.get("location"):
                        raise ScrapeError("community redirect has no Location")
                    current = urljoin(current, response.headers["location"])
                    break
                if response.status_code != 200:
                    raise ScrapeError(
                        f"community public retrieval returned HTTP {response.status_code}"
                    )
                if str(response.url) != current:
                    raise ScrapeError("community HTTP client followed an unvalidated redirect")
                return response
            else:
                raise ScrapeError("community request did not complete")
        raise ScrapeError("community redirect limit exceeded")

    async def json(self, url: str, *, expected_id: int | None = None) -> dict[str, Any]:
        response = await self.request(url, expected_id=expected_id)
        try:
            value = response.json()
        except ValueError as error:
            raise ScrapeError("community returned malformed JSON") from error
        if not isinstance(value, dict):
            raise ScrapeError("community JSON must be an object")
        return value

    async def topic(self, identifier: int) -> dict[str, Any]:
        return validate_topic(
            await self.json(f"{BASE}/t/{identifier}.json", expected_id=identifier), identifier
        )

    async def listing(
        self, initial: str, category_id: int | None = None
    ) -> dict[int, dict[str, Any]]:
        result: dict[int, dict[str, Any]] = {}
        current = initial
        seen: set[str] = set()
        while current:
            if current in seen:
                raise ScrapeError("community pagination loop")
            seen.add(current)
            data = await self.json(current)
            listing = data.get("topic_list")
            if not isinstance(listing, dict) or not isinstance(listing.get("topics"), list):
                raise ScrapeError("community listing is malformed")
            for item in listing["topics"]:
                if not is_topic_id(item.get("id")) or not is_topic_id(item.get("category_id")):
                    raise ScrapeError("community listing has invalid topic identity")
                if category_id is not None and item["category_id"] != category_id:
                    raise ScrapeError("community category listing escaped selected category")
                result[item["id"]] = {key: item[key] for key in ("id", "category_id")}
                if isinstance(item.get("excerpt"), str):
                    result[item["id"]]["excerpt"] = item["excerpt"]
            more = listing.get("more_topics_url")
            if more:
                parts = urlsplit(urljoin(BASE, more))
                expected_path = urlsplit(initial).path.removesuffix(".json")
                if (
                    parts.scheme != "https"
                    or parts.netloc != "community.f5.com"
                    or parts.path != expected_path
                ):
                    raise ScrapeError("community pagination escaped listing")
                if not listing["topics"]:
                    raise ScrapeError("empty community listing has a next page")
                current = parts._replace(path=parts.path + ".json").geturl()
            else:
                current = ""
        if not result:
            raise ScrapeError("community listing is empty")
        return result

    async def sitemap_ids(self) -> set[int]:
        pending = [f"{BASE}/sitemap.xml"]
        seen: set[str] = set()
        ids: set[int] = set()
        while pending:
            url = pending.pop()
            if url in seen:
                raise ScrapeError("community sitemap loop")
            seen.add(url)
            response = await self.request(url)
            try:
                root = ElementTree.fromstring(response.content)
            except ValueError as error:
                raise ScrapeError("community sitemap is malformed") from error
            kind = root.tag.rsplit("}", 1)[-1]
            if kind not in {"sitemapindex", "urlset"}:
                raise ScrapeError("community sitemap has invalid root")
            for entry in root:
                location = entry.findtext("{*}loc")
                if not location:
                    raise ScrapeError("community sitemap has empty location")
                if kind == "sitemapindex":
                    if not re.fullmatch(
                        r"https://community\.f5\.com/sitemap(?:_\d+|_recent)?\.xml", location
                    ):
                        raise ScrapeError("community sitemap escaped host or route")
                    pending.append(location)
                elif urlsplit(location).path.startswith("/t/"):
                    parts = urlsplit(location)
                    if parts.query and not re.fullmatch(r"page=[1-9][0-9]*", parts.query):
                        raise ScrapeError("community sitemap topic has unexpected query")
                    ids.add(topic_id(parts._replace(query="").geturl()))
        if not ids:
            raise ScrapeError("community sitemap has no topics")
        return ids


class CommunityAdapter(SourceAdapter):
    source_id = "community-f5-com"
    root_url = BASE

    def needs_browser(self, html: str) -> bool:
        return False

    async def discover(self, fetcher: Any) -> list[DiscoveredPage]:
        inventory = await inventory_pass(fetcher.community_client, fetcher.community_inventory_dir)
        return [
            DiscoveredPage(
                source_id=self.source_id,
                url=f"{BASE}/t/{item['id']}",
                source_last_modified=item["updated_at"],
            )
            for item in inventory["topics"]
            if item["decision"] == "include"
        ]

    def readiness_script(self) -> str | None:
        return None

    async def rendered_html(self, playwright_page: Any) -> str:
        raise ScrapeError("community articles must use public first-post JSON")

    def classify_page(self, page: FetchResult) -> None:
        validate_topic(json.loads(page.html), topic_id(page.url))
        if topic_id(page.final_url) != topic_id(page.url):
            raise ScrapeError("community final URL changed topic identity")

    def extract(self, page: FetchResult) -> ExtractedPage:
        self.classify_page(page)
        value = validate_topic(json.loads(page.html), topic_id(page.url))
        decision, _ = relevance(value)
        review = json.loads(page.headers.get("community-review", "{}"))
        if review.get("article_hash") != article_hash(value):
            review = {}
        if review.get("decision"):
            decision = review["decision"]
        if decision != "include":
            raise PublicationBlockedError("community relevance review is unresolved or excluded")
        html = reviewed_text(clean_html(value), review)
        if (privacy_findings(html) or image_urls(html)) and review.get("privacy") != "approved":
            raise PublicationBlockedError("community text and media privacy review is unresolved")
        for url in image_urls(html):
            validate_asset_url(url)
        return ExtractedPage(metadata=self.normalize_metadata(page, html), html=html)

    def normalize_metadata(self, page: FetchResult, html: str) -> PageMetadata:
        value = validate_topic(json.loads(page.html), topic_id(page.url))
        post = first_post(value)
        category = CATEGORIES[value["category_id"]]
        text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
        return PageMetadata(
            sourceId=self.source_id,
            title=value["title"],
            slug=value["slug"],
            url=validate_source_url(self.source_id, page.url),
            category=category,
            publication_date=post["created_at"][:10],
            modification_date=post["updated_at"][:10],
            description=text[:240],
            tags=value["tags"],
            breadcrumb=[category],
        )


async def capture_inventory(
    client: CommunityClient, output: Path
) -> tuple[str, dict[int, Any], dict[int, Any], set[int]]:
    checkpoint = output / "pass.json"
    if checkpoint.exists():
        saved = json.loads(checkpoint.read_text())
        started = saved["started_at"]
        listings = {int(key): value for key, value in saved["listings"].items()}
        tagged = {int(key): value for key, value in saved["tagged"].items()}
        sitemap = set(saved["sitemap"])
    else:
        started = utc_now()
        listings = {}
        for identifier, slug in CATEGORY_PATHS.items():
            listings.update(
                await client.listing(f"{BASE}/c/articles/{slug}/{identifier}.json", identifier)
            )
        tagged = await client.listing(f"{BASE}/tag/f5-distributed-cloud.json")
        sitemap = await client.sitemap_ids()
        checkpoint.write_text(
            json.dumps(
                {
                    "started_at": started,
                    "listings": listings,
                    "tagged": tagged,
                    "sitemap": sorted(sitemap),
                }
            )
        )
    return started, listings, tagged, sitemap


async def inventory_pass(client: CommunityClient, output: Path) -> dict[str, Any]:
    """Finish all first-post reads before admitting any community document."""
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    topic_dir = output / "topics"
    topic_dir.mkdir(exist_ok=True, mode=0o700)
    started, listings, tagged, sitemap = await capture_inventory(client, output)
    tag_articles = {
        identifier for identifier, item in tagged.items() if item["category_id"] in CATEGORIES
    }
    if tag_articles - set(listings):
        raise ScrapeError(
            "community tag contains article IDs absent from exhausted category listings"
        )
    reviews_path = output / "reviews.json"
    baseline = Path(__file__).with_name("community_reviews.json")
    reviews = json.loads(baseline.read_text()) if baseline.exists() else {}
    if reviews_path.exists():
        reviews.update(json.loads(reviews_path.read_text()))
    records: list[dict[str, Any]] = []
    semaphore = asyncio.Semaphore(4)

    async def inspect(identifier: int) -> None:
        async with semaphore:
            target = topic_dir / f"{identifier}.json"
            if target.exists():
                value = validate_topic(json.loads(target.read_text()), identifier)
            else:
                value = await client.topic(identifier)
                target.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
                target.chmod(0o600)
            digest = article_hash(value)
            decision, reason = relevance(
                {**value, "excerpt": listings[identifier].get("excerpt", "")}
            )
            review = reviews.get(str(identifier), {})
            if (
                review.get("article_hash") == digest
                and review.get("reason")
                and review.get("decision") in {"include", "exclude"}
            ):
                decision, reason = review["decision"], review["reason"]
            html = (
                reviewed_text(clean_html(value), review)
                if review.get("article_hash") == digest
                else clean_html(value)
            )
            images = image_urls(html)
            unknown: list[str] = []
            for image in images:
                try:
                    validate_asset_url(image)
                except AllowlistError:
                    unknown.append(urlsplit(image).hostname or "")
            privacy = privacy_findings(html)
            approved = review.get("article_hash") == digest and review.get("privacy") == "approved"
            records.append(
                {
                    "id": identifier,
                    "url": f"{BASE}/t/{identifier}",
                    "category": CATEGORIES[value["category_id"]],
                    "title": value["title"],
                    "article_hash": digest,
                    "decision": decision,
                    "reason": reason,
                    "tagged": identifier in tagged,
                    "in_sitemap": identifier in sitemap,
                    "updated_at": first_post(value)["updated_at"],
                    "privacy_findings": privacy,
                    "image_count": len(images),
                    "unknown_image_hosts": sorted(set(unknown)),
                    "privacy_review": "approved"
                    if approved
                    else ("required" if privacy or images else "clear"),
                }
            )
            if len(records) % 100 == 0:
                print(f"community first-post progress: {len(records)}/{len(listings)}", flush=True)

    try:
        await asyncio.gather(
            *(
                inspect(identifier)
                for identifier in sorted(
                    listings,
                    key=lambda identifier: (
                        identifier not in REQUIRED_IDS,
                        identifier not in tagged,
                        -identifier,
                    ),
                )
            )
        )
    except Exception:
        (output / "incomplete.json").write_text(
            json.dumps({"started_at": started, "checked": len(records), "expected": len(listings)})
        )
        raise
    unresolved = [
        item["id"]
        for item in records
        if item["decision"] == "review"
        or (
            item["decision"] == "include"
            and (item["privacy_review"] == "required" or item["unknown_image_hosts"])
        )
    ]
    report = {
        "schema_version": 1,
        "started_at": started,
        "ended_at": utc_now(),
        "complete": True,
        "topic_count": len(listings),
        "sitemap_topic_count": len(sitemap),
        "tag_topic_count": len(tagged),
        "listing_missing_sitemap": sorted(set(listings) - sitemap),
        "tag_listing_missing_sitemap": sorted((set(tagged) & set(listings)) - sitemap),
        "unresolved": sorted(unresolved),
        "topics": sorted(records, key=lambda item: item["id"]),
    }
    (output / "inventory.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    included = {item["id"] for item in records if item["decision"] == "include"}
    if not included >= REQUIRED_IDS:
        raise PublicationBlockedError(
            "community inventory does not include all required example IDs"
        )
    if unresolved:
        raise PublicationBlockedError(
            f"community inventory has {len(unresolved)} unresolved relevance or privacy reviews"
        )
    return report


def validate_community_publication(output: Path, store: Any) -> None:
    """Prevent advisory reconciliation from publishing partial or rejected articles."""
    rows = store.rows(["community-f5-com"])
    documents = sorted(output.glob("content/community-f5-com/**/index.md"))
    if not rows and not documents:
        return
    inventory_path = output / "community-inventory/inventory.json"
    if not inventory_path.is_file():
        raise PublicationBlockedError(
            "community publication requires a complete reviewed inventory"
        )
    inventory = json.loads(inventory_path.read_text())
    if inventory.get("complete") is not True or inventory.get("unresolved"):
        raise PublicationBlockedError(
            "community publication has incomplete or unresolved inventory"
        )
    accepted = {item["url"] for item in inventory["topics"] if item["decision"] == "include"}
    actual = {row["canonical_url"] for row in rows if row["status"] == "success"}
    if actual != accepted or any(row["status"] != "success" for row in rows):
        raise PublicationBlockedError(
            "community publication must contain every accepted article exactly once"
        )
    paths = {f"content/community-f5-com/t/{topic_id(url)}/index.md" for url in accepted}
    if {path.relative_to(output).as_posix() for path in documents} != paths:
        raise PublicationBlockedError(
            "community publication document identity set differs from review"
        )
