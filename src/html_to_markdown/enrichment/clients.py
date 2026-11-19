"""Choose host orchestration without changing the Mac Codex inference route."""

import os
import platform
from pathlib import Path

from .codex_client import CodexClient
from .queued_client import QueuedClient


def client_for_host(
    journal: Path, concurrency: int | None = None, *, local_client: type[CodexClient] = CodexClient
) -> CodexClient:
    """Ubuntu needs an explicit private queue; the Mac keeps its configured transport."""
    if platform.system() == "Linux" and os.environ.get("HTML_TO_MARKDOWN_INFERENCE_QUEUE"):
        return QueuedClient(journal, 4 if concurrency is None else concurrency)
    if concurrency is None:
        return local_client(journal)
    return local_client(journal, concurrency=concurrency)
