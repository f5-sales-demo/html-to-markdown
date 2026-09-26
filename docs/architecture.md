# Architecture

## Boundaries

The application is a Python 3.12 `src/` package. `SourceAdapter` is the only site-specific interface and defines discovery, readiness, classification, rendered DOM extraction, content extraction, and metadata normalization. Version 1 registers exactly `docs-cloud-f5-com` and `my-f5-com`; it has no dynamic plugin loading.

`Pipeline` owns the shared lifecycle:

1. canonicalize and allowlist discovery results;
2. persist work in SQLite;
3. fetch with retries through HTTP or one reused Chromium process;
4. classify hard/soft errors and terminal removals;
5. clean and render deterministic Markdown;
6. download content-addressed assets;
7. write ordered YAML frontmatter and validate it;
8. validate the complete snapshot and create a reproducible archive.

Each browser task gets an isolated context, bounded by one semaphore. Docs-cloud is HTTP-first and falls back to Chromium when static HTML is insufficient. MyF5 always uses Chromium because Lightning content and Shadow DOM are rendered client-side.

## Data and publication

SQLite records canonical URL, source, status, attempts, timestamps, source last-modified value, output path, content hash, and classified error. It is an optimization and resume mechanism, not an input required for a clean crawl.

The publication validator enforces artifact integrity: valid frontmatter and content hashes,
resolvable local assets, manifest consistency, checksums, and a readable deterministic archive.
Source-access failures and quality regressions are advisory. A prior successful document is carried
forward after a transient failure or first terminal observation; two consecutive terminal
confirmations remove it. First-baseline failures are recorded as unavailable and omitted.

Archives use sorted members, zero timestamps, numeric ownership, stable JSON, and content-addressed
assets. Runtime timestamps are recorded in `manifest.json`; repeat packaging of the same manifest
and tree is byte-identical. Every completed run includes the content archive, checksum, manifest,
and JSON and Markdown quality reports.
