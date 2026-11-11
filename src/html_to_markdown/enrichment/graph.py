"""Canonical alias and retained reference checks for final corpus publication."""

import re
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

from .analysis import links


def validate_source_pins(manifest: dict[str, Any]) -> None:
    refresh = manifest.get("refresh")
    if isinstance(refresh, dict) and refresh.get("schema_version") == 2:
        selected, inputs = refresh.get("refreshed_sources"), refresh.get("source_inputs")
        if (
            not isinstance(selected, list)
            or not isinstance(inputs, dict)
            or not selected
            or any(not isinstance(source, str) for source in selected)
        ):
            raise ValueError("invalid refreshed source inventory")
        if (
            len(selected) != len(set(selected))
            or set(selected) != set(inputs)
            or set(selected) - set(manifest["source_roots"])
        ):
            raise ValueError("invalid refreshed source inventory")
        for source, entry in inputs.items():
            if not isinstance(entry, dict) or any(
                not re.fullmatch(r"[0-9a-f]{64}", str(entry.get(field, "")))
                for field in ("admission_sha256", "content_sha256", "inventory_sha256")
            ):
                raise ValueError(f"invalid source admission pin: {source}")


def validate_aliases(manifest: dict[str, Any]) -> None:
    for name in ("refresh", "enrichment"):
        if name in manifest:
            evidence = manifest[name]
            if not isinstance(evidence, dict) or not re.fullmatch(
                r"[0-9a-f]{64}", str(evidence.get("artifact_sha256", ""))
            ):
                raise ValueError(f"invalid {name} artifact pin")
    validate_source_pins(manifest)
    enrichment = manifest.get("refresh") if "aliases" in manifest else manifest.get("enrichment")
    if enrichment is None:
        return
    if not isinstance(enrichment, dict) or not re.fullmatch(
        r"[0-9a-f]{64}", str(enrichment.get("artifact_sha256", ""))
    ):
        raise ValueError("invalid enrichment artifact pin")
    aliases = manifest.get("aliases") if "aliases" in manifest else enrichment.get("aliases")
    if not isinstance(aliases, list):
        raise ValueError("invalid enrichment aliases")
    documents = {d["path"] for d in manifest["documents"]}
    seen = set()
    for alias in aliases:
        if not isinstance(alias, dict) or set(alias) != {"path", "target", "url"}:
            raise ValueError("invalid canonical alias entry")
        source, target = alias["path"], alias["target"]
        path = PurePosixPath(source)
        unsafe_path = (
            not isinstance(source, str)
            or "\\" in source
            or str(path) != source
            or path.is_absolute()
            or any(p in {"", ".", ".."} for p in source.split("/"))
            or not source.startswith("content/")
            or not source.endswith("/index.md")
            or len(path.parts) < 4
            or path.parts[1] not in manifest["source_roots"]
        )
        if unsafe_path:
            raise ValueError("unsafe alias path")
        if source in seen or source in documents or target not in documents or source == target:
            raise ValueError("canonical alias collision or missing target")
        if not isinstance(alias["url"], str) or urlsplit(alias["url"]).scheme != "https":
            raise ValueError("invalid canonical alias URL")
        seen.add(source)


def anchors(body: str) -> set[str]:
    seen: dict[str, int] = {}
    result = set(re.findall(r'\bid=["\']([^"\']+)["\']', body))
    for title in re.findall(r"(?m)^#{1,6}\s+(.+)$", body):
        slug = re.sub(r"[^\w\- ]", "", title.lower()).replace(" ", "-")
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        result.add(slug if count == 0 else f"{slug}-{count}")
    return result


def graph_fallbacks(analysis: dict[str, Any], results: list[dict[str, Any]]) -> None:
    """Retain a whole article when a new edit breaks a known incoming anchor."""
    original = {d["document"]: d for d in analysis["documents"]}
    urls = {str(d["metadata"]["url"]): d["document"] for d in analysis["documents"]}
    resolved = {r["document"]: r for r in results}
    for result in results:
        if result["disposition"] not in {"rewrite", "exclude_shell"}:
            continue
        source_doc = original[result["document"]]
        before = anchors(source_doc["body"])
        after = anchors(result["body"]) if result["disposition"] == "rewrite" else set()
        needed = set()
        for incoming in analysis["documents"]:
            incoming_links = incoming["links"] if "links" in incoming else links(incoming["body"])
            for link in incoming_links:
                try:
                    parsed = urlsplit(link)
                except ValueError:
                    # Malformed captured references do not authorize repairs.
                    if incoming["document"] == result["document"]:
                        needed.add("malformed_source_reference")
                    continue
                target = urls.get(parsed._replace(fragment="").geturl())
                if link.startswith("#") and incoming["document"] == result["document"]:
                    target = result["document"]
                if target == result["document"]:
                    if result["disposition"] == "exclude_shell":
                        needed.add("incoming_reference")
                    if parsed.fragment and unquote(parsed.fragment) in before:
                        needed.add(unquote(parsed.fragment))
        if needed - after:
            result.update(
                disposition="fallback",
                body=source_doc["body"],
                description=source_doc["metadata"].get("description"),
                canonical_document=None,
                reason="retained incoming reference or anchor required",
            )
    for result in results:
        if result["disposition"] == "alias" and resolved[result["canonical_document"]][
            "disposition"
        ] in {"exclude_shell", "alias"}:
            doc = original[result["document"]]
            result.update(
                disposition="fallback",
                body=doc["body"],
                description=doc["metadata"].get("description"),
                canonical_document=None,
                reason="canonical target unavailable",
            )
