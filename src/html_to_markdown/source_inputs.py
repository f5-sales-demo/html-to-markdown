"""Seal private source-admission inputs and verify their complete replay chain."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from .content_inventory import atomic_private
from .curation import corpus_digest, json_bytes
from .enrichment.analysis import sha
from .source_review_jobs import verify_reviews

MARKER = "source-review-receipt.json"


def seal_source(capture: Path, review: Path, output: Path) -> dict[str, Any]:
    verified = verify_reviews(capture, review)
    if output.exists():
        raise ValueError("source sealing requires a fresh directory")
    output.mkdir(parents=True, mode=0o700)
    root = output / ".admission"
    shutil.copytree(capture, root / "capture")
    shutil.copytree(
        review, root / "review", ignore=shutil.ignore_patterns("journal", "*.private.log")
    )
    if (review / "reviewed" / "content").exists():
        shutil.copytree(review / "reviewed" / "content", output / "content")
    inventory = json.loads((capture / "inventory.json").read_bytes())
    review_inventory = json.loads((review / "review-inventory.json").read_bytes())
    records = []
    for entry in review_inventory["documents"]:
        if "path" not in entry:
            records.append(entry)
            continue
        record = root / "review" / entry["path"]
        saved = json.loads(record.read_bytes())
        item = saved["item"]
        records.append(
            {
                "identity": item["identity"],
                "url": item["url"],
                "sourceId": item["sourceId"],
                "record": entry["path"],
                "record_sha256": sha(record.read_bytes()),
                "disposition": saved["disposition"],
                "reason": saved["reason"],
                "captured_at": item.get("captured_at"),
                "article_sha256": item["article_sha256"],
                "source_html_sha256": item["source_html_sha256"],
                "published_path": str(
                    Path(item.get("published_path", item["article_path"])).relative_to("articles")
                ),
            }
        )
    receipt = {
        "schema_version": 1,
        "source": inventory["binding"]["source"],
        "capture_binding": inventory["binding"],
        "inventory_sha256": sha((capture / "inventory.json").read_bytes()),
        "review_inventory_sha256": verified["inventory_sha256"],
        "documents": records,
        "accepted_files": verified["accepted_files"],
        "content_sha256": corpus_digest(output),
        "enumeration_complete": inventory["completeness"]["enumeration"],
        "capture_complete": inventory["completeness"]["capture"],
        "review_complete": all(
            entry.get("disposition") in {"accepted", "policy_excluded"} for entry in records
        ),
        "ended_at": inventory["ended_at"],
    }
    atomic_private(output / MARKER, json_bytes(receipt))
    verify_source(output, sha((output / MARKER).read_bytes()))
    return receipt


def verify_source(output: Path, digest: str) -> dict[str, Any]:
    if sha((output / MARKER).read_bytes()) != digest:
        raise ValueError("source admission receipt digest mismatch")
    receipt = json.loads((output / MARKER).read_bytes())
    root = output / ".admission"
    if sha((root / "capture" / "inventory.json").read_bytes()) != receipt["inventory_sha256"]:
        raise ValueError("source capture inventory changed")
    verified = verify_reviews(root / "capture", root / "review")
    if (
        verified["inventory_sha256"] != receipt["review_inventory_sha256"]
        or verified["accepted_files"] != receipt["accepted_files"]
    ):
        raise ValueError("source independent review inventory changed")
    for entry in receipt["documents"]:
        if "record" not in entry:
            continue
        path = root / "review" / entry["record"]
        if sha(path.read_bytes()) != entry["record_sha256"]:
            raise ValueError("source admission decision changed")
        record = json.loads(path.read_bytes())
        if (
            record["disposition"] != entry["disposition"]
            or record["item"]["identity"] != entry["identity"]
        ):
            raise ValueError("source admission disposition changed")
    actual = {
        path.relative_to(output).as_posix(): sha(path.read_bytes())
        for path in (output / "content").rglob("*")
        if path.is_file()
    }
    if actual != verified["accepted_files"] or corpus_digest(output) != receipt["content_sha256"]:
        raise ValueError("source admitted content or media changed")
    return dict(receipt)
