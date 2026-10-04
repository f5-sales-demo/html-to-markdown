# html-to-markdown

Deterministic, resumable HTML-to-Markdown snapshots of F5 Distributed Cloud documentation at `docs.cloud.f5.com/docs-v2`, support articles at `my.f5.com/manage/s`, reviewed product and solution pages at `www.f5.com`, and Distributed Cloud articles at `community.f5.com`.

The Python 3.12 application shares fetching, normalization, Markdown rendering, asset storage, SQLite state, validation, and release packaging across four typed source adapters. The `www-f5-com` adapter accepts only the Distributed Cloud product family and three reviewed solution URLs. Generated content and state are release artifacts only; they are not committed to `main`.

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

`run` discovers, scrapes, validates, and creates `html-to-markdown-content.tar.gz` in the output directory. A snapshot is blocked unless every discovered page and referenced local asset succeeds or is reconciled against a previous snapshot.

## Enriched metadata contract

Every newly published Markdown document carries additive `metadata_schema: 1` frontmatter. The
reviewed [`metadata_rules.yaml`](src/html_to_markdown/metadata_rules.yaml) file is the sole authority
for product slugs, content and task types, aliases, lifecycle overrides, and curated cross-source
relationships. The most-specific matching path rule wins; uncertain products remain `null`.

`canonical_url` records the normalized final URL while the legacy `url` remains available.
`last_updated` uses the page modification date, then an authoritative source last-modified date,
then publication date; crawl time is never substituted. Related documents are emitted only when
their canonical targets exist in the same complete snapshot. `superseded` overrides require an
in-snapshot `replacement_url`. Lists and relationships are deduplicated and deterministically
sorted. Manifest schema v2 is unchanged because document file hashes already cover this metadata.

`--retain-previous` preserves verified prior bodies when their current pages are unavailable,
with explicit removal-candidate provenance. Community publication still requires current review
and successful extraction for every approved topic.

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
`content/docs-cloud-f5-com`, `content/my-f5-com`, `content/www-f5-com`, `content/community-f5-com`, `manifest.json`, and
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

## Community article review

`community-f5-com` reads only first posts from F5 Technical Articles, Community CodeShare,
and F5 Security Insights. Topic IDs determine paths (`content/community-f5-com/t/<id>/index.md`)
regardless of slug changes. Customer and employee contributions use the same relevance criteria.
The adapter inventories category and tag pagination and the public sitemap, then checks each
first post at two HTTP requests per second. Forums, news, events and search are excluded.

Discovery writes `build/community-inventory/inventory.json` after the full pass. It blocks when
relevance, text privacy, media privacy or image hosts need review. `topics/` holds minimized
first-post responses for review; author, profile and reply fields are discarded. Treat these
local inputs as review material; publish only the approved Markdown and diagrams.

Review `reviews.json` entries keyed by topic ID contain `article_hash`, `decision` (`include`
or `exclude`), `reason`, and `privacy` (`approved` after text and visual media inspection).
Each `assets` entry maps an image URL to its reviewed input SHA-256. Optional `asset_redactions`
map an image URL to reviewed pixel rectangles and the expected sanitized `output_sha256`.
Changed first-post content invalidates a decision. Unknown image hosts require an explicit
source-policy update. A review never bypasses the host policy. Do not approve images based
only on automated text scanning.

An interrupted pass resumes its captured listing and first-post checkpoint in the same output
directory. Use a fresh output directory for every new snapshot so all first posts are checked
again. Re-running discovery in the captured pass reevaluates review decisions. Packaging requires
a complete inventory and exactly one successful document for every accepted topic; reconciliation
cannot carry forward an excluded community article or silently omit a failed one.
