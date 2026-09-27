# Quality assessment and artifact-first snapshots

The scraper separates hard artifact integrity from advisory source and extraction quality. Invalid
frontmatter, content hashes, local asset references, manifest entries, checksums, or archives fail a
run. Authentication walls, unavailable pages, discovery reductions, and quality regressions remain
visible in the manifest and quality reports but do not suppress an otherwise valid archive.

Run a deterministic comparison with:

```bash
uv run html-to-markdown quality --candidate build \
  --reference previous \
  --benchmark benchmarks/docs-cloud-12.json
```

The command writes `quality-report.json` and `quality-report.md`. Reports include relevant-text
recall, headings, lists, tables, code blocks, callouts, figures, links, local assets, recognized
chrome, repeated cross-page blocks, promotional fragments, and title/description relevance. The
report flags weak source metadata; it never synthesizes replacement summaries.

The pinned 12-page corpora support rapid side-by-side review. The 100-page corpora expand
qualification, and `full-prototype-inventory.json` reconciles the final crawl with the pinned Docs
Cloud prototype corpus and the recorded MyF5 discovery inventory.
Use `run --inventory-only --benchmark <manifest>` for an exact pinned crawl; omit
`--inventory-only` on the final run to combine live discovery with the inventory.

When a previous release is supplied, transient failures restore its document and adjacent assets.
The manifest records freshness, the current failure, the last successful timestamp, consecutive
failures, and terminal confirmations. A previously released URL remains a removal candidate after
one terminal observation and is removed only after the second consecutive confirmation. First-run
unavailable URLs are recorded and omitted.
