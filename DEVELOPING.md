# Developing html-to-markdown

## Immutable inputs

Production Pages builds consume exactly one timestamped `content-*` release.
Supply both `snapshot-tag` and the SHA-256 digest of that release's
`publication.json` as workflow inputs. The reusable workflow verifies the
closed release asset set, receipt, source commit, file sizes and digests,
archive checksum, safe member paths and types, `SHA256SUMS`, manifest schema,
document hashes, and asset hashes before extraction.

Never use GitHub's `latest` release or `main` as a content dependency. A
software release such as `v1.1.0` and a timestamped content snapshot are
independent identities.

The Pages caller also pins `docs-builder@sha256:<digest>` and the reusable
docs-control workflow commit. It passes that same full commit SHA as
`snapshot-verifier-ref`, ensuring the checked-out verifier is identical to the
reusable workflow revision. A manual rebuild requires the same immutable
`snapshot-tag` and `publication-sha256` values as an automatic deployment.

## Local application checks

```bash
uv sync --all-groups --locked
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest --cov=html_to_markdown --cov-fail-under=80
uv run bandit -q -r src
uv run pip-audit
```

Verify downloaded release artifacts before inspecting or extracting them:

```bash
gh release download content-YYYYMMDDTHHMMSSZ \
  --repo f5-sales-demo/html-to-markdown \
  --dir snapshot-release
uv run html-to-markdown verify-publication \
  --output snapshot-release \
  --release-tag content-YYYYMMDDTHHMMSSZ \
  --publication-sha256 <sha256>
```

The production docs-control verifier additionally binds the receipt's source
commit to the exact release tag before mounting the extracted tree read-only.

## Documentation build

Mount the one-page `docs/` directory and a verified extracted corpus
separately:

```bash
docker run --rm --pull=never \
  -e DOCS_BASE=/html-to-markdown \
  -e DOCS_SITE=https://f5-sales-demo.github.io \
  -e MACHINE_CORPUS_DIR=/content/machine-corpus \
  -v "$PWD/docs:/content/docs:ro" \
  -v "$PWD/verified-corpus:/content/machine-corpus:ro" \
  -v "$PWD/output:/output" \
  ghcr.io/f5-sales-demo/docs-builder@sha256:<digest>
```

The corpus must never be placed under `docs/` or `src/content/docs`.

## Route hierarchy

- `/html-to-markdown/` is the only human content route.
- `/llms.txt` links to one index per source and includes source roots.
- `/llms-full.txt` is an exhaustive grouped link manifest; it never embeds
  document bodies.
- `/_llms-txt/<source>/<path>.txt` contains metadata-only intermediate
  indices and full Markdown at leaf routes.
- `/snapshot/` exposes the verified manifest, checksums, quality reports,
  provenance, Markdown, and content-addressed assets.

Progressive mode is English-only and does not publish locale routes,
`llms-small.txt`, a context-dump endpoint, or scraped-document HTML pages.
