from pathlib import Path

from html_to_markdown.models import DiscoveredPage, PageStatus
from html_to_markdown.state import StateStore


def test_state_transitions_and_resume(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite"
    store = StateStore(path)
    page = DiscoveredPage(source_id="docs-cloud-f5-com", url="https://docs.cloud.f5.com/docs-v2/a")
    store.discover([page])
    assert len(store.pending([page.source_id])) == 1
    store.mark_fetching(page.url)
    store.mark(page.url, PageStatus.SUCCESS, output_path="content/a/index.md", digest="a" * 64)
    assert store.counts() == {"success": 1}
    assert store.pending([page.source_id]) == []
    assert len(store.pending([page.source_id], force=True)) == 1
    assert store.rows()[0]["attempts"] == 1
    store.close()

    reopened = StateStore(path)
    assert reopened.counts() == {"success": 1}
    reopened.close()
