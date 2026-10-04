"""Qualify a pinned release and captured route inventory before catalog promotion."""

from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import json
import re
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import httpx
from defusedxml import ElementTree

ROOT = "https://f5-sales-demo.github.io/api-specs-enriched/"
METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


class PageIdentity(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.canonical = None
        self.in_main = False
        self.text = []
        self.links = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "link" and values.get("rel") == "canonical":
            self.canonical = values.get("href")
        if tag == "main":
            self.in_main = True
        if tag == "a" and self.in_main:
            self.links.append(values.get("href"))

    def handle_endtag(self, tag):
        if tag == "main":
            self.in_main = False

    def handle_data(self, data):
        if self.in_main and data.strip():
            self.text.append(data.strip())


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def operations(archive: Path, routes: set[str]) -> list[dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    with zipfile.ZipFile(archive) as specs:
        for member in sorted(specs.namelist()):
            if not member.startswith("domains/") or not member.endswith(".json"):
                continue
            spec = json.loads(specs.read(member))
            if "openapi" not in spec or not isinstance(spec.get("paths"), dict):
                continue
            domain = Path(member).stem
            overview = ROOT + f"api-reference/{domain}/"
            if overview not in routes:
                raise ValueError(f"overview missing from captured routes: {overview}")
            for path, methods in sorted(spec["paths"].items()):
                for method, op in sorted(methods.items()):
                    if method not in METHODS:
                        continue
                    identity = op["operationId"]
                    slug = re.sub(r"[^a-z0-9_-]", "", identity.lower())
                    url = ROOT + f"api-reference/{domain}/operations/{slug}/"
                    if url not in routes:
                        raise ValueError(f"operation missing from captured routes: {identity}")
                    resource = identity.rsplit(".", 2)[0].removeprefix("ves.io.schema.")
                    record = {
                        "operation_id": identity,
                        "method": method.upper(),
                        "path": path,
                        "resource": resource,
                        "domain": domain,
                        "url": url,
                        "overview_url": overview,
                        "summary": op.get("summary", identity),
                        "legacy_url": op.get("externalDocs", {}).get("url"),
                        "schema_sha256": hashlib.sha256(specs.read(member)).hexdigest(),
                    }
                    if url in result and result[url] != record:
                        raise ValueError(f"ambiguous specification operation: {identity}")
                    result[url] = record
    return [result[key] for key in sorted(result)]


async def qualify(records: list[dict[str, Any]], fallback: str) -> list[dict[str, Any]]:
    semaphore = asyncio.Semaphore(8)
    async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:

        async def check(url: str, record: dict[str, Any] | None) -> dict[str, Any]:
            async with semaphore:
                response = await client.get(url)
                response.raise_for_status()
                if response.status_code != 200:
                    raise ValueError(f"destination did not return 200: {url}")
                identity = PageIdentity()
                identity.feed(response.text)
                if identity.canonical != url:
                    raise ValueError(f"canonical identity mismatch: {url}")
                if not identity.text:
                    raise ValueError(f"destination has no main content: {url}")
                if record is not None:
                    text = " ".join(identity.text)
                    if record["method"] + " " + record["path"] not in text:
                        raise ValueError(f"method/path identity mismatch: {url}")
                    legacy = record.get("legacy_url")
                    if legacy and legacy not in identity.links:
                        raise ValueError(f"operation source identity mismatch: {url}")
                return {
                    "url": url,
                    "status": 200,
                    "html_sha256": hashlib.sha256(response.content).hexdigest(),
                }

        targets = {record["url"]: record for record in records}
        for record in records:
            targets.setdefault(record["overview_url"], None)
        targets[fallback] = None
        return await asyncio.gather(*(check(url, targets[url]) for url in sorted(targets)))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec-archive", type=Path, required=True)
    parser.add_argument("--spec-sha256", required=True)
    parser.add_argument("--routes", type=Path, required=True)
    parser.add_argument("--routes-sha256", required=True)
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if digest(args.spec_archive) != args.spec_sha256 or digest(args.routes) != args.routes_sha256:
        raise ValueError("pinned input digest mismatch")
    routes = {
        e.text
        for e in ElementTree.fromstring(args.routes.read_bytes()).iter()
        if e.tag.endswith("loc") and e.text
    }
    records = operations(args.spec_archive, routes)
    fallback = ROOT + "en/api-reference/"
    checks = asyncio.run(qualify(records, fallback))
    catalog = {
        "schema_version": 1,
        "specification": {
            "repository": "f5-sales-demo/api-specs-enriched",
            "release_tag": args.release_tag,
            "asset": args.spec_archive.name,
            "sha256": args.spec_sha256,
        },
        "route_inventory": {
            "source_url": ROOT + "sitemap-0.xml",
            "sha256": args.routes_sha256,
            "routes": sorted(routes),
        },
        "fallback": fallback,
        "operations": records,
        "qualification": checks,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(catalog, indent=2, sort_keys=True) + "\n").encode()
    if args.output.suffix == ".gz":
        payload = gzip.compress(payload, mtime=0)
    args.output.write_bytes(payload)
    print(
        f"Qualified {len(records)} operations and {len(checks)} destinations; catalog sha256={digest(args.output)}"
    )


if __name__ == "__main__":
    main()
