# Terraform provider curation

The `terraform-provider-current` topic removes instructions for the retired
`volterraedge/volterra` provider from the curated Markdown corpus. It runs after
SMSv2 decisions and before API-reference migration on both normal and offline
builds. Examination uses the same transaction planner and reports proposed
removals without changing the snapshot.

## Evidence boundary

The replacement provider is `f5-sales-demo/xcsh` Registry **13.1.1**, released
from `terraform-provider-xcsh` commit
`b35faa68ef9f230fa8299444aaceddab43150715`. The captured Registry version
response is `terraform_registry_13_1_1.json.gz`; its uncompressed SHA-256 is
`8dca1331ff5a6650a5d85b3923ab6f142adf9a42ac8a353c3cf222b1b724e623`.
The response lists the exact resource document paths and Registry IDs in
`terraform_destination_catalog.json`. Each promoted destination also has the
SHA-256 of its matching `v13.1.1` `docs/resources/*.md` document. These
documents contain the named `xcsh_*` resource and a complete configuration
example. Resource mappings are explicit. Any legacy resource without a
qualified mapping uses the provider landing page when retained text needs
Terraform context. The public URLs deliberately use Registry `/latest/` routes.

`terraform_linked_examples.json` records individually reviewed GitHub paths,
commits and relevant file digests. Its decisions distinguish old provider
modules from manual console guides and independent AWS, Azure, GCP, Kubernetes,
Helm, NGINX and telemetry material. Retained links resolve to pinned commits.
Unreachable and unverifiable instructional paths are removed. The catalog is
review evidence; production curation performs no network lookup.

The receipt-verified source was `content-20261004T190132Z`, with
`publication.json` SHA-256
`fb59211185470cd051fe3e06aed721c2561c7b2d182c6a98d1c41e53b5a8b128`.
It has 773 Markdown documents. Terraform text is only an examination signal:
valid `volterra_trusted_ca` API fields, API credential and service domains,
current xcsh content, and independent cloud examples remain eligible.

## Transformation

The automatic filter examines Markdown blocks and the contents of every code
fence, regardless of its language label. It detects exact provider identities,
aliases from `required_providers`, `volterra_*` resource declarations and HCL
references, retired documentation URLs, commands and reviewed linked modules.
Balanced HCL declarations let an independent cloud resource survive a mixed
fence. Unparseable legacy code is removed as a complete block. Markdown list
items, table rows, HTML nodes and reference definitions are processed with
their dependent uses. Known complex pages have digest-guarded structural
profiles for complete procedural sections; changed bytes still run through the
automatic rules.

The filter removes dependent prerequisites, variables, commands, captions and
media, then prunes empty headings. It omits a page when no substantive
independent content remains. Retained media decisions are pinned to asset
digests and reviewed visually; obsolete tutorial media is pruned. The policy
pins the review of 45 affected source pages and 159 media decisions. Existing
SMSv2 subject exclusions take precedence, including cloud-site resources
that xcsh still happens to publish.

Each topic records input and output body digests. Already-curated output is
recognized only after revalidation of retired identities, links, media and
dependencies. The curation audit is stored separately as `curation-audit.json`;
it is excluded from release assets, archives and Pages. Manifest schema v2 and
the release asset set are unchanged.

## Reproduction

Download and verify the exact source release before extraction. On a copy of
the verified corpus:

```bash
uv run html-to-markdown examine-content --output verified-corpus > impact.json
uv run html-to-markdown curate-content --output verified-corpus
uv run html-to-markdown curate-content --output verified-corpus
```

The second command's output, metadata, reports, manifest, checksums, archive
and audit must be byte-identical after the third command. Repeat from a second
copy of the same pinned input to check build determinism. Inspect all retained
affected pages and the full relationship graph before publication. The
qualified release must be published as a new immutable snapshot; earlier
content releases stay untouched.
