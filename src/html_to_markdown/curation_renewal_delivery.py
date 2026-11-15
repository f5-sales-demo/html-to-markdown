"""Ubuntu catalog delivery with resumable linked issues and pull requests."""

from __future__ import annotations

import fcntl
import json
import platform
import re
import subprocess  # nosec B404
from pathlib import Path
from typing import Any

from .content_inventory import atomic_private
from .curation import json_bytes, load_curation_policy
from .curation_renewal_jobs import _save
from .enrichment.analysis import sha
from .publication_queue import deployed_revision


def _run(root: Path, args: list[str]) -> bytes:
    return subprocess.run(args, cwd=root, capture_output=True, check=True).stdout  # nosec B603


def _text(root: Path, args: list[str]) -> str:
    return _run(root, args).decode().strip()


def _body(root: Path, name: str, value: str) -> str:
    path = root / name
    atomic_private(path, value.encode())
    return str(path)


def _main_policy(root: Path) -> str:
    data = _run(root, ["git", "show", "origin/main:src/html_to_markdown/curation_policy.json"])
    linked = bytearray(data)
    for topic in json.loads(data)["topics"]:
        if catalog := topic.get("review_catalog"):
            linked.extend(
                _run(root, ["git", "show", "origin/main:src/html_to_markdown/" + catalog["path"]])
            )
    return sha(bytes(linked))


# Delivery checkpoints preserve issue/PR identity across transport interruption.
# pylint: disable-next=too-many-locals,too-many-statements,too-many-branches
def submit_policy(repository: Path, staged: Path, private_output: Path) -> dict[str, Any]:
    if platform.system() != "Linux":
        raise ValueError("GitHub catalog mutations are routed exclusively to Ubuntu")
    receipt = json.loads((staged / "curation-renewal-receipt.json").read_bytes())
    if not re.fullmatch(r"[0-9a-f]{64}", str(receipt.get("qualification_sha256", ""))):
        raise ValueError("unsafe catalog qualification identity")
    if not receipt.get("post_planner_validated") or receipt.get("enforcement_findings") != 0:
        raise ValueError("catalog submission requires independent actual-planner approval")
    if receipt["baseline_receipt_sha256"] != deployed_revision()["publication_sha256"]:
        raise ValueError("catalog submission baseline has been superseded")
    if receipt["previous_policy_sha256"] != load_curation_policy().sha256:
        raise ValueError("catalog submission policy has been superseded")
    _run(repository, ["git", "fetch", "--prune"])
    if _text(repository, ["git", "remote", "get-url", "origin"]) not in {
        "https://github.com/f5-sales-demo/html-to-markdown.git",
        "git@github.com:f5-sales-demo/html-to-markdown.git",
    }:
        raise ValueError("catalog delivery repository identity mismatch")
    for key in ("user.name", "user.email"):
        if not _text(repository, ["git", "config", key]):
            raise ValueError("catalog delivery Git identity is unavailable")
    if _main_policy(repository) != receipt["previous_policy_sha256"]:
        raise ValueError("latest protected-main policy differs from reviewed inputs")
    allowed = {
        "curation_policy.json",
        "legacy_review.json",
        "appstack_review.json",
        "vesctl_review.json",
    }
    files = receipt.get("files", {})
    if not files or set(files) - allowed:
        raise ValueError("catalog public file allowlist mismatch")
    for name, pin in files.items():
        if (
            not re.fullmatch(r"[0-9a-f]{64}", pin)
            or (staged / name).is_symlink()
            or sha((staged / name).read_bytes()) != pin
        ):
            raise ValueError("staged public catalog digest changed")
    private_output.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (private_output / "catalog-delivery.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        checkpoint = private_output / (receipt["qualification_sha256"] + ".delivery.json")
        state = json.loads(checkpoint.read_bytes()) if checkpoint.exists() else {}
        if state.get("pr"):
            _run(
                repository,
                [
                    "gh",
                    "pr",
                    "merge",
                    "--repo",
                    "f5-sales-demo/html-to-markdown",
                    "--auto",
                    "--squash",
                    state["pr"],
                ],
            )
            return state
        repo_args = ["--repo", "f5-sales-demo/html-to-markdown"]
        if not state.get("issue"):
            issue_body = """## Problem

Current source updates require renewed, exact retirement and media-placement guards.

## Scope

Promote the independently reviewed catalog batch under #167 and delivery #155.
Captured source, OCR, responses and identifying evidence remain private.

## Acceptance

- Source/media guards and two-model decisions replay exactly.
- The actual whole-corpus planner and complete preservation/privacy validation pass.
- The latest verified deployed baseline and protected-main policy match qualification.
- Repeated curation and catalog staging are byte-identical.
- A linked PR passes CI and merges through normal squash governance.
- Affected admission resumes with merged pins before content publication.
"""
            url = _text(
                repository,
                [
                    "gh",
                    "issue",
                    "create",
                    *repo_args,
                    "--title",
                    "Renew independently verified current-source curation guards",
                    "--body-file",
                    _body(private_output, "issue.md", issue_body),
                ],
            )
            state["issue"] = int(url.rsplit("/", 1)[-1])
            _save(checkpoint, state)
        branch = f"feature/{state['issue']}-reviewed-curation-catalog"
        worktree = private_output / f"worktree-{state['issue']}"
        if not worktree.exists():
            _run(
                repository,
                [
                    "git",
                    "worktree",
                    "add",
                    "--no-track",
                    "-b",
                    branch,
                    str(worktree),
                    "origin/main",
                ],
            )
        for name in files:
            atomic_private(worktree / "src/html_to_markdown" / name, (staged / name).read_bytes())
        atomic_private(worktree / "engineering/curation-renewal-receipt.json", json_bytes(receipt))
        policy = load_curation_policy(worktree / "src/html_to_markdown/curation_policy.json")
        if policy.sha256 != receipt["policy_sha256"]:
            raise ValueError("staged registry does not reproduce the qualified policy pin")
        _run(worktree, ["git", "diff", "--check"])
        _run(
            worktree,
            [
                "git",
                "add",
                *["src/html_to_markdown/" + name for name in files],
                "engineering/curation-renewal-receipt.json",
            ],
        )
        _run(worktree, ["bash", "scripts/check-pii.sh", "--scope", "staged"])
        if _text(worktree, ["git", "diff", "--cached", "--name-only"]):
            _run(
                worktree,
                [
                    "git",
                    "commit",
                    "-m",
                    "fix(curation): renew independently verified source and media guards",
                ],
            )
        _run(worktree, ["git", "push", "-u", "origin", branch])
        body = f"""Current source updates require renewed retirement and media-placement guards. This batch promotes exact, independently validated source decisions under the authoritative policy.

Closes #{state["issue"]}. Parent #167; complete delivery #155.

Validation: complete source/media review, exact offline replay, actual whole-corpus planner, zero enforcement findings, and identical repeated curation/staging. Baseline receipt `{receipt["baseline_receipt_sha256"]}`; resulting policy `{receipt["policy_sha256"]}`. Sanitized validation digests are in `engineering/curation-renewal-receipt.json`.

Source admission and immutable snapshot/deployment remain required after merge.
"""
        existing = json.loads(
            _run(worktree, ["gh", "pr", "list", *repo_args, "--head", branch, "--json", "url"])
        )
        state["pr"] = (
            existing[0]["url"]
            if existing
            else _text(
                worktree,
                [
                    "gh",
                    "pr",
                    "create",
                    *repo_args,
                    "--head",
                    branch,
                    "--title",
                    "fix(curation): renew independently verified source and media guards",
                    "--body-file",
                    _body(private_output, "pr.md", body),
                ],
            )
        )
        _save(checkpoint, state)
        _run(worktree, ["gh", "pr", "merge", *repo_args, "--auto", "--squash", state["pr"]])
        return state
