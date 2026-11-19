"""Ubuntu orchestrates durable jobs; isolated inference remains on the MacBook."""

from __future__ import annotations

import fcntl
import json
import os
import platform
import threading
import time
from pathlib import Path
from typing import Any

from ..content_inventory import atomic_private
from ..curation import json_bytes
from ..inference_queue import invalidate, result, submit
from .analysis import sha
from .codex_client import CodexClient
from .contracts import ResponseEvidence
from .gates import request_hash


def executor_context() -> str:
    root = Path(__file__).parent
    return sha(
        json_bytes(
            {
                name: sha((root / name).read_bytes())
                for name in [
                    "codex_client.py",
                    "contracts.py",
                    "native_visuals.py",
                    "native_pixel_views.py",
                    "visual_capacity.py",
                ]
            }
        )
    )


class QueuedClient(CodexClient):
    """Wait on resumable private Ubuntu job identities without invoking any local model."""

    # Ubuntu overrides transport construction; the parent constructor requires a Mac.
    # pylint: disable-next=super-init-not-called
    def __init__(self, journal: Path, concurrency: int = 4) -> None:
        configured = os.environ.get("HTML_TO_MARKDOWN_INFERENCE_QUEUE")
        if platform.system() != "Linux" or not configured:
            raise ValueError(
                "Ubuntu review requires an explicitly configured private Mac inference queue"
            )
        if not 1 <= concurrency <= 4:
            raise ValueError("Content inference supports at most four concurrent jobs")
        self.queue = Path(configured)
        if not self.queue.is_absolute() or self.queue.is_symlink():
            raise ValueError("Private inference queue must be an absolute real directory")
        self.queue.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.journal = journal
        self.journal.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.concurrency = concurrency
        self.executor_sha256 = executor_context()
        self.lock = (self.queue / "orchestrator.lock").open("a+b")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as failure:
            self.lock.close()
            raise ValueError(
                "Another Ubuntu review orchestrator holds the worker lock"
            ) from failure
        self._jobs: dict[str, str] = {}
        self._guards: dict[str, threading.Lock] = {}
        self._registry = threading.Lock()

    def execute(self, body: dict[str, Any]) -> ResponseEvidence:
        digest = request_hash(body)
        with self._registry:
            guard = self._guards.setdefault(digest, threading.Lock())
        with guard:
            identity = submit(self.queue, body, self.executor_sha256)
            self._jobs[digest] = identity
            pointer = self.journal / (digest + ".queued.json")
            atomic_private(
                pointer,
                json_bytes(
                    {
                        "job": identity,
                        "request_sha256": digest,
                        "executor_sha256": self.executor_sha256,
                        "state": "waiting_for_mac",
                        "article_approved": False,
                    }
                ),
            )
            while True:
                evidence = result(self.queue, identity)
                if evidence is not None:
                    atomic_private(
                        pointer,
                        json_bytes(
                            {
                                "job": identity,
                                "request_sha256": digest,
                                "response_sha256": evidence.response_sha256,
                                "state": "transport_received",
                                "article_approved": False,
                            }
                        ),
                    )
                    return evidence
                time.sleep(10)

    def invalidate(self, evidence: ResponseEvidence) -> None:
        identity = self._jobs.get(evidence.request_sha256)
        if identity is None:
            pointer = self.journal / (evidence.request_sha256 + ".queued.json")
            identity = json.loads(pointer.read_bytes())["job"]
        invalidate(self.queue, identity, evidence.response_sha256)
