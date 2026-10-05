# AppStack retirement curation

The `appstack-retired` topic is versioned separately from SMSv2 and Terraform
curation. It runs after SMSv2 and before Terraform and API-reference migration.
The topic detects exact AppStack and VoltStack aliases, retired site paths, and
retired API identities. Managed Kubernetes procedures are excluded only when
their steps require an AppStack Customer Edge site.

The initial review uses the receipt-verified
`content-20261005T035630Z` snapshot
(`publication.json` SHA-256
`7cc12578f4d9a98ea8d686d29a78c64080fbf0ea0fabfd94abadb415926fcce7`).
All 67 pages with AppStack or VoltStack text or links have a decision in
`appstack_review.json`. The baseline review retains 43 mixed pages and removes
24 pages devoted to the retired service or its dependent procedure. Reviewed
edits bind the input and output body digests and any structural removals to
exact Markdown blocks. The 257 retained image assets have page-scoped,
digest-bound visual decisions. This scope matters because an identical generic
icon can be obsolete in one topic and useful in another.

New or changed pages use the same structural planner as examination and
application. It removes complete affected sections, list items, table rows,
paragraphs, code blocks, and references. If it cannot separate the retired
material from an independent explanation, the page is omitted. Dependency
closure examines links to retired pages and removes complete dependent blocks;
a page left without a complete explanation is omitted. Current Mesh,
security, billing, WAAP, API, networking, and independent Kubernetes content
remains in the consumer corpus.

The policy digest includes the pinned review catalog. Update the review catalog
and its digest together when reviewing changed source bytes or media. The
consumer manifest remains schema v2; detailed decisions stay only in
`curation-audit.json`, outside archives and Pages.

To check a receipt-verified extracted corpus without network discovery:

```sh
uv run html-to-markdown examine-content --output verified-corpus > impact.json
uv run html-to-markdown curate-content --output verified-corpus
uv run html-to-markdown curate-content --output verified-corpus
```

Rebuild from two separate copies of the same pinned input and compare document,
metadata, report, manifest, checksum, archive, and audit digests. Scan the
complete retained corpus and relationship graph for aliases, retired URLs and
API identities, stale media, unresolved links, and audit leakage.
