"""SQLite-backed resumable state."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .models import DiscoveredPage, PageStatus, utc_now

SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
  canonical_url TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  discovered_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  source_last_modified TEXT,
  output_path TEXT,
  content_hash TEXT,
  error_class TEXT,
  error_message TEXT
);
CREATE INDEX IF NOT EXISTS pages_source_status ON pages(source, status);
"""


class StateStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)

    def close(self) -> None:
        self.connection.close()

    def discover(self, pages: list[DiscoveredPage]) -> None:
        now = utc_now()
        with self.connection:
            self.connection.executemany(
                """INSERT INTO pages (
                       canonical_url, source, status, discovered_at, updated_at,
                       source_last_modified
                   ) VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(canonical_url) DO UPDATE SET
                     source_last_modified=excluded.source_last_modified,
                     updated_at=excluded.updated_at""",
                [
                    (
                        page.url,
                        page.source_id,
                        PageStatus.DISCOVERED,
                        now,
                        now,
                        page.source_last_modified,
                    )
                    for page in pages
                ],
            )

    def rows(self, source_ids: list[str] | None = None) -> list[sqlite3.Row]:
        rows = list(self.connection.execute("SELECT * FROM pages ORDER BY source, canonical_url"))
        return rows if not source_ids else [row for row in rows if row["source"] in source_ids]

    def pending(self, source_ids: list[str], force: bool = False) -> list[sqlite3.Row]:
        statuses = {PageStatus.DISCOVERED, PageStatus.FAILED}
        if force:
            statuses.add(PageStatus.SUCCESS)
        return [row for row in self.rows(source_ids) if PageStatus(row["status"]) in statuses]

    def mark_fetching(self, url: str) -> None:
        with self.connection:
            self.connection.execute(
                """UPDATE pages SET status=?, attempts=attempts+1, updated_at=?,
                   error_class=NULL, error_message=NULL WHERE canonical_url=?""",
                (PageStatus.FETCHING, utc_now(), url),
            )

    def mark(
        self,
        url: str,
        status: PageStatus,
        *,
        output_path: str | None = None,
        digest: str | None = None,
        error: Exception | None = None,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """UPDATE pages SET status=?, updated_at=?, output_path=?, content_hash=?,
                   error_class=?, error_message=? WHERE canonical_url=?""",
                (
                    status,
                    utc_now(),
                    output_path,
                    digest,
                    type(error).__name__ if error else None,
                    str(error) if error else None,
                    url,
                ),
            )

    def counts(self) -> dict[str, int]:
        return {
            row["status"]: row["count"]
            for row in self.connection.execute(
                "SELECT status, COUNT(*) AS count FROM pages GROUP BY status ORDER BY status"
            )
        }
