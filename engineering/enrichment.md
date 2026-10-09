# Verified article enrichment

Enrichment makes captured technical articles concise while preserving their
procedures, constraints, warnings, prerequisites, examples and useful media.
Repetition and short length identify review candidates. They do not authorize
removing content.

## Inputs and model route

The baseline is `content-20261007T124508Z`, with 712 documents and publication
SHA-256 `f3626d507f0c3ea515b683e6bceecf43cad5793f87922e2d1f80fd353df44f21`.
Verify its receipt and archive before preparing decisions. Keep this captured
release and the privacy-reviewed, policy-curated input separately.

Per the user's execution requirement, inference uses the Codex CLI installed on
the MacBook through its configured LiteLLM Responses provider. Direct OpenAI
Batch submission is available as a transport module but is not used for this
delivery. No credential is copied into artifacts or repository files.

`gpt-6.1-sol` classifies every structural address and proposes exact-span edits.
`gpt-6-astra` independently reviews the complete original and candidate, including
necessary facts, action order, descriptions and media relevance. Both use high
reasoning effort and Structured Outputs. Isolated document workers disable
execution, connectors, plugins, web search and collaboration tools. Scraped
instructions are document data. Any attempted tool execution invalidates a
worker's response.

Unique image digests receive OCR and visual review once. Each document receives
its own contextual media decision; both article models receive image input.
Captured image bytes remain unchanged. Unsupported formats and uncertain
technical images are retained. SVG review uses a PNG derivative. The derivative
is inference input, never a replacement publication asset.

## Commands

```sh
html-to-markdown analyze-corpus --output curated --report corpus-analysis.json
html-to-markdown enrich-content --mode prepare --output curated \
  --baseline baseline-release --baseline-tag content-20261007T124508Z \
  --publication-sha256 f3626d507f0c3ea515b683e6bceecf43cad5793f87922e2d1f80fd353df44f21 \
  --decisions enrichment-decisions.json
html-to-markdown enrich-content --mode media --output curated \
  --decisions enrichment-decisions.json --journal private-media-journal
html-to-markdown enrich-content --mode editor --output curated \
  --decisions enrichment-decisions.json --journal private-editor-journal
html-to-markdown enrich-content --mode validator --output curated \
  --decisions enrichment-decisions.json --journal private-validator-journal
html-to-markdown enrich-content --mode replay --output candidate \
  --decisions enrichment-decisions.json --decisions-sha256 <exact-digest>
html-to-markdown verify-enrichment --output candidate
```

Inference journals are private, mode-0600 files addressed by the complete request
digest. Interrupted runs reuse completed requests. Replay requires no inference
or credentials. Each document has an explicit accepted disposition or a whole
curated-article fallback, with the reason recorded outside consumer Markdown.

The analyzer records source metadata, complete text, structural spans, sections,
tables, procedures, code examples, links, images, captions, repetition candidates
and estimated context cost. Its character-based token estimates are advisory.
Actual model usage remains separate. LiteLLM billing rates must be supplied from
the deployment's rate card; missing rates are reported as unpriced, never zero.

## Acceptance and replay

Deterministic gates verify source addresses, hashes, nonoverlapping edits,
replacement counts, evidence references, exact protected quotes, numeric limits,
code and table payloads, action order, media identity and description completeness.
Independent validation must pass every preservation and relevance check.
Unavailable authoritative repairs remain unresolved and retain the whole article.
Runtime changes are never inferred from prose.

Alias decisions require identical canonical identity and complete substantive
text. Canonical routes remain search and inventory entries; existing duplicate
routes resolve to the canonical article. Related documents are reconstructed from
retained references. Generated API appendices are reviewed against the article's
task. Packaging verifies the preserved upstream policy chain and pinned output;
it cannot regenerate removed appendices or clipped descriptions.

The manifest remains schema v2. Its optional enrichment field pins the separate
decision artifact and contains the alias mapping. Detailed classifications,
model responses, unresolved findings, usage and transformation evidence remain
outside consumer Markdown, release content archives and Pages.

Publication requires paired builds from identical inputs and decisions, repeated
curation and packaging, full-corpus independent review, source-stratified
retrieval evaluations, and immutable receipt and deployed route verification.
The four observability procedural articles and independent Blindfold and Wingman
material remain regression requirements. Fresh inference is not assumed to be
byte-deterministic.
