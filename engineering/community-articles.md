# Community article publication

Track implementation and delivery under [issue #64](https://github.com/f5-sales-demo/html-to-markdown/issues/64).

## Scope and discovery

The source checks every public first post in F5 Technical Articles, Community CodeShare,
and F5 Security Insights. Contributor affiliation does not affect inclusion.
Distributed Cloud focused articles and substantive integrations are included.
BIG-IP focused articles, forums, events, news, profiles, and search routes are excluded.

Category and tag pagination are exhausted and reconciled with sitemap IDs.
Requests share a two-per-second clock, honor Retry-After, and retry transient failures.
A failed or incomplete pass cannot publish. Each new snapshot refreshes all first posts;
explicit API resume continues the captured pass.

The reviewed cutoff contains 8,082 topics: 378 included and 7,704 excluded.
All category IDs reconcile with the sitemap. Of the included topics, 233 are tagged;
untagged discovery is essential. There are no unresolved relevance or privacy decisions.
The seven supplied example IDs are 73730, 72834, 72617, 70152, 71412, 72266, and 70153.

## Extraction and privacy

Topic IDs define stable paths at content/community-f5-com/t/ID/index.md.
Redirects, JSON identities, categories, visibility, and first-post identity are validated.
Dates use first-post timestamps. Existing metadata types and manifest schema v2 remain.

Only first-post content is extracted. Headings, prose, lists, tables, code, links,
and meaningful diagrams are retained. Replies, profiles, avatars, mention identities,
reactions, and decorative emoji are removed. The reviewed inventory identifies
2,699 image URLs; rendering also preserves repeated substantive image references.

Every included article has a hash-bound privacy approval. Image policies bind source
URLs and bytes, and any synthetic replacement also binds its output hash.
Unknown hosts and changed content reopen review. Asset hosts include the observed
community CloudFront host. Public artifacts exclude raw topic responses and OCR drafts.

Synthetic replacements retain network relationships, commands, policy outcomes, and
diagnostic evidence. Metadata-only rewrites preserve pixels and transparency.
Animated transformations preserve every reviewed frame, duration, and loop.
The final segmentation guides preserve 473 and 390 frames respectively.

The final text sweep additionally normalized explicit namespace and tenant placeholders.
Residual scanner findings require contextual review: standard Kubernetes namespaces,
executable Terraform and Azure template expressions, and authoritative upstream
maintainer attribution must retain their meaning. No blanket scanner suppression is used.
The older downstream scanner raises an exception on structured address fields;
docs-control PR #2331 already repaired it. The current authoritative scanner completes.

## Artifact and delivery verification

The prior release content-20261004T093443Z passed receipt, checksum, archive, and
closed asset-set verification. Its 1,057 documents include one newly published API page.
The candidate also preserves the older release documents retained during this task.

The combined archive bounds are 1 GiB compressed, 1 GiB expanded, and 20,000 members.
The earlier qualified corpus exceeded the old bounds, so producer and verifier changed
together. [Docs-control PR #2277](https://github.com/f5-sales-demo/docs-control/pull/2277)
merged at d7f8d7be211c945d501c5dd8eb0dbfe6454fdd27.
Pages pins that workflow and verifier commit and the existing immutable builder digest.

Local application checks pass: 245 tests, 88.75 percent coverage, and the strict
95 percent adapter and pipeline gate. Ruff, formatting, mypy, Bandit, dependency audit,
actionlint, shell PII regression tests, and gitleaks pass. Staged PII enforcement is clean.
Wheel and source distribution builds pass; an isolated v1.3.0 install exposes all four
sources and includes 378 privacy approvals.

The complete production-path candidate contains 1,445 documents and 5,518 assets.
Every accepted community URL appears once and both prior releases are fully retained.
Its archive is 825,250,970 bytes, expanded payload is 887,720,885 bytes, and it has
6,967 members. Archive verification passes under the user-authorized 1 GiB limit.
Docs-control PR #2337 merged at e6c1b8c7731a5b67bff692b29484e8a11f0fdfbf.
Pages pins that workflow and verifier for the coordinated 1 GiB increase.

The history audit flags only earlier synthetic example addresses and standard OS
user paths; current HEAD is clean. These were corrected to reserved domains and explicit
placeholders. They contain no customer or personal data, so no history rewrite is needed.

The remaining delivery includes the linked PR,
CI, human acceptance, merge, software and immutable content releases, pinned Pages
deployment, live graph checks, and scoped cleanup remain required before delivery.
