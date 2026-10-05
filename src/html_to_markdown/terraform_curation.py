"""Offline, structural removal of the retired Volterra Terraform provider.

This filter is intentionally independent of a page's original digest. A changed
page goes through the same analysis as a first capture or a carried-forward page.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup

from .render import normalize_body

PROVIDER = re.compile(
    r"volterraedge/volterra|terraform-provider-volterra|provider\s+[\"']volterra[\"']"
    r"|registry\.terraform\.io/providers/volterraedge/volterra"
    r"|\bvolterra\s+(?:terraform\s+)?(?:provider|terraform\s+module)\b",
    re.I,
)
RESOURCE = re.compile(r"\b(?:resource|data)\s+[\"'](volterra_[\w]+)[\"']", re.I)
REFERENCE = re.compile(r"\bvolterra_[a-z][\w]*(?:\.[\w-]+)?\b", re.I)
HCL_REFERENCE = re.compile(r"\bvolterra_[a-z][\w]*\.[\w-]+", re.I)
TERRAFORM_COMMAND = re.compile(r"\bterraform\s+(?:init|apply|plan|destroy|test|validate)\b", re.I)
PROCEDURE = re.compile(
    r"\b(?:create|set|run|execute|apply|plan|init|install|configure|"
    r"prerequisite|pre-requirement|step|then|example|variable|module|"
    r"must|should|need to|following|how to use|code)\b",
    re.I,
)
INDEPENDENT = re.compile(
    r"\b(?:aws|azure|gcp|google|kubernetes|helm|big-ip|nginx|vpc|subnet|"
    r"route table|nat gateway|igw)\b",
    re.I,
)
SECTION_TITLE = re.compile(
    r"\b(?:terraform|provider|module|automation|configuration|setup)\b", re.I
)
LINK = re.compile(r"https?://[^\s<>\"'`\[\]()]+", re.I)
FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
HCL_DECLARATION = re.compile(
    r"^\s*(resource|data|provider|module|variable)\s+[\"\']([^\"\']+)[\"\']"
)


def _normal(text: str) -> str:
    import html  # local import keeps this module's public surface small

    return re.sub(r"\\([_\-*\[\]()])", r"\1", html.unescape(unquote(text))).casefold()


def _visible_terraform(body: str) -> bool:
    # Repository names in link destinations are not reader-facing Terraform
    # guidance. Only visible prose/code can justify a replacement link.
    return bool(re.search(r"\bterraform\b", LINK.sub("", _normal(body))))


def _tidy_headings(body: str) -> str:
    result: list[str] = []
    fenced = False
    for line in body.splitlines(keepends=True):
        if FENCE.match(line):
            fenced = not fenced
        if not fenced and re.match(r"^#{1,6}[ \t]+\S", line) and result and result[-1].strip():
            result.append("\n")
        result.append(line)
    return "".join(result)


def _braced_end(lines: list[str], start: int) -> int | None:
    """Find a complete HCL block, ignoring braces in quoted strings/comments."""
    depth = 0
    opened = False
    quoted = False
    escaped = False

    def interpolation_end(line: str, position: int) -> int:
        nested = 1
        inner_quote = False
        escape = False
        while position < len(line):
            char = line[position]
            if escape:
                escape = False
            elif inner_quote and char == "\\":
                escape = True
            elif char == '"':
                inner_quote = not inner_quote
            elif not inner_quote and char == "{":
                nested += 1
            elif not inner_quote and char == "}":
                nested -= 1
                if nested == 0:
                    return position + 1
            position += 1
        return position

    for index in range(start, len(lines)):
        line = lines[index]
        pos = 0
        while pos < len(line):
            char = line[pos]
            if escaped:
                escaped = False
            elif quoted and char == "\\":
                escaped = True
            elif quoted and line[pos : pos + 2] == "${":
                pos = interpolation_end(line, pos + 2)
                continue
            elif char == '"':
                quoted = not quoted
            elif not quoted and (char == "#" or line[pos : pos + 2] == "//"):
                break
            elif not quoted and char == "{":
                depth += 1
                opened = True
            elif not quoted and char == "}":
                depth -= 1
                if depth < 0:
                    return None
                if opened and depth == 0:
                    return index + 1
            pos += 1
    return None


def _strip_hcl(
    code: str, retired_links: tuple[str, ...], aliases: set[str]
) -> tuple[str, list[str]]:
    """Retain independent declarations in a mixed HCL fence when boundaries parse."""
    lines = code.splitlines(keepends=True)
    removed: list[str] = []
    spans: list[tuple[int, int]] = []
    index = 0
    while index < len(lines):
        match = HCL_DECLARATION.match(lines[index])
        entry_match = re.match(r"^\s*([\w-]+)\s*=\s*\{", lines[index], re.I)
        provider_entry = bool(entry_match and entry_match.group(1).casefold() in aliases)
        obsolete_module = bool(
            match
            and match.group(1) == "module"
            and any(link in _normal("".join(lines[index:])) for link in retired_links)
        )
        obsolete = bool(
            provider_entry
            or match
            and (
                match.group(1) in {"resource", "data"}
                and match.group(2).casefold().startswith("volterra_")
                or match.group(1) == "provider"
                and match.group(2).casefold() in aliases
                or obsolete_module
            )
        )
        if obsolete:
            end = _braced_end(lines, index)
            if end is None:
                return "", ["unparseable_hcl_with_retired_provider"]
            snippet = "".join(lines[index:end])
            removed.extend(RESOURCE.findall(snippet))
            if provider_entry or (match and match.group(1) == "provider"):
                removed.append("provider")
            spans.append((index, end))
            index = end
        else:
            index += 1
    if not spans:
        return "", ["retired_provider_in_unseparable_code"]
    dropped = {i for start, end in spans for i in range(start, end)}
    remaining = "".join(line for i, line in enumerate(lines) if i not in dropped)
    # An otherwise empty required_providers/terraform shell has no instructional value.
    remaining = re.sub(
        r"(?ms)^\s*terraform\s*\{\s*required_providers\s*\{\s*\}\s*\}\s*",
        "",
        remaining,
    )
    if not re.search(r"\b(?:resource|data|provider|module)\s+[\"\'](?!volterra)", remaining, re.I):
        return "", removed
    if PROVIDER.search(_normal(remaining)) or RESOURCE.search(_normal(remaining)):
        return "", ["retired_provider_in_unseparable_code"]
    return remaining.strip() + "\n" if remaining.strip() else "", removed


@dataclass(frozen=True)
class TerraformResult:
    body: str
    findings: list[dict[str, Any]]
    destinations: list[str]
    removed_media: list[str]
    omit: bool = False


class TerraformFilter:
    """Use only digest-bound local policy and a Registry 13.1.1 catalog."""

    def __init__(self, topic: dict[str, Any], root: Path) -> None:
        self.topic = topic
        path = root / topic["catalog"]["path"]
        import hashlib

        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != topic["catalog"]["sha256"]:
            raise ValueError("Terraform destination catalog digest mismatch")
        self.catalog = json.loads(payload)
        if self.catalog.get("release_tag") != "v13.1.1":
            raise ValueError("Terraform destination catalog release mismatch")
        registry_path = root / topic["registry_evidence"]["path"]
        import gzip

        registry_payload = gzip.decompress(registry_path.read_bytes())
        if hashlib.sha256(registry_payload).hexdigest() != self.catalog["registry_response_sha256"]:
            raise ValueError("Terraform Registry response digest mismatch")
        registry = json.loads(registry_payload)
        if (
            registry.get("version") != "13.1.1"
            or registry.get("tag") != "v13.1.1"
            or registry.get("id") != "f5-sales-demo/xcsh/13.1.1"
        ):
            raise ValueError("Terraform Registry evidence version mismatch")
        listed = {(item["path"], str(item["id"])) for item in registry["docs"]}
        for item in self.catalog["documents"]:
            if (item["path"], item["registry_id"]) not in listed:
                raise ValueError("Terraform destination absent from pinned Registry")
            if (
                item["resource"] != "xcsh_" + item["slug"]
                or item["url"] != self.catalog["landing"] + "/docs/resources/" + item["slug"]
                or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
            ):
                raise ValueError("unqualified Terraform resource mapping")
        self.destinations = {item["slug"]: item["url"] for item in self.catalog["documents"]}
        self.mappings = self.catalog["mappings"]
        if any(
            not key.startswith("volterra_") or value not in self.destinations
            for key, value in self.mappings.items()
        ):
            raise ValueError("invalid Terraform resource mapping")
        self.retired_links = tuple(_normal(value) for value in topic["retired_documentation_urls"])
        self.reviewed_urls = set(topic.get("baseline_reviewed_urls", []))
        linked_path = root / topic["linked_examples_catalog"]["path"]
        linked_payload = linked_path.read_bytes()
        if hashlib.sha256(linked_payload).hexdigest() != topic["linked_examples_catalog"]["sha256"]:
            raise ValueError("linked Terraform examples catalog digest mismatch")
        linked = json.loads(linked_payload)
        if linked.get("schema_version") != 1:
            raise ValueError("unsupported linked Terraform examples catalog")
        self.linked_examples = linked["entries"]
        self.qualified_links = {
            _normal(item["replacement_url"])
            for item in self.linked_examples
            if item.get("replacement_url")
        }
        for item in self.linked_examples:
            if item["disposition"] not in {"keep", "remove", "replace"}:
                raise ValueError("invalid linked Terraform example disposition")
            if item["disposition"] in {"keep", "replace"} and (
                not item.get("commit")
                or not item.get("evidence_files")
                or not item.get("replacement_url")
            ):
                raise ValueError("unqualified retained linked Terraform example")
            if item.get("commit") and not re.fullmatch(r"[0-9a-f]{40}", item["commit"]):
                raise ValueError("invalid linked Terraform example commit")
            if item.get("replacement_url") and item["commit"] not in item["replacement_url"]:
                raise ValueError("linked Terraform example is not commit-pinned")
            if any(
                not re.fullmatch(r"[0-9a-f]{64}", evidence["sha256"])
                for evidence in item["evidence_files"]
            ):
                raise ValueError("invalid linked Terraform example file digest")

    def _retired_link(self, text: str, base_url: str = "") -> bool:
        value = _normal(text)
        if any(link in value for link in self.retired_links):
            return True
        for target in re.findall(r"(?:\]\(|\]:\s*|href\s*=\s*[\"'])\s*<?([^\s)>\"']+)", text, re.I):
            resolved = _normal(urljoin(base_url, target))
            if any(resolved.startswith(link) for link in self.retired_links):
                return True
        return False

    def _linked_decision(self, url: str) -> dict[str, Any] | None:
        value = _normal(url.rstrip(".,;"))
        if value in self.qualified_links:
            return None
        parsed = urlsplit(value)
        if parsed.hostname not in {"github.com", "github.com.mcas.ms"}:
            return None
        matches = [
            item
            for item in self.linked_examples
            if parsed.path == item["path_prefix"].casefold()
            or parsed.path.startswith(item["path_prefix"].casefold() + "/")
        ]
        return max(matches, key=lambda item: len(item["path_prefix"])) if matches else None

    def _obsolete_linked_example(self, text: str) -> bool:
        return any(
            (item := self._linked_decision(url)) is not None and item["disposition"] == "remove"
            for url in LINK.findall(text)
        )

    def _pin_linked_examples(self, body: str) -> tuple[str, list[dict[str, Any]]]:
        findings = []
        for url in sorted(set(LINK.findall(body)), key=lambda value: (-len(value), value)):
            item = self._linked_decision(url)
            if item is None or item["disposition"] == "remove":
                continue
            destination = item["replacement_url"]
            if url.rstrip(".,;") != destination:
                body = body.replace(url.rstrip(".,;"), destination)
                findings.append(
                    {
                        "reason": "pinned_linked_example"
                        if item["disposition"] == "keep"
                        else "replaced_linked_example",
                        "source": url.rstrip(".,;"),
                        "destination": destination,
                        "evidence": item["evidence_files"],
                    }
                )
        return body, findings

    def _obsolete(self, text: str, base_url: str = "") -> bool:
        value = _normal(text)
        resource_context = any(
            REFERENCE.search(line) and re.search(r"\b(?:terraform|resource|provider)\b", line)
            for line in value.splitlines()
        )
        return bool(
            PROVIDER.search(value)
            or RESOURCE.search(value)
            or resource_context
            or self._retired_link(text, base_url)
            or self._obsolete_linked_example(value)
        )

    def _profile(self, body: str, url: str) -> tuple[str, list[dict[str, Any]], list[str]]:
        """Apply a digest-guarded manual review to known complex source layouts."""
        from .curation import blocks, digest

        profile = self.topic.get("profiles", {}).get(url)
        if profile is None:
            return body, [], []
        if digest(body.encode()) != profile["input_sha256"] and not self._obsolete(body, url):
            return body, [], []
        lines = body.splitlines(keepends=True)
        headings = [block for block in blocks(body) if block.kind == "heading"]
        removed: list[str] = []
        findings: list[dict[str, Any]] = []

        def section(heading: str) -> tuple[int, int]:
            matches = [block for block in headings if block.text.strip() == heading]
            if len(matches) != 1:
                raise ValueError("stale Terraform section profile")
            start = matches[0].start
            level = len(heading) - len(heading.lstrip("#"))
            end = next(
                (
                    block.start
                    for block in headings
                    if block.start > start
                    and len(block.text) - len(block.text.lstrip("#")) <= level
                ),
                len(lines),
            )
            return start, end

        spans: list[tuple[int, int, str]] = []
        if heading := profile.get("remove_before_heading"):
            spans.append((0, section(heading)[0], ""))
        if heading := profile.get("remove_from_heading"):
            spans.append((section(heading)[0], len(lines), ""))
        for heading in profile.get("remove_sections", []):
            start, end = section(heading)
            spans.append((start, end, ""))
        for heading, replacement in profile.get("replace_sections", {}).items():
            start, end = section(heading)
            spans.append((start, end, replacement))
        if any(a < d and c < b for i, (a, b, _) in enumerate(spans) for c, d, _ in spans[i + 1 :]):
            raise ValueError("overlapping Terraform section profile")
        for start, end, replacement in sorted(spans, reverse=True):
            removed.append("".join(lines[start:end]))
            findings.append({"reason": "reviewed_dependent_section", "lines": [start, end]})
            lines[start:end] = [replacement]
        body = "".join(lines)
        for pattern in profile.get("remove_text_patterns", []):
            body, count = re.subn(pattern, "", body)
            if count != 1:
                raise ValueError("stale Terraform text profile")
            findings.append({"reason": "reviewed_dependent_text", "pattern": pattern})
        for old, new in profile.get("replace_headings", {}).items():
            if body.count(old) != 1:
                raise ValueError("stale Terraform heading profile")
            body = body.replace(old, new)
            findings.append({"reason": "reviewed_heading", "source": old, "destination": new})
        for old, new in profile.get("replace_text", {}).items():
            if old not in body:
                raise ValueError("stale Terraform wording profile")
            body = body.replace(old, new)
            findings.append({"reason": "reviewed_wording", "source": old, "destination": new})
        return body, findings, removed

    def transform(self, body: str, url: str) -> TerraformResult:
        # Late import avoids a curation/terraform_curation import cycle.
        from .curation import blocks, digest, remove_blocks

        original = body
        try:
            body, profile_findings, profile_removed = self._profile(body, url)
        except ValueError:
            profile = self.topic.get("profiles", {}).get(url)
            if profile is not None and digest(body.encode()) == profile["input_sha256"]:
                raise
            # A changed source may have replaced an exact reviewed span. The
            # reusable identity and dependency rules still analyze it below.
            body, profile_findings, profile_removed = original, [], []
        body, link_findings = self._pin_linked_examples(body)
        if (
            not profile_findings
            and not link_findings
            and not self._obsolete(body, url)
            and not (HCL_REFERENCE.search(_normal(body)) and "terraform" in _normal(body))
        ):
            return TerraformResult(body, [], [], [])
        parsed = blocks(body)
        lines = body.splitlines(keepends=True)
        changed: dict[tuple[int, int], str] = {}
        findings: list[dict[str, Any]] = [*profile_findings, *link_findings]
        removed_resources: set[str] = set(RESOURCE.findall("\n".join(profile_removed)))
        removed_resources.update(
            name for name in REFERENCE.findall("\n".join(profile_removed)) if name in self.mappings
        )
        removed_media: list[str] = re.findall(r"!\[[^\]]*\]\(([^)]+)", "\n".join(profile_removed))
        removed_labels = {
            match.group(1).casefold()
            for block in parsed
            if block.kind == "reference" and self._obsolete(block.text, url)
            for match in [re.match(r"^\s*\[([^\]]+)\]:", block.text)]
            if match
        }
        obsolete_sections = {
            block.location.split(":", 1)[0]
            for block in parsed
            if block.kind not in {"section", "heading"} and self._obsolete(block.text, url)
        }
        doc_has_provider = bool(
            PROVIDER.search(_normal(body))
            or RESOURCE.search(_normal(body))
            or "terraform" in _normal(body)
            and HCL_REFERENCE.search(_normal(body))
        )
        aliases = {"volterra"}
        aliases.update(
            match.group(1).casefold()
            for match in re.finditer(
                r"(?ms)^\s*([a-z][\w-]*)\s*=\s*\{[^}]*?\bsource\s*=\s*[\"']volterraedge/volterra[\"']",
                _normal(original),
            )
        )
        legacy_vars = {
            name
            for block in parsed
            if block.kind in {"fence", "code_block"} and self._obsolete(block.text, url)
            for name in re.findall(r"\bvar\.([a-z][\w]*)", _normal(block.text))
        }
        atomic = [
            block
            for block in parsed
            if block.kind
            in {"fence", "code_block", "html_block", "paragraph", "table_row", "reference"}
        ]
        for block in atomic:
            text = block.text
            value = _normal(text)
            obsolete = self._obsolete(text, url) or bool(
                doc_has_provider
                and block.kind in {"fence", "code_block"}
                and HCL_REFERENCE.search(value)
                and "terraform" in _normal(body)
            )
            if block.kind in {"fence", "code_block"} and any(
                re.search(r"\bprovider\s+[\"\']" + re.escape(alias) + r"[\"\']", value)
                for alias in aliases
            ):
                obsolete = True
            if removed_labels and any(
                re.search(r"\[" + re.escape(label) + r"\]", value, re.I) for label in removed_labels
            ):
                obsolete = True
            section = block.location.split(":", 1)[0]
            dependent = bool(
                section in obsolete_sections
                and doc_has_provider
                and PROCEDURE.search(value)
                and not INDEPENDENT.search(value)
            )
            if (
                doc_has_provider
                and TERRAFORM_COMMAND.search(value)
                and not INDEPENDENT.search(value)
            ):
                dependent = True
            if (
                doc_has_provider
                and "terraform" in value
                and re.search(
                    r"\b(?:resource creation|works as expected|third-party module)\b", value
                )
                and not INDEPENDENT.search(value)
            ):
                dependent = True
            # Fence labels are unreliable, so inspect the body in every code block.
            code = block.kind in {"fence", "code_block"}
            if code and not obsolete and doc_has_provider and section in obsolete_sections:
                variables = set(re.findall(r"\bvariable\s+[\"\']([^\"\']+)", value))
                dependent = bool(
                    TERRAFORM_COMMAND.search(value)
                    or variables
                    and variables <= legacy_vars
                    and not INDEPENDENT.search(value)
                )
            if not obsolete and not dependent:
                continue
            replacement = ""
            reason = "retired_provider" if obsolete else "dependent_instruction"
            if code and obsolete:
                content_lines = text.splitlines(keepends=True)
                enclosed = len(content_lines) >= 2 and bool(FENCE.match(content_lines[0]))
                inner = "".join(content_lines[1:-1]) if enclosed else text
                stripped, resources = _strip_hcl(inner, self.retired_links, aliases)
                removed_resources.update(resources)
                removed_resources.update(RESOURCE.findall(value))
                if stripped and not self._obsolete(stripped):
                    replacement = (
                        content_lines[0] + stripped + content_lines[-1] if enclosed else stripped
                    )
                    reason = "mixed_hcl_retained_independent_declarations"
            elif block.kind == "html_block" and obsolete:
                soup = BeautifulSoup(text, "html.parser")
                for node in list(soup.find_all(["p", "li", "a", "code", "pre"])):
                    if node.parent is not None and self._obsolete(str(node), url):
                        node.decompose()
                candidate = str(soup).strip()
                if candidate and not self._obsolete(candidate, url):
                    replacement = candidate + "\n"
                    reason = "mixed_html_retained_independent_nodes"
            else:
                removed_resources.update(RESOURCE.findall(value))
                if block.kind == "paragraph" and "![" in text:
                    removed_media.extend(re.findall(r"!\[[^\]]*\]\(([^)]+)", text))
            # A paragraph inside a list must include its marker and children.
            start, end = block.start, block.end
            if block.kind == "paragraph" and not replacement:
                parents = [
                    item
                    for item in parsed
                    if item.kind == "list_item" and item.start <= start and end <= item.end
                ]
                if parents:
                    dependent_parents = [
                        item
                        for item in parents
                        if self._obsolete(item.text, url)
                        and re.search(
                            r"\b(?:setup|prerequisite|step|configure|run|example)\b",
                            item.text,
                            re.I,
                        )
                        and not INDEPENDENT.search(item.text)
                    ]
                    parent = (
                        max(dependent_parents, key=lambda item: item.end - item.start)
                        if dependent_parents
                        else min(parents, key=lambda item: item.end - item.start)
                    )
                    start, end = parent.start, parent.end
            if any(a < end and start < b for a, b in changed):
                continue
            changed[(start, end)] = replacement
            findings.append(
                {
                    "location": block.location,
                    "input_sha256": digest(text.encode()),
                    "reason": reason,
                }
            )
        if not changed and not profile_findings and not link_findings:
            return TerraformResult(body, [], [], [])
        rewritten = "".join(
            changed.get((i, end), line)
            for i, line in enumerate(lines)
            for end in ([next((b for a, b in changed if a == i), i + 1)])
            if not any(a < i < b for a, b in changed)
        )
        rewritten = remove_blocks(rewritten, [])
        rewritten = re.sub(r"(?m)(?:\n---\n)+\s*$", "\n", rewritten)
        # A short fragment containing only the purpose of a removed procedure is
        # not a standalone explanation.
        prose = re.sub(r"(?ms)```.*?```|~~~.*?~~~|<[^>]+>|\[[^\]]+\]\([^)]+\)", " ", rewritten)
        prose = re.sub(r"(?m)^\s*#+[^\n]*$|^\s*[-*]\s*$", " ", prose)
        words = re.findall(r"\b[\w'-]+\b", prose)
        if len(words) < self.topic.get("minimum_independent_words", 25) or url in self.topic.get(
            "omit_urls", []
        ):
            return TerraformResult(normalize_body(rewritten), findings, [], removed_media, True)
        resources = [
            self.mappings[name] for name in sorted(removed_resources) if name in self.mappings
        ]
        # Existing subject exclusions override a published xcsh resource.
        forbidden = set(self.topic.get("excluded_resource_slugs", []))
        resources = [name for name in resources if name not in forbidden]
        destinations = [self.destinations[name] for name in dict.fromkeys(resources)]
        if not _visible_terraform(rewritten):
            destinations = []
        needs_replacement = bool(
            profile_findings
            or changed
            or any(item["reason"] == "replaced_linked_example" for item in link_findings)
        )
        if not destinations and needs_replacement and _visible_terraform(rewritten):
            destinations = [self.catalog["landing"]]
        additions = []
        for destination in destinations:
            if destination in rewritten:
                continue
            if destination == self.catalog["landing"]:
                additions.append(
                    f"For Terraform configuration, see the [xcsh provider documentation]({destination})."
                )
            else:
                name = next(
                    slug for slug, target in self.destinations.items() if target == destination
                )
                label = name.replace("_", " ")
                additions.append(
                    f"For Terraform configuration, see the [xcsh {label} documentation]({destination})."
                )
        if additions:
            rewritten = rewritten.rstrip() + "\n\n" + "\n\n".join(additions) + "\n"
        if rewritten == original:
            return TerraformResult(original, [], [], [])
        return TerraformResult(
            normalize_body(_tidy_headings(rewritten)), findings, destinations, removed_media
        )
