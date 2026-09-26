"""URL boundaries and deterministic output paths."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from .errors import AllowlistError

SOURCE_ROOTS = {
    "docs-cloud-f5-com": "https://docs.cloud.f5.com/docs-v2",
    "my-f5-com": "https://my.f5.com/manage/s",
}

ASSET_HOSTS = frozenset(
    {"docs.cloud.f5.com", "my.f5.com", "cdn.f5.com", "techdocs.f5.com", "f5.file.force.com"}
)


def canonicalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    port = parts.port
    netloc = host if port in {None, 443} else f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if path != "/":
        path = path.rstrip("/")
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return urlunsplit((scheme, netloc, quote(path, safe="/%:@-._~"), query, ""))


def validate_source_url(source_id: str, url: str) -> str:
    if source_id not in SOURCE_ROOTS:
        raise AllowlistError(f"unknown source: {source_id}")
    canonical = canonicalize_url(url)
    root = urlsplit(SOURCE_ROOTS[source_id])
    value = urlsplit(canonical)
    root_path = root.path.rstrip("/")
    if value.scheme != "https" or value.hostname != root.hostname:
        raise AllowlistError(f"URL is outside {source_id}: {url}")
    if value.path != root_path and not value.path.startswith(f"{root_path}/"):
        raise AllowlistError(f"URL path is outside {root_path}: {url}")
    return canonical


def validate_asset_url(url: str) -> str:
    canonical = canonicalize_url(url)
    parts = urlsplit(canonical)
    if parts.scheme != "https" or parts.hostname not in ASSET_HOSTS:
        raise AllowlistError(f"asset URL is outside the F5 allowlist: {url}")
    return canonical


def stable_path(source_id: str, url: str) -> PurePosixPath:
    canonical = validate_source_url(source_id, url)
    parts = urlsplit(canonical)
    root = urlsplit(SOURCE_ROOTS[source_id]).path.rstrip("/")
    relative = parts.path[len(root) :].strip("/")
    if source_id == "my-f5-com":
        match = re.search(r"K\d{6,}", relative, re.IGNORECASE)
        relative = match.group(0).upper() if match else relative
    safe = [
        re.sub(r"[^A-Za-z0-9._-]+", "-", item).strip(".-") or "index"
        for item in relative.split("/")
        if item
    ]
    return PurePosixPath(*safe) if safe else PurePosixPath("index")
