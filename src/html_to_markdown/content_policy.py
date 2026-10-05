"""Digest-pinned exclusions and exact, offline API reference migration."""

from __future__ import annotations

import gzip
import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .curation import (
    corpus_digest,
    curate_topics,
    json_bytes,
    load_curation_policy,
    validate_curation,
)
from .models import PageMetadata
from .render import serialize_document, split_document
from .state import StateStore

DESTINATION_ROOT = "https://f5-sales-demo.github.io/api-specs-enriched/"
FALLBACK = DESTINATION_ROOT + "en/api-reference/"
FAMILIES = (
    "https://docs.cloud.f5.com/docs-v2/api",
    "https://docs.cloud.f5.com/docs-v2/platform/reference/api-ref",
)
URL_TOKEN = re.compile(
    r"""(?:https?://[^\s<>"'`\[\]()]+|//docs\.cloud\.f5\.com[^\s<>"'`\[\]()]+|(?<![\w:/])(?:/?docs-v2/|(?:\.\./)+|\./|api/|platform/reference/api-ref/)[^\s<>"'`\[\]()]+)"""
)
SECTION = "<!-- content-policy:related-api -->"


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def _normalized(value: str) -> str:
    return value.casefold().replace("_", "-").replace(".", "-")


def _canonical(value: str) -> str:
    parsed = urlsplit(value)
    path = unquote(parsed.path).rstrip("/")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, "", ""))


def _destination(value: str) -> str:
    # Validate the complete URL boundary in one predicate.
    # pylint: disable=too-many-boolean-expressions
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "f5-sales-demo.github.io"
        or parsed.query
        or parsed.fragment
        or unquote(parsed.path) != parsed.path
        or any(part in {".", ".."} for part in parsed.path.split("/"))
        or not value.endswith("/")
        or (not value.startswith(DESTINATION_ROOT + "api-reference/") and value != FALLBACK)
    ):
        raise ValueError(f"destination outside canonical API boundary: {value}")
    return value


class ContentPolicy:
    # Exact identity indexes intentionally stay together with their pinned inputs.
    # pylint: disable=too-many-instance-attributes
    """Validated policy and its compact specification/route projection."""

    def __init__(
        self, raw: dict[str, Any], catalog: dict[str, Any], policy_digest: str, catalog_digest: str
    ) -> None:
        self.raw, self.catalog = raw, catalog
        self.policy_digest, self.catalog_digest = policy_digest, catalog_digest
        if raw.get("schema_version") != 1 or catalog.get("schema_version") != 1:
            raise ValueError("unsupported content policy/catalog schema")
        if raw.get("exclusions") != list(FAMILIES):
            raise ValueError("exclusion policy must contain the two exact approved families")
        self.fallback = _destination(raw["fallback"])
        if self.fallback != FALLBACK or catalog.get("fallback") != self.fallback:
            raise ValueError("catalog fallback mismatch")
        self.operations = [
            op
            for op in catalog["operations"]
            if op["resource"].rsplit(".", 1)[-1] not in load_curation_policy().retired_identities
        ]
        self.by_operation: dict[str, list[dict[str, Any]]] = {}
        self.by_legacy: dict[str, list[dict[str, Any]]] = {}
        self.by_resource: dict[str, list[dict[str, Any]]] = {}
        self.by_fragment: dict[str, list[dict[str, Any]]] = {}
        destinations = {self.fallback}
        for op in self.operations:
            for key in (
                "operation_id",
                "method",
                "path",
                "resource",
                "domain",
                "url",
                "overview_url",
            ):
                if not isinstance(op.get(key), str) or not op[key]:
                    raise ValueError(f"malformed operation field: {key}")
            if op["method"] not in {
                "GET",
                "POST",
                "PUT",
                "PATCH",
                "DELETE",
                "HEAD",
                "OPTIONS",
            } or not op["path"].startswith("/"):
                raise ValueError("malformed operation method/path")
            destinations.update({_destination(op["url"]), _destination(op["overview_url"])})
            self.by_operation.setdefault(op["operation_id"], []).append(op)
            self.by_operation.setdefault(op["operation_id"].replace(".", "-").lower(), []).append(
                op
            )
            self.by_resource.setdefault(_normalized(op["resource"]), []).append(op)
            fragment = (
                _normalized(op["resource"]) + "#" + op["operation_id"].rsplit(".", 1)[-1].casefold()
            )
            self.by_fragment.setdefault(fragment, []).append(op)
            legacy = op.get("legacy_url")
            if legacy:
                self.by_legacy.setdefault(_canonical(legacy), []).append(op)
        self.overrides: dict[str, str] = {}
        for item in raw.get("overrides", []):
            legacy, target = item["legacy_url"], _destination(item["destination"])
            if not self.excludes(legacy) or target not in destinations or legacy in self.overrides:
                raise ValueError("malformed or unqualified explicit mapping")
            self.overrides[legacy] = target
        if "qualification" in catalog:
            checks = catalog["qualification"]
            if any(item.get("status") != 200 for item in checks) or not destinations <= {
                item["url"] for item in checks
            }:
                raise ValueError("catalog destination qualification incomplete")
            routes = set(catalog["route_inventory"]["routes"])
            if not (destinations - {self.fallback}) <= routes:
                raise ValueError("catalog destination absent from pinned route inventory")

    @classmethod
    def from_catalog(cls, raw: dict[str, Any], catalog: dict[str, Any]) -> ContentPolicy:
        return cls(raw, catalog, _digest(_json(raw).encode()), _digest(_json(catalog).encode()))

    def excludes(self, url: str) -> bool:
        canonical = _canonical(url)
        return load_curation_policy().excludes(url) or any(
            canonical == family or canonical.startswith(family + "/")
            for family in self.raw["exclusions"]
        )

    def resolve(self, url: str) -> dict[str, Any]:
        """Resolve only exact declared identities; retain all ambiguous candidates."""
        if load_curation_policy().excludes(url):
            raise ValueError("retired reference has no replacement")
        if not self.excludes(url):
            raise ValueError(f"reference outside excluded families: {url}")
        canonical = _canonical(url)
        parsed = urlsplit(url)
        fragment = unquote(parsed.fragment).removeprefix("operation/")
        leaf = unquote(parsed.path).rstrip("/").rsplit("/", 1)[-1]
        candidates: list[dict[str, Any]] = []
        reason = "unmatched"
        if url in self.overrides or canonical in self.overrides:
            return {
                "legacy_url": url,
                "destination": self.overrides.get(url, self.overrides.get(canonical)),
                "reason": "explicit_override",
                "candidates": [],
                "related_operations": [],
            }
        for identity in [fragment, leaf]:
            if identity in self.by_operation:
                candidates = self.by_operation[identity]
                break
        if not candidates:
            candidates = self.by_legacy.get(canonical, [])
        if not candidates and fragment:
            candidates = self.by_fragment.get(_normalized(leaf) + "#" + fragment.casefold(), [])
        if candidates:
            urls = sorted({op["url"] for op in candidates})
            reason = "exact_operation" if len(urls) == 1 else "ambiguous_operation"
            target = urls[0] if len(urls) == 1 else self.fallback
            related: list[str] = []
        else:
            candidates = self.by_resource.get(_normalized(leaf), [])
            identities = {(op["resource"], op["domain"]) for op in candidates}
            if len(identities) == 1:
                reason, target = "exact_resource", candidates[0]["overview_url"]
                related = sorted({op["url"] for op in candidates})
            else:
                reason = "ambiguous_resource" if identities else "unmatched"
                target, related = self.fallback, []
            urls = sorted({op["url"] for op in candidates})
        return {
            "legacy_url": url,
            "destination": target,
            "reason": reason,
            "candidates": urls,
            "related_operations": related,
        }


@lru_cache(maxsize=8)
def load_content_policy(path: Path | None = None) -> ContentPolicy:
    path = path or Path(__file__).with_name("content_policy.json")
    policy_bytes = path.read_bytes()
    raw = json.loads(policy_bytes)
    identity = raw["catalog"]
    catalog_path = (path.parent / identity["path"]).resolve()
    if catalog_path.parent != path.parent.resolve():
        raise ValueError("catalog path escapes policy directory")
    catalog_bytes = catalog_path.read_bytes()
    if _digest(catalog_bytes) != identity["sha256"]:
        raise ValueError("catalog digest mismatch")
    decoded = gzip.decompress(catalog_bytes) if catalog_path.suffix == ".gz" else catalog_bytes
    return ContentPolicy(raw, json.loads(decoded), _digest(policy_bytes), _digest(catalog_bytes))


def record_exclusion(store: StateStore, url: str, origin: str) -> None:
    with store.connection:
        store.connection.execute(
            "CREATE TABLE IF NOT EXISTS content_exclusions (url TEXT, origin TEXT, PRIMARY KEY(url, origin))"
        )
        store.connection.execute(
            "INSERT OR IGNORE INTO content_exclusions VALUES (?, ?)", (url, origin)
        )


def purge_excluded_state(store: StateStore, policy: ContentPolicy | None = None) -> None:
    active = policy or load_content_policy()
    for row in store.rows():
        if active.excludes(row["canonical_url"]):
            record_exclusion(store, row["canonical_url"], "state")
            with store.connection:
                store.connection.execute(
                    "DELETE FROM pages WHERE canonical_url=?", (row["canonical_url"],)
                )


def rewrite_text(
    text: str, base_url: str, policy: ContentPolicy
) -> tuple[str, list[dict[str, Any]]]:
    mappings: dict[str, dict[str, Any]] = {}

    def replace(match: re.Match[str]) -> str:
        token = match.group()
        # Sentence punctuation is outside the URL; preserve it byte for byte.
        value = token.rstrip(".,;:")
        candidate = urljoin(base_url, "/" + value if value.startswith("docs-v2/") else value)
        if load_curation_policy().excludes(candidate):
            # Examination never manufactures an API destination for retirement.
            return token
        if not policy.excludes(candidate):
            return token
        mapping = policy.resolve(candidate)
        mappings[candidate] = mapping
        return str(mapping["destination"]) + token[len(value) :]

    return URL_TOKEN.sub(replace, text), [mappings[key] for key in sorted(mappings)]


def _description(body: str) -> str | None:
    text = re.sub(r"```.*?```", "", body, flags=re.DOTALL)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
    lines = [
        re.sub(r"[*_`>#]", "", line).strip()
        for line in text.splitlines()
        if line.strip() and not line.startswith(("#", "<!--"))
    ]
    return " ".join(" ".join(lines).split())[:240] or None


def _previous_evidence(output: Path, policy: ContentPolicy, current_sha256: str) -> dict[str, Any]:
    path = output / "curation-audit.json"
    if not path.is_file():
        return {}
    audit = json.loads(path.read_text())
    if audit.get("output_sha256") != current_sha256:
        return {}
    value = audit.get("content_migration", {})
    return (
        value if value.get("input_digests", {}).get("policy_sha256") == policy.policy_digest else {}
    )


def _content_lineage_digest(output: Path) -> str:
    """Hash bodies and assets without metadata rebuilt by later stages."""
    entries = []
    for path in sorted(output.glob("content/**/*")):
        if not path.is_file():
            continue
        payload = (
            split_document(path.read_text(encoding="utf-8"))[1].encode()
            if path.name == "index.md"
            else path.read_bytes()
        )
        entries.append((path.relative_to(output).as_posix(), _digest(payload)))
    return _digest(json_bytes(entries))


def migrate_content(
    output: Path,
    store: StateStore | None = None,
    *,
    policy: ContentPolicy | None = None,
    apply: bool = True,
) -> dict[str, Any]:
    """Examine or apply a snapshot transaction without fetching any input."""
    # One transaction keeps document, asset and evidence counts consistent.
    # pylint: disable=too-many-locals,too-many-branches,too-many-statements
    active = policy or load_content_policy()
    topic_policy = load_curation_policy()
    audit_path = output / "curation-audit.json"
    before = corpus_digest(output)
    prior_audit = json.loads(audit_path.read_text()) if audit_path.is_file() else {}
    policies_match = (
        prior_audit.get("policy_sha256") == topic_policy.sha256
        and prior_audit.get("api_policy_sha256") == active.policy_digest
    )
    audit_matches = policies_match and (
        prior_audit.get("output_sha256") == before
        or bool(prior_audit.get("content_lineage_sha256"))
        and prior_audit["content_lineage_sha256"] == _content_lineage_digest(output)
    )
    if apply and audit_matches:
        if store:
            purge_excluded_state(store, active)
        validate_content_policy(output, active)
        if prior_audit["output_sha256"] != before:
            prior_audit["output_sha256"] = before
            audit_path.write_bytes(json_bytes(prior_audit))
        return dict(prior_audit["content_migration"])
    topics = curate_topics(output, store, apply=apply)
    planned = topics.pop("_planned")
    old = _previous_evidence(output, active, before)
    excluded = {item["url"]: item for item in old.get("exclusions", [])}
    removed_assets = set(old.get("removed_assets", [])) | set(topics["removed_assets"])
    findings = {item["document"]: item for item in old.get("affected_documents", [])}
    images: list[dict[str, Any]] = []
    stages: list[dict[str, Any]] = []
    if store:
        if apply:
            purge_excluded_state(store, active)
        tables = {
            r[0]
            for r in store.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "content_exclusions" in tables:
            for row in store.connection.execute(
                "SELECT url, origin FROM content_exclusions ORDER BY url, origin"
            ):
                item = excluded.setdefault(row[0], {"url": row[0], "origins": [], "document": None})
                item["origins"] = sorted(set(item["origins"]) | {row[1]})
    retained: list[tuple[Path, str]] = []
    for path in sorted(output.glob("content/*/**/index.md")):
        relative = path.relative_to(output).as_posix()
        if relative not in planned:
            continue
        metadata_raw, body = (
            split_document(path.read_text(encoding="utf-8"))
            if apply
            else (planned[relative]["metadata"], planned[relative]["body"])
        )
        url = str(metadata_raw["url"])
        if active.excludes(url) or active.excludes(str(metadata_raw.get("canonical_url", url))):
            item = excluded.setdefault(url, {"url": url, "origins": [], "document": relative})
            item["document"] = relative
            item["origins"] = sorted(set(item["origins"]) | {"content"})
            if apply:
                path.unlink()
                if store:
                    with store.connection:
                        store.connection.execute("DELETE FROM pages WHERE canonical_url=?", (url,))
            continue
        metadata = PageMetadata.model_validate(metadata_raw)
        migrated, mappings = rewrite_text(body, metadata.canonical_url or url, active)
        previous = findings.get(relative, {})
        # Keep original transformation evidence when reapplying migrated bytes.
        if not mappings:
            mappings = previous.get("mappings", [])
        allowed_operations = {op["url"] for op in active.operations}
        mappings = [item for item in mappings if not topic_policy.excludes(item["legacy_url"])]
        for item in mappings:
            item["related_operations"] = [
                target for target in item["related_operations"] if target in allowed_operations
            ]
        related = sorted({target for item in mappings for target in item["related_operations"]})
        if related:
            generated = (
                SECTION
                + "\n## Related API reference\n\n"
                + "\n".join(
                    f"- [{next(op['summary'] for op in active.operations if op['url'] == target)}]({target})"
                    for target in related
                )
                + "\n"
            )
            if SECTION in migrated:
                migrated = migrated.split(SECTION, 1)[0].rstrip() + "\n\n" + generated
            else:
                migrated = migrated.rstrip() + "\n\n" + generated
        stages.append(
            {
                "document": relative,
                "topic": "api-reference",
                "input_sha256": _digest(body.encode()),
                "output_sha256": _digest(migrated.encode()),
            }
        )
        metadata.description = _description(migrated)
        metadata.related_documents = [
            item for item in metadata.related_documents if not active.excludes(item.canonical_url)
        ]
        document = serialize_document(metadata, migrated)
        for match in re.finditer(r"!\[([^\]]*)\]\(([^)]+)\)|<img\b[^>]*>", body):
            images.append(
                {"document": relative, "reference": match.group(), "review": "pixels_not_examined"}
            )
        if mappings:
            findings[relative] = {
                "document": relative,
                "url": url,
                "mappings": mappings,
                "related_operations": related,
            }
        retained.append((path, migrated))
        if apply and path.read_text(encoding="utf-8") != document:
            path.write_text(document, encoding="utf-8", newline="\n")
        if apply and store:
            with store.connection:
                store.connection.execute(
                    "UPDATE pages SET content_hash=? WHERE canonical_url=?",
                    (metadata.content_hash, url),
                )
    referenced: set[Path] = set()
    for path, body in retained:
        for match in re.finditer(r"assets/[a-zA-Z0-9_.-]+", body):
            referenced.add((path.parent / match.group()).resolve())
    for path in sorted(output.glob("content/*/**/assets/*")):
        if path.resolve() not in referenced:
            removed_assets.add(path.relative_to(output).as_posix())
            if apply:
                path.unlink()
    existing = {path.relative_to(output).as_posix() for path, _ in retained}
    affected = [findings[key] for key in sorted(findings) if key in existing]
    evidence = {
        "schema_version": 1,
        "policy_version": active.raw["version"],
        "input_digests": {
            "policy_sha256": active.policy_digest,
            "catalog_sha256": active.catalog_digest,
            "specification_sha256": active.catalog.get("specification", {}).get("sha256"),
            "route_inventory_sha256": active.catalog.get("route_inventory", {}).get("sha256"),
        },
        "counts": {
            "excluded_urls": len(excluded),
            "excluded_documents": sum(bool(item["document"]) for item in excluded.values()),
            "affected_documents": len(affected),
            "mappings": sum(len(item["mappings"]) for item in affected),
            "fallbacks": sum(
                mapping["destination"] == active.fallback
                for item in affected
                for mapping in item["mappings"]
            ),
            "removed_assets": len(removed_assets),
            "image_references": len(images),
        },
        "exclusions": [excluded[key] for key in sorted(excluded)],
        "affected_documents": affected,
        "removed_assets": sorted(removed_assets),
        "image_inventory": images,
        "stages": stages,
    }
    if apply:
        audit = {
            "schema_version": 1,
            "policy_sha256": topic_policy.sha256,
            "api_policy_sha256": active.policy_digest,
            "input_sha256": before,
            "output_sha256": corpus_digest(output),
            "content_lineage_sha256": _content_lineage_digest(output),
            "topics": topics,
            "content_migration": evidence,
        }
        audit_path.write_bytes(json_bytes(audit))
        # Remove historical public migration evidence before rebuilding reports.
        for name in ("quality-report.json", "quality-report.md"):
            report_path = output / name
            if report_path.is_file():
                report_path.unlink()
    else:
        evidence["topic_inventory"] = topics
    return evidence


def validate_content_policy(output: Path, policy: ContentPolicy | None = None) -> None:
    active = policy or load_content_policy()
    validate_curation(output)
    for path in sorted(output.glob("content/*/**/index.md")):
        metadata, body = split_document(path.read_text())
        if active.excludes(str(metadata["url"])) or active.excludes(
            str(metadata.get("canonical_url", ""))
        ):
            raise ValueError(f"excluded body remains: {path}")
        _, mappings = rewrite_text(body, str(metadata["url"]), active)
        if mappings or any(family in body for family in FAMILIES):
            raise ValueError(f"legacy textual references remain: {path}")
