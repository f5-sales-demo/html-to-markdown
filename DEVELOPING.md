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
Every changed snapshot dispatches the repository's own Pages workflow. The
project site receives the exact protected-main content commit from its caller;
it never resolves a floating release or content branch.

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
- `/html-to-markdown/llms.txt` links to one index per source and includes source roots.
- `/html-to-markdown/llms-full.txt` is an exhaustive grouped link manifest; it never embeds
  document bodies.
- `/html-to-markdown/_llms-txt/<source>/<path>.txt` contains metadata-only intermediate
  category and shared-subcategory indices plus full Markdown at canonical leaf routes.
- `/html-to-markdown/snapshot/` exposes the verified manifest, checksums, quality reports,
  provenance, Markdown, and content-addressed assets.

Progressive mode is English-only and does not publish locale routes,
`llms-small.txt`, a context-dump endpoint, or scraped-document HTML pages.
Repository `docs/llms-config.json` selects the authoritative `category` then
`subcategory` taxonomy, collapses missing or singleton subcategories, and caps
plain first-sentence hints at 240 characters. The builder consumes this file
before Starlight renders content.

## Content policy and API migration

`src/html_to_markdown/content_policy.json` is separate from metadata classification.
It excludes the two exact retired documentation path families, pins the compact
compressed destination catalog by SHA-256, and supports explicit URL overrides. The catalog
records the immutable specification release and archive digest, domain schema
digests, captured route inventory digest, and successful destination qualification.
Operation destinations use the published canonical routes without a locale prefix.

Normal runs apply the policy to discovery, benchmark and previous-release seeds,
resumed state, canonical redirects, reconciliation, and packaging. Retained content
is migrated after extraction and privacy review, including carried-forward pages.
Metadata descriptions, internal relationships, body and file hashes, checksums,
and quality reports are rebuilt. External API references remain external; original
source URLs and freshness provenance remain attached to retained documents.

Examine or migrate a verified extracted snapshot entirely offline:

```bash
uv run html-to-markdown examine-content --output verified-corpus > impact.json
uv run html-to-markdown migrate-content --output verified-corpus
```

Resolution uses an explicit override, exact operation identity from a path or
fragment, then exact resource identity with hyphen/underscore normalization.
Unmatched and ambiguous references use the catalog fallback and retain candidates
and reasons. Resource matches link to a domain overview and append a deduplicated
Related API reference section. Markdown, reference definitions, HTML, bare URLs,
and documentation URLs in code examples are rewritten; REST request paths remain.

Migration and topic evidence lives in `curation-audit.json`, a separate CI artifact.
It is excluded from archives, release assets and Pages. Consumer quality reports
measure retained content only. The manifest remains schema v2.

`curation_policy.json` declares independent versioned topics, digest-pinned evidence,
exact retired identities and URLs, reviewed source decisions, structural block
removals and media decisions. Detectors only identify review candidates. Changed
or unclassified candidates, stale/overlapping guards, unverifiable media and
incomplete dependencies are omitted. See `engineering/smsv2-curation.md` for the
first topic's analysis and ownership boundaries.

```bash
uv run html-to-markdown examine-content --output verified-corpus > impact.json
uv run html-to-markdown curate-content --output verified-corpus
```

Both normal runs and offline curation use the same transformation before API
migration and packaging. Identical inputs produce identical artifacts; reapplication
is stable. Updating captured source bytes or decisions requires a versioned PR.

Catalog updates require a versioned pull request. Download an immutable enriched
specification archive, verify its GitHub asset digest, capture the published sitemap,
then qualify every promoted operation, overview and fallback before promotion:

```bash
uv run python scripts/qualify_api_catalog.py \
  --spec-archive f5xc-api-specs-v10.0.1.zip \
  --spec-sha256 c7ed97855749306619296dbf125e510298f859098fa6ed84c34bca39537019ec \
  --routes sitemap-0.xml \
  --routes-sha256 b5491b6aa0afcf1c768c7f4f716105db2a1c322a8fc2e6c3e543d0bbfd077cd5 \
  --release-tag v10.0.1 --output candidate-catalog.json.gz
```

Qualification requires HTTP 200, the expected canonical URL, rendered method/path,
and the specification's source operation link. Any failure prevents output promotion.
Commit the qualified catalog and its updated policy digest together. Runtime migration
performs no network discovery or model inference. Previously published snapshots
remain immutable; publish migrated content under a new receipt-pinned snapshot tag.

## Curated collection landing page

The landing page explains source-to-Markdown conversion and curation. Its hero
and trunk/branch/leaf cards introduce the collection, sources/topics, and complete
documents. `CorpusBrowser` comes from the pinned docs-builder and derives its
source cards, topic cards, document summaries, breadcrumbs and search from the
verified mounted snapshot. It never maintains a separate manual page inventory.

Every card has a canonical Markdown destination. The browser supports ordinary
index links without JavaScript and progressively narrower cards with JavaScript.
Captured Markdown and assets remain unchanged; display summaries omit capture
dates and invisible text. Curation is an extensible collection feature, not a
showcase for any individual policy.
