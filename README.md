# html-to-markdown

Deterministic, resumable HTML-to-Markdown snapshots of the public F5 Distributed Cloud documentation at `docs.cloud.f5.com/docs-v2` and `my.f5.com/manage/s`.

The Python 3.12 application shares fetching, normalization, Markdown rendering, asset storage, SQLite state, validation, and release packaging across two typed source adapters. Generated content and state are release artifacts only; they are not committed to `main`.

## Install

```bash
uv sync --all-groups
uv run playwright install chromium
```

## Use

```bash
uv run html-to-markdown discover --source all --output build
uv run html-to-markdown scrape --source all --output build
uv run html-to-markdown run --source docs-cloud-f5-com --url \
  https://docs.cloud.f5.com/docs-v2/platform/concepts/about
uv run html-to-markdown status --output build
uv run html-to-markdown validate --output build
```

`run` discovers, scrapes, validates, and creates `dist/html-to-markdown-content.tar.gz`. A snapshot is blocked unless every discovered page and referenced local asset succeeds or has a terminal removal classification. A page-count drop over 5% from a previous manifest requires `--acknowledge-page-drop`.

Options include `--concurrency`, `--retries`, `--timeout`, `--headed`, `--force`, and `--output`. Focused `--url` runs still enforce the source allowlist.

## Consume an exact snapshot release

```bash
tag=content-YYYYMMDDTHHMMSSZ
receipt_sha256=<sha256>
gh release download "$tag" --repo f5-sales-demo/html-to-markdown --dir snapshot-release
uv run html-to-markdown verify-publication --output snapshot-release \
  --release-tag "$tag" --publication-sha256 "$receipt_sha256"
```

Never select GitHub's floating latest release for content. The archive contains
`content/docs-cloud-f5-com`, `content/my-f5-com`, `manifest.json`, and
`SHA256SUMS`. See [architecture](engineering/architecture.md),
[requirements](engineering/feature-requirements.md), and
[provenance](engineering/provenance.md).

## Development

See [DEVELOPING.md](DEVELOPING.md) for the complete immutable publication and
documentation-build contract.

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest --cov=html_to_markdown --cov-fail-under=80
uv run coverage report \
  --include='src/html_to_markdown/adapters/*,src/html_to_markdown/pipeline.py' \
  --fail-under=95
uv run bandit -q -r src
uv run pip-audit
```

## License

MIT
