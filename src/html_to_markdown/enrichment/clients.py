"""Choose host orchestration without changing the Mac Codex inference route."""

import os
import platform
from pathlib import Path

from .codex_client import CodexClient
from .queued_client import QueuedClient


def client_for_host(
    journal: Path, concurrency: int = 4, *, local_client: type[CodexClient] = CodexClient
) -> CodexClient:
    """Ubuntu needs an explicit private queue; the Mac keeps its configured transport."""
    if platform.system() == "Linux" and os.environ.get("HTML_TO_MARKDOWN_INFERENCE_QUEUE"):
        return QueuedClient(journal, concurrency)
    return local_client(journal, concurrency=concurrency)
