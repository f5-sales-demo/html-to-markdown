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
