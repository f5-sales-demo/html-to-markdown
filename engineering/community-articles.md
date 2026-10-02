# Community article publication

Track implementation and delivery under [issue #64](https://github.com/f5-sales-demo/html-to-markdown/issues/64).

## Scope and decisions

Inventory all public first posts in F5 Technical Articles, Community CodeShare and F5 Security
Insights. Include Distributed Cloud focused content regardless of contributor affiliation.
Exclude BIG-IP focused articles; retain incidental BIG-IP mentions and relevant integrations.
Review mixed-product and alias-only candidates individually. Do not inventory forums, news,
events, profiles or the robots-disallowed search endpoint.

Use HTTP JSON, validated community-host redirects and immutable numeric topic IDs. Topic JSON
must identify the requested topic, selected article category, visible public content and exactly
one first post. Persist only minimized article data. Slug changes preserve the numeric output
path. Dates come from first-post timestamps. Existing frontmatter types and manifest v2 remain
unchanged. Code, headings, tables, prose, lists, links and reviewed diagrams survive conversion.

Discovery exhausts category and tag pagination and reconciles their IDs with the public sitemap.
The sitemap's known `page` query is used only to identify a topic; retrieval always selects the
first post. A shared clock limits requests to two per second. Transient failures retry and
`Retry-After` pauses subsequent requests. An incomplete first-post pass blocks publication.

Each new snapshot starts in a fresh output directory. Interrupted runs resume the same captured
listing and first-post checkpoint. The full reviewed baseline supplies benchmark URLs; the
required example IDs are 73730, 72834, 72617, 70152, 71412, 72266 and 70153.

## Review evidence

Relevance and privacy decisions bind to the minimized article hash. Text and media require
inspection before approval. Image approvals additionally bind each source URL to its SHA-256
bytes in the review's `assets` mapping. Changed images fail scraping. Reviewed pixel rectangles may sanitize account or resource
identifiers; the resulting PNG strips image metadata and must match its approved output hash. Unknown image hosts require
a reviewed host-policy change. Decoration and profile data are removed; substantive diagrams
are preserved at the original lightbox resolution.

Review files contain IDs, hashes, decisions and reasons. Do not copy matched personal data or
credentials into review reasons. Local topic responses and OCR evidence are private review
inputs, never release assets. Publication contains only the existing combined content archive,
manifest, checksums, quality reports and immutable receipt.

## Verification and delivery tasks

- [x] Create linked issue and fresh Ubuntu worktree from fetched main.
- [x] Write failing source contract tests before implementing the adapter.
- [x] Register fourth source, URL policy, first-post adapter and reviewed metadata rules.
- [x] Verify numeric identity, replies, dates, Markdown preservation and privacy/media gates.
- [x] Exercise pagination, sitemap, retries, redirects and unresolved review blocking.
- [ ] Complete and review every topic in the live baseline.
- [ ] Add accepted URLs and hash-bound review decisions to the benchmark and package.
- [ ] Qualify archive sizes and member count; increase producer and pinned verifier bounds
  together only if the reviewed combined corpus exceeds current bounds.
- [ ] Finish four-source snapshot and prove prior document retention and accepted-ID equality.
- [ ] Run all final lint, type, security, packaging, coverage and installed checks.
- [ ] Obtain concrete output acceptance before merge as required by CONTRIBUTING.md.
- [ ] Merge linked PR through required CI and review repair loop.
- [ ] Publish next unused minor software version and merged-main immutable content snapshot.
- [ ] Verify downloaded releases, pinned Pages deployment, progressive leaves and assets.
- [ ] Clean this task's worktree and confirmed-merged branch; preserve other worktrees.

## Verified execution evidence

The complete live cutoff pass checked all 8,082 first posts. Relevance review selected 382
articles and excluded 7,700. The selected set includes all seven required example IDs. The
sitemap reconciles every category topic; 236 selected topics have the Distributed Cloud tag
and the baseline also includes untagged articles. Privacy and visual review remain pending.

The refreshed existing sources contain 1,066 documents and retain all 1,063 prior documents
from `content-20260928T200055Z`. Ten currently unavailable API pages retain verified prior
bodies with removal-candidate provenance under the explicit retention option.

All selected media were downloaded: 2,772 references, 2,697 unique digests. OCR completed for
all unique media with zero operational failures; 202 images were flagged for inspection.
These are private review inputs and are not published artifacts.

Measured combined payload: 473,614,237 compressed bytes, 558,782,263 expanded bytes and
6,657 members. The paired bounds are 512 MiB, 1 GiB and 20,000 members. Docs-control
[PR #2277](https://github.com/f5-sales-demo/docs-control/pull/2277) merged at
`d7f8d7be211c945d501c5dd8eb0dbfe6454fdd27`; Pages pins that commit for both workflow
and verifier. Its task worktree and branch were cleaned after merge.

Local application verification: 199 tests passed, 88.43% overall coverage and the 95%
adapter/pipeline gate passed. Ruff, formatting, mypy, Pylint, Bandit, dependency audit,
changed-file PII enforcement and audit, secret scan, wheel/sdist builds and isolated CLI
installation passed. Full privacy review, approved community extraction, final archive
verification, scraper PR, software/content releases and Pages acceptance remain open.

Inventory refresh correction: new snapshot runs now refresh category/tag/sitemap listings and every first post even when the output directory already exists. Explicit Python API resume continues the same private review pass. Failed passes invalidate the previous inventory approval report. Regression tests reproduced both stale reads and stale approval before the fixes; the complete suite passes 201 tests with 88.43% coverage. Visual inspection now covers 265 articles; inspection is separate from privacy approval. Two additional screenshot redactions are prepared and visually checked, with article approval still pending.
