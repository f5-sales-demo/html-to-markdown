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

Each snapshot refreshes every listing and first post by default. Explicit API resume continues
the same captured listing and first-post checkpoint; failed runs invalidate prior approval. The full reviewed baseline supplies benchmark URLs; the
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

The private inspection ledger now covers all 382 selected articles, including every image-bearing topic, the 29 remaining topics without images, and the separately rendered decorative SVG. This is inspection coverage, not privacy approval: recorded text and pixel edits still need completion and final review. Four additional screenshot transforms are prepared and visually checked; an identical repeated screenshot shares a verified transform. Nine specific text policies and additional tenant normalization are prepared and hash-validated. New-pass cache files are cleared before refresh so an interrupted pass cannot resume previous topic bytes. The full regression suite passes 202 tests with 88.46% coverage. Publication remains gated; no scraper PR, software release, snapshot, or Pages deployment has been published.

Credential publication enforcement now rejects remaining private-key, assigned-secret and authorization-token findings even when an article has a privacy approval. Three regression cases reproduced the bypass before correction. All 205 tests pass with 88.48% coverage and the strict adapter/pipeline 95% gate. Explicit offline re-evaluation of the captured pass confirms 8,082 topics, 382 accepted relevance decisions and 353 unresolved privacy reviews after 27 individually reviewed no-media approvals. OCR box proposals cover 2,697 unique images with zero errors; 1,437 images have candidate rectangles. These are unapproved proposals. Metadata review additionally identifies 681 images requiring metadata inspection or stripping. The combined artifact, PR and release remain unfinished.

Media review supports metadata-only, hash-bound PNG rewrites that preserve pixels and transparency. Tests first rejected metadata-only policies; the implementation now strips annotations without adding pixel rectangles. The complete suite passes 207 tests with 88.49% coverage; strict adapter/pipeline coverage passes. Private proposal generation validates unchanged pixels and absent metadata before final approval. Snapshot publication remains gated on completing 353 unresolved reviews.

Reviewed synthetic diagram labels now preserve routing relationships while replacing identifiers. The hash-bound policy validates label rectangles, font sizes and text fit. Animated input cannot pass through a still-image transform: twelve animated assets were identified and need all-frame review. Regression tests reproduced silent frame loss and missing label support before implementation. CodeShare placeholder repairs preserve fenced code under exact input/output hashes. The complete suite passes 220 tests with 88.62% coverage and the strict adapter/pipeline gate. Ruff, formatting, mypy, Bandit and changed PII checks pass. Pylint on the media module scores 10 with its unavailable import check disabled. Individually reviewed private previews now include two repaired CodeShare snippets, the required routing article with eight diagrams and five synthetic label transforms, and nine further configuration or architecture articles. The full inventory still requires completion of the remaining privacy reviews before publication.

The packaged baseline contains 251 reviewed relevance records and 51 individually approved privacy records. Only complete approved text and asset policies enter the package; unfinished private proposals remain under build. A production-baseline regression exposed stale fetcher review headers on changed content; the fetcher now discards hash-mismatched decisions before extraction. All 220 tests pass with the baseline installed, 88.60% coverage and the strict gate. The wheel includes the exact review JSON. Full extraction, final review, PR and publication remain unfinished.

GIF metadata transforms now remove comment and non-rendering application extensions while preserving compressed image data, graphic controls, loop count and timing. All-frame verification of twelve live animated assets confirms identical RGBA pixels and durations; three contain removable comments or XMP. These private transforms remain proposals until visual review completes. The full suite passes 225 tests with 88.67% coverage. The package now has 272 relevance decisions and 87 individual privacy approvals. Publication remains gated on unresolved reviews.

The production baseline now has 293 relevance decisions and 114 individual privacy approvals. Private preview validation resolved all 222 image references in 81 documents and verified content hashes. All-frame OCR now covers 1,763 GIF frames, 1,752 distinct pixel images, with zero operational failures; two animated-article groups have candidate identifiers beyond the first frame. Those remain pending. No snapshot, scraper PR, release or Pages deployment has been published.

Session-cookie publication checks now reject Cookie and Set-Cookie bearer material even with a privacy approval. Two regression examples reproduced the bypass before correction. All 227 tests pass with 88.67% coverage and the strict adapter/pipeline gate. Current packaged review decisions contain 301 relevance records and 123 individual privacy approvals. The remaining selected articles stay gated; no scraper PR or release has been published.

Review progress now includes 131 individual approvals, plus prepared media for additional required examples. The current baseline has 307 relevance records. Private preview checks resolved 276 references across 102 articles with valid asset hashes. Two animation-only architecture guides now retain GIF frames and timing while removing XMP. Required example 70153 has fourteen preserved media references and ten synthetic identifier transforms after final pixel inspection. Full publication remains unfinished.

Required examples 71412, 72266 and 72617 now have reviewed previews preserving 12, 25 and 28 images with 2, 4 and 7 identifier transforms respectively. Combined preview integrity resolves 357 asset references across 108 articles. The production baseline contains 312 relevance records and 136 individual privacy approvals; offline re-evaluation confirms 247 selected articles still require privacy review. The listener article remains pending refinement of synthetic diagram labels. No release or Pages publication has occurred.

All seven required example IDs now have individually reviewed local previews. The preview resolves all 373 asset references across 114 articles with matching hashes. Production review policy has 317 relevance records and 141 individual privacy approvals. The complete suite remains at 227 passing tests, 88.67% coverage. The snapshot remains gated on the other selected articles; no scraper PR, minor release, immutable content snapshot or Pages deployment has been published.

The baseline now contains 320 reviewed relevance decisions and 147 individual privacy approvals. Every supplied example has a reviewed preview. Remaining selected topics still require privacy review before complete extraction and publication. Local tests remain green, and meaningful diagrams have been retained with synthetic identifiers rather than removed.

Review baseline now has 321 relevance records and 148 individual privacy approvals, with subsequent API diagram review also prepared. Every no-media selected article has a privacy decision. Full media review and the complete four-source snapshot remain outstanding.

Exact packaged-policy extraction now produces a private review bundle of 158 articles and 430 image references, with matching document and asset hashes and all seven required IDs present. The wheel includes 328 review decisions. Tests pass 227 with 88.67% coverage and the strict gate. A subsequent legacy guide approval brings the baseline to 159 individual privacy approvals. Offline re-evaluation still has 224 unresolved reviews before that last approval. No complete snapshot or deployment has been published.

Current exact-policy bundle contains 158 approved articles with 430 image references; the subsequent legacy API discovery review preserves fifteen further images, bringing approvals to 159. All seven supplied examples are present and hashes validate. Latest complete offline inventory still reports 224 unresolved privacy reviews before the last approval. Wheel/sdist packaging and all 227 tests pass; output publication remains gated. One PII audit item is the Azure Logic App contentVersion 1.0.0.0, verified as a version field. Enforcement passes.

Review package now contains 329 relevance decisions and 160 individual privacy approvals. The exact-policy bundle was verified at 158 documents and 430 media references before the latest two approvals; rebuilt wheel/sdist and all 227 tests pass. Current unresolved work is media sanitization for the remaining selected articles. The seven-example preview ZIP is a review artifact only.
