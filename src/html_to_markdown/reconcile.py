"""Previous-release reconciliation and last-known-good restoration."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from .models import DiscoveredPage, PageStatus
from .state import StateStore
from .urls import infer_source, validate_source_url


def _load(path: Path | None) -> dict[str, object]:
    if path is None or not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("previous manifest must be a JSON object")
    return value


def _documents(manifest: dict[str, object]) -> list[dict[str, object]]:
    value = manifest.get("documents", [])
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def include_previous_urls(
    store: StateStore, previous_manifest: Path | None, source_ids: set[str]
) -> None:
    existing = {str(row["canonical_url"]): PageStatus(row["status"]) for row in store.rows()}
    for document in _documents(_load(previous_manifest)):
        source = document.get("sourceId")
        url = document.get("url")
        if not isinstance(source, str) or not isinstance(url, str) or source not in source_ids:
            continue
        store.discover([DiscoveredPage(source_id=source, url=url)])
        provenance = document.get("provenance", {})
        if isinstance(provenance, dict) and existing.get(url) in {None, PageStatus.DISCOVERED}:
            store.seed_provenance(url, provenance)


def include_inventory_urls(store: StateStore, inventory: Path | None, source_ids: set[str]) -> None:
    if inventory is None:
        return
    value = _load(inventory)
    urls = value.get("urls", [])
    if not isinstance(urls, list) or not all(isinstance(url, str) for url in urls):
        raise ValueError("inventory urls must be a list of strings")
    pages: list[DiscoveredPage] = []
    for url in urls:
        source = infer_source(url)
        if source is not None:
            pages.append(DiscoveredPage(source_id=source, url=validate_source_url(source, url)))
    store.discover(pages)


def _safe_path(root: Path, relative: object) -> Path:
    if not isinstance(relative, str):
        raise ValueError("previous document path must be a string")
    candidate = (root / relative).resolve()
    if root.resolve() not in candidate.parents:
        raise ValueError("previous document path escapes snapshot")
    return candidate


def _restore(output: Path, previous_manifest: Path, document: dict[str, object]) -> None:
    source = _safe_path(previous_manifest.parent, document.get("path"))
    if not source.is_file():
        raise FileNotFoundError(f"previous document is unavailable: {source}")
    destination = _safe_path(output, document.get("path"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    source_assets = source.parent / "assets"
    if source_assets.is_dir():
        shutil.copytree(source_assets, destination.parent / "assets", dirs_exist_ok=True)


def reconcile_previous(
    output: Path,
    store: StateStore,
    previous_manifest: Path | None,
    *,
    retain_previous: bool = False,
) -> None:
    prior_manifest = _load(previous_manifest)
    prior_documents = {
        str(item["url"]): item
        for item in _documents(prior_manifest)
        if isinstance(item.get("url"), str)
    }
    raw_terminal = {PageStatus.REMOVED_NOT_FOUND, PageStatus.REMOVED_NAVIGATION}
    reconciled_statuses = {
        PageStatus.CARRIED_FORWARD,
        PageStatus.REMOVAL_CANDIDATE,
        PageStatus.CONFIRMED_REMOVAL,
        PageStatus.UNAVAILABLE,
    }
    for row in store.rows():
        status = PageStatus(row["status"])
        if status == PageStatus.SUCCESS or status in reconciled_statuses:
            continue
        url = str(row["canonical_url"])
        prior = prior_documents.get(url)
        failure = str(status)
        if prior is None:
            store.reconcile(
                url,
                PageStatus.UNAVAILABLE,
                output_path=None,
                digest=None,
                freshness="unavailable",
                failure_classification=failure,
                last_success_at=None,
                consecutive_failures=1,
                terminal_confirmations=1 if status in raw_terminal else 0,
            )
            continue
        provenance = prior.get("provenance", {})
        provenance = provenance if isinstance(provenance, dict) else {}
        last_success = provenance.get("last_success_at") or prior_manifest.get("ended_at")
        terminal = int(provenance.get("terminal_confirmation_count", 0))
        consecutive = int(provenance.get("consecutive_failure_count", 0))
        if status in raw_terminal:
            terminal += 1
            reconciled = (
                PageStatus.CONFIRMED_REMOVAL
                if terminal >= 2 and not retain_previous
                else PageStatus.REMOVAL_CANDIDATE
            )
            if reconciled == PageStatus.CONFIRMED_REMOVAL:
                store.reconcile(
                    url,
                    reconciled,
                    output_path=None,
                    digest=None,
                    freshness="confirmed_removal",
                    failure_classification=failure,
                    last_success_at=str(last_success or "") or None,
                    consecutive_failures=consecutive + 1,
                    terminal_confirmations=terminal,
                )
                continue
            freshness = "removal_candidate"
        else:
            terminal = 0
            reconciled = PageStatus.CARRIED_FORWARD
            freshness = "carried_forward"
        if previous_manifest is None:
            raise ValueError("previous manifest required to restore content")
        _restore(output, previous_manifest, prior)
        store.reconcile(
            url,
            reconciled,
            output_path=str(prior.get("path")),
            digest=str(prior.get("sha256", "")),
            freshness=freshness,
            failure_classification=failure,
            last_success_at=str(last_success or "") or None,
            consecutive_failures=consecutive + 1,
            terminal_confirmations=terminal,
        )
